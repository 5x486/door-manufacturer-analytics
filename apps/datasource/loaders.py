# -*- coding: utf-8 -*-
"""
CSV → 业务表 导入器（apps.datasource.loaders）
=============================================
把 data/raw/*.csv 与 data/processed/*.csv 灌进 ODS 业务表。

设计要点（面试会问的三点）：
  1. **幂等**：全部用 update_or_create + 业务编码自然键，重复执行不会产生重复行，
     也不会改变已有行 ，， 这是「可重复运行的 ETL」的前提。
  2. **Decimal**：金额一律走 to_decimal 转 DecimalField，绝不用 float 累加，
     否则 2401 单的营收合计会出现分位误差。
  3. **无 N+1**：先一次性把外键映射（业务编码 → 主键 id）读进内存字典，
     再批量 upsert，4000 行明细的导入从 4000 次查询降到 1 次。
"""
from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from pathlib import Path

import pandas as pd
from django.conf import settings
from django.db import transaction

from apps.core import constants as C
from apps.masterdata.models import Customer, Product, SalesRep, Store
from apps.sales.models import AfterSales, Order, OrderItem, Payment

logger = logging.getLogger(__name__)

RAW_DIR = Path(settings.BASE_DIR) / "data" / "raw"
PROCESSED_DIR = Path(settings.BASE_DIR) / "data" / "processed"
ENCODING = "utf-8-sig"      # 原始 CSV 带 BOM，必须用 utf-8-sig 否则首列名会带 \ufeff


# ---------------------------------------------------------------------------
# 类型转换工具
# ---------------------------------------------------------------------------
def read_csv(directory: Path, name: str) -> pd.DataFrame:
    """读取 CSV，统一用 utf-8-sig（原始数据带 BOM）。"""
    path = Path(directory) / name
    if not path.exists():
        raise FileNotFoundError(f"缺少数据文件：{path}")
    return pd.read_csv(path, encoding=ENCODING)


def is_na(value) -> bool:
    """判断是否缺失（None / NaN / 空串 都算缺失）。"""
    if value is None:
        return True
    if value is pd.NaT:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        return False
    return isinstance(value, str) and value.strip() == ""


def to_dec(value, default=None) -> Decimal | None:
    """转 Decimal（金额字段必须走这里，避免浮点误差）。"""
    if is_na(value):
        return default
    return Decimal(str(value)).quantize(Decimal("0.01"))


def to_int(value, default=None) -> int | None:
    """转 int（容忍 1.0 这种浮点字符串）。"""
    if is_na(value):
        return default
    return int(float(value))


# 原始数据里的「空值占位符」。
# 生成脚本对「企业客户没有性别」这类情况写的是破折号（U+2014），
# 这在真实数据里很常见（Excel 导出的空单元格常被写成 - 或 —）。
# 导入时统一规范成空串，理由有两条：
#   1. 页面规范不允许出现破折号（会与设计系统的方向符号混淆）；
#   2. 「没有性别」的语义就是空值，把它当字符串存会让「按性别分组」多出一个假的取值。
NULL_PLACEHOLDERS = {"\u2014", "\u2013", "-", "--", "N/A", "n/a", "NULL", "null", "无"}


def to_str(value, default="") -> str:
    """
    转字符串，缺失返回默认值（默认空串，而不是 None）。

    为什么默认空串而不是 None：模型里这些字段大多是 `blank=True` 而非 `null=True`，
    在 MySQL 严格模式下写 None 会直接 IntegrityError（例如 stores.csv 里
    线上门店没有省市，值为空 → 写 NULL 就报 "Column 'province' cannot be null"）。
    业务上「没有省份」的语义就是空串，不是未知的 NULL。
    """
    if is_na(value):
        return default
    text = str(value).strip()
    if text in NULL_PLACEHOLDERS:
        return default
    return text


def to_bool01(value) -> bool:
    """把 0/1、True/False、"1"/"0" 统一成 bool。"""
    if is_na(value):
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip() in ("1", "1.0", "True", "true", "是")


def to_date(value):
    """把各种日期表现统一成 datetime.date；缺失返回 None。"""
    if is_na(value):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    parsed = pd.to_datetime(value, errors="coerce")
    if parsed is pd.NaT or pd.isna(parsed):
        return None
    return parsed.date()


# ---------------------------------------------------------------------------
# 导入计数
# ---------------------------------------------------------------------------
class ImportStat:
    """导入统计（命令输出要与 ETL 日志一起进 DataVersion，便于审计）。"""

    def __init__(self, table):
        self.table = table
        self.created = 0
        self.updated = 0
        self.skipped = 0

    @property
    def total(self):
        return self.created + self.updated + self.skipped

    def as_dict(self):
        return {
            "table": self.table,
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
            "total": self.total,
        }

    def __str__(self):
        return f"{self.table}: 新增 {self.created} / 更新 {self.updated} / 跳过 {self.skipped}"


# ---------------------------------------------------------------------------
# 主数据
# ---------------------------------------------------------------------------
def load_products() -> ImportStat:
    """产品目录。"""
    stat = ImportStat("md_product")
    df = read_csv(RAW_DIR, "products.csv")
    for _, row in df.iterrows():
        Product.objects.update_or_create(
            product_code=to_str(row["product_id"]),
            defaults=dict(
                product_name=to_str(row["product_name"], to_str(row["product_id"])),
                category=to_str(row["category"]),
                material=to_str(row["material"]),
                surface=to_str(row["surface"]),
                style=to_str(row["style"]),
                feature=to_str(row["feature"]),
                price_multiplier=to_dec(row["price_multiplier"], Decimal("1.000")),
                base_price=to_dec(row["base_price"], Decimal("0")),
                std_width=to_int(row["std_width"], 0),
                std_height=to_int(row["std_height"], 0),
                std_area=to_int(row["std_area"], 0),
                lead_min=to_int(row["lead_min"], 0),
                lead_max=to_int(row["lead_max"], 0),
                defect_rate=to_dec(row["defect_rate"], Decimal("0")),
            ),
        )
        stat.created += 1
    return stat


def load_stores() -> ImportStat:
    """门店。"""
    stat = ImportStat("md_store")
    df = read_csv(RAW_DIR, "stores.csv")
    for _, row in df.iterrows():
        Store.objects.update_or_create(
            store_code=to_str(row["store_id"]),
            # 门店名缺失时用编码兜底：store_name 是必填字段，写 None 会被数据库拒绝
            defaults=dict(
                store_name=to_str(row["store_name"], to_str(row["store_id"])),
                province=to_str(row["province"]),
                city=to_str(row["city"]),
                store_type=to_str(row["store_type"]),
                status=to_str(row["status"], "营业中"),
            ),
        )
        stat.created += 1
    return stat


def load_sales_reps() -> ImportStat:
    """业务员（关联门店）。"""
    stat = ImportStat("md_sales_rep")
    df = read_csv(RAW_DIR, "sales_reps.csv")
    store_map = {s.store_code: s for s in Store.objects.all()}
    for _, row in df.iterrows():
        SalesRep.objects.update_or_create(
            rep_code=to_str(row["rep_id"]),
            defaults=dict(
                rep_name=to_str(row["rep_name"]),
                gender=to_str(row["gender"]),
                store=store_map.get(to_str(row["store_id"])),
                hire_date=to_date(row["hire_date"]),
                level=to_str(row["level"]),
                status=to_str(row["status"], "在职"),
            ),
        )
        stat.created += 1
    return stat


def load_customers() -> ImportStat:
    """客户（含未成交线索）。total_orders / total_spend 先按 CSV 灌入，后续由 ETL 重算。"""
    stat = ImportStat("md_customer")
    df = read_csv(RAW_DIR, "customers.csv")
    for _, row in df.iterrows():
        is_lead = to_bool01(row.get("is_lead"))
        Customer.objects.update_or_create(
            customer_code=to_str(row["customer_id"]),
            # 客户名同样必填，缺失时退回编码，保证行不被丢弃
            defaults=dict(
                customer_name=to_str(row["customer_name"], to_str(row["customer_id"])),
                customer_type=to_str(row["customer_type"]),
                gender=to_str(row["gender"]),
                age=to_int(row.get("age")),
                phone=to_str(row.get("phone")),
                province=to_str(row.get("province")),
                city=to_str(row.get("city")),
                address=to_str(row.get("address")),
                channel=to_str(row["channel"]),
                register_date=to_date(row["register_date"]),
                member_level=to_str(row.get("member_level"), C.MemberLevelChoices.NORMAL.value),
                total_orders=to_int(row.get("total_orders"), 0),
                total_spend=to_dec(row.get("total_spend"), Decimal("0")),
                first_order_date=to_date(row.get("first_order_date")),
                last_order_date=to_date(row.get("last_order_date")),
                is_lead=is_lead,
            ),
        )
        stat.created += 1
    return stat


# ---------------------------------------------------------------------------
# 业务事实
# ---------------------------------------------------------------------------
def load_orders() -> ImportStat:
    """订单主表。外键用内存字典解析，避免 N+1。"""
    stat = ImportStat("fact_order")
    df = read_csv(RAW_DIR, "orders.csv")

    customer_map = {c.customer_code: c for c in Customer.objects.all()}
    store_map = {s.store_code: s for s in Store.objects.all()}
    rep_map = {r.rep_code: r for r in SalesRep.objects.all()}

    objs = []
    for _, row in df.iterrows():
        order_date = to_date(row["order_date"])
        planned = to_date(row.get("planned_delivery_date"))
        actual = to_date(row.get("actual_delivery_date"))

        # is_on_time 缺失 = 未交付 → -1（业务属性，不是脏数据）
        raw_on_time = row.get("is_on_time")
        if is_na(raw_on_time):
            is_on_time = -1
        else:
            is_on_time = 1 if to_bool01(raw_on_time) else 0

        # 实际交付周期：仅已完成订单有意义
        actual_lead_days = None
        if actual and order_date:
            actual_lead_days = (actual - order_date).days
            if actual_lead_days < 0:
                actual_lead_days = None

        delay_days = 0
        if actual and planned and actual > planned:
            delay_days = (actual - planned).days

        status = to_str(row["order_status"])

        # 成交额口径统一：final_amount = total_amount × (1 - discount) + freight。
        # 原 CSV 自带的 final_amount 是生成时四舍五入的产物，与公式存在分位差异，
        # 会让「金额对账」类校验长期处于「差 1 分钱」的灰色状态。
        # 口径只有一个来源 —— 能算出来的就不导入，避免两个真相。
        total_amount = to_dec(row["total_amount"], Decimal("0"))
        discount = to_dec(row.get("discount"), Decimal("0"))
        freight = to_dec(row.get("freight"), Decimal("0"))
        final_amount = (total_amount * (Decimal("1") - discount) + freight).quantize(Decimal("0.01"))

        objs.append(Order(
            order_no=to_str(row["order_id"]),
            customer=customer_map[to_str(row["customer_id"])],
            store=store_map.get(to_str(row.get("store_id"))),
            sales_rep=rep_map.get(to_str(row.get("sales_rep_id"))),
            customer_type=to_str(row["customer_type"]),
            channel=to_str(row["channel"]),
            order_type=to_str(row["order_type"]),
            delivery_type=to_str(row.get("delivery_type"), C.DeliveryTypeChoices.INSTALLED.value),
            order_date=order_date,
            order_status=status,
            planned_delivery_date=planned,
            actual_delivery_date=actual,
            is_on_time=is_on_time,
            total_amount=total_amount,
            discount=discount,
            freight=freight,
            final_amount=final_amount,
            lead_days=to_int(row.get("lead_days"), 0),
            actual_lead_days=actual_lead_days,
            delay_days=delay_days,
            delay_reason=("" if delay_days == 0 else C.DelayReasonChoices.NONE.value),
            dominant_category=to_str(row["dominant_category"]),
            item_count=to_int(row.get("item_count"), 0),
            order_year=to_int(row.get("order_year"), order_date.year if order_date else 0),
            order_month=to_int(row.get("order_month"), order_date.month if order_date else 0),
            order_quarter=to_int(row.get("quarter"), 0),
            order_weekday=to_int(row.get("weekday"), 0),
        ))

    # 幂等导入：按业务编码 order_no 逐条 upsert。
    # 曾写成「批量拆成 to_create / to_update 再 bulk_update」，但那批对象是新建的
    # 内存实例、pk 为 None，bulk_update 会直接抛
    # "All bulk_update() objects must have a primary key set"。
    # 用 update_or_create 语义正确、代码可读；本项目量级（2401 单）耗时在秒级，
    # 不值得为省这点时间引入「先查主键再回填」的复杂度。
    for obj in objs:
        _, is_created = Order.objects.update_or_create(
            order_no=obj.order_no,
            defaults={f.name: getattr(obj, f.name)
                      for f in Order._meta.fields
                      if f.name not in ("id", "order_no", "created_at", "updated_at")},
        )
        if is_created:
            stat.created += 1
        else:
            stat.updated += 1
    return stat


def load_order_items() -> ImportStat:
    """订单明细（关联订单与产品）。"""
    stat = ImportStat("fact_order_item")
    df = read_csv(RAW_DIR, "order_items.csv")

    order_map = {o.order_no: o for o in Order.objects.only("id", "order_no")}
    product_map = {p.product_code: p for p in Product.objects.only("id", "product_code")}

    payload = []
    for _, row in df.iterrows():
        order = order_map.get(to_str(row["order_id"]))
        product = product_map.get(to_str(row["product_id"]))
        if order is None:
            stat.skipped += 1
            continue
        payload.append(dict(
            # 业务自然键：原始 CSV 的 item_id 直接作为对外标识。
            # 此前用「整表删除重建」来回避自然键问题，代价是每次导入都会
            # 清空明细上的派生字段（成本/毛利），也让 DQ 的唯一性规则无键可查。
            item_no=to_str(row["item_id"]),
            order_id=order.id,
            product_id=product.id if product else None,
            product_name=to_str(row["product_name"]),
            category=to_str(row["category"]),
            quantity=to_int(row["quantity"], 1),
            width_mm=to_int(row["width_mm"], 0),
            height_mm=to_int(row["height_mm"], 0),
            thickness_mm=to_int(row.get("thickness_mm"), 0),
            is_custom=to_bool01(row.get("is_custom")),
            area_m2=to_dec(row.get("area_m2"), Decimal("0")),
            unit_price=to_dec(row["unit_price"], Decimal("0")),
            line_amount=to_dec(row["line_amount"], Decimal("0")),
        ))

    stat.created = _replace_items(payload)
    return stat


def _replace_items(payload) -> int:
    """
    明细表重建式导入。

    为什么仍用「删除重建」而不是逐行 upsert：
      虽然现在有 item_no 作为自然键，本命令每次都会完整重灌明细，
      删除重建能保证「库里 = CSV 的当前内容」，不会残留 CSV 里已删除的行。
      代价是明细上的成本/毛利派生字段会被清空 ，， 这由随后的
      gen_costs 步骤重新计算，整个 seed_demo 是一次完整的事务式重建。
    """
    with transaction.atomic():
        OrderItem.objects.all().delete()
        OrderItem.objects.bulk_create([OrderItem(**p) for p in payload], batch_size=1000)
    return len(payload)


def load_payments() -> ImportStat:
    """付款记录。"""
    stat = ImportStat("fact_payment")
    df = read_csv(RAW_DIR, "payments.csv")
    order_map = {o.order_no: o for o in Order.objects.only("id", "order_no")}

    objs = []
    for _, row in df.iterrows():
        order = order_map.get(to_str(row["order_id"]))
        if order is None:
            stat.skipped += 1
            continue
        objs.append(Payment(
            payment_no=to_str(row["payment_id"]),
            order=order,
            payment_date=to_date(row["payment_date"]),
            stage=to_str(row["stage"]),
            amount=to_dec(row["amount"], Decimal("0")),
            payment_method=to_str(row.get("payment_method")),
            is_refund=to_bool01(row.get("is_refund")),
        ))

    with transaction.atomic():
        Payment.objects.all().delete()
        Payment.objects.bulk_create(objs, batch_size=1000)
    stat.created = len(objs)
    return stat


def load_after_sales() -> ImportStat:
    """
    售后记录。

    注意：原始 CSV 的 product_id 字段里存的其实是**品类名**（如「铜门」），
    不只是产品编码 ，， 导入时写入 AfterSales.category，不做外键关联。
    """
    stat = ImportStat("fact_after_sales")
    df = read_csv(RAW_DIR, "after_sales.csv")
    order_map = {o.order_no: o for o in Order.objects.only("id", "order_no", "customer_id")}
    customer_map = {c.customer_code: c for c in Customer.objects.only("id", "customer_code")}

    objs = []
    for _, row in df.iterrows():
        order = order_map.get(to_str(row["order_id"]))
        if order is None:
            stat.skipped += 1
            continue
        customer = customer_map.get(to_str(row.get("customer_id")))
        objs.append(AfterSales(
            after_no=to_str(row["after_id"]),
            order=order,
            customer=customer or order.customer,
            report_date=to_date(row["report_date"]),
            after_type=to_str(row["type"]),
            category=to_str(row.get("product_id")),
            cost=to_dec(row.get("cost"), Decimal("0")),
            status=to_str(row.get("status"), C.AfterSalesStatusChoices.PROCESSING.value),
            handling_days=to_int(row.get("handling_days"), 0),
        ))

    with transaction.atomic():
        AfterSales.objects.all().delete()
        AfterSales.objects.bulk_create(objs, batch_size=500)
    stat.created = len(objs)
    return stat


# ---------------------------------------------------------------------------
# 汇总字段回填
# ---------------------------------------------------------------------------
def backfill_customer_aggregates() -> ImportStat:
    """
    重算客户累计指标（total_orders / total_spend / first_order_date / last_order_date）。

    为什么不让 CSV 里的值直接信任：口径必须由系统统一计算，
    否则「CSV 里的 total_spend」和「订单表加总」两个数会打架 ，， 这是数据治理的经典坑。
    """
    from django.db.models import Count, Max, Min, Sum

    stat = ImportStat("md_customer_backfill")
    valid = Order.objects.exclude(order_status__in=C.VALID_ORDER_EXCLUDE_STATUS)

    agg = (valid.values("customer_id")
           .annotate(n=Count("id"), amount=Sum("final_amount"),
                     first=Min("order_date"), last=Max("order_date")))

    touched = 0
    for row in agg:
        Customer.objects.filter(pk=row["customer_id"]).update(
            total_orders=row["n"],
            total_spend=row["amount"] or Decimal("0"),
            first_order_date=row["first"],
            last_order_date=row["last"],
            is_lead=False,
            frequency=row["n"],
            monetary=row["amount"] or Decimal("0"),
        )
        touched += 1

    # 无有效订单的客户 = 未成交线索
    Customer.objects.filter(total_orders=0).update(
        is_lead=True, total_spend=Decimal("0"), monetary=Decimal("0"), frequency=0,
    )
    stat.updated = touched
    return stat


def load_all() -> dict:
    """按依赖顺序导入全部数据，返回统计 dict。"""
    stats = [
        load_products(),
        load_stores(),
        load_sales_reps(),
        load_customers(),
        load_orders(),
        load_order_items(),
        load_payments(),
        load_after_sales(),
        backfill_customer_aggregates(),
    ]
    result = {}
    for stat in stats:
        result[stat.table] = stat.as_dict()
        logger.info("%s", stat)
    return result
