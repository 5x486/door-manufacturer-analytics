"""DWS 层预聚合 ETL（整表重建）。

分层口径
--------
``raw CSV → 业务表（ODS/DWD 合一） → dws_*（本模块） → selectors/视图``

为什么要有 DWS 层，而不是让视图每次现算：
1. **一致性**：同一个「月度渠道毛利」在经营总览、渠道页、导出 Excel 三处若各写
   一遍聚合逻辑，早晚会出现三个不同的数字。DWS 让它只有一份实现。
2. **性能**：明细 2401 单 / 3087 行 / 5982 笔付款，MySQL 上现算一次 8~20 个
   聚合要几秒；改成预聚合后每次只是带维度过滤的 ``SELECT``。
3. **可审计**：``DataVersion`` 记录每张表的行数、最大业务日期与版本指纹，
   页面上那句「数据截止 2026-08-09 · 版本 a1b2c3d4」就是从这里来的。

重建方式：**整表删除 + 批量插入**，不做增量 upsert。
原因：本项目的数据规模（全量重建 < 15 秒）让「确定性」比「省 CPU」更值钱，，
增量更新一旦因为某天补单而漏算，错误会永久留在表里且极难发现。

只读表的删除姿势
----------------
``DwsBusinessMonthly`` 等继承自 ``ReadOnlyModel``，其 ``save()/delete()`` 被显式
禁用（分析层不允许被应用代码随手改写）。批量重建必须绕过默认管理器：

    Model._base_manager.all().delete()

``_base_manager`` 是 Django 提供的「不做任何过滤、直连数据库」的管理器，
它不经过自定义 ``get_queryset()`` 的拦截，因此可以合法清表。
不要写成 ``Model.objects.model._default_manager.delete()`` 之类的变体，
那仍会走被拦截的管理器。

金额与比率
----------
金额字段全部 ``Decimal``（库表是 ``DecimalField``，聚合结果仍是 Decimal）；
比率字段（``gross_margin`` / ``retention_rate`` / ``on_time_rate`` …）
统一存 **0~1 的小数**，展示层再乘 100。
一份数据既要进图表（要 0~1）又要进表格（要百分数），存原始比率比存半成品灵活。
"""

from __future__ import annotations

import hashlib
import logging
import statistics
from calendar import monthrange
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.analytics.models import (
    DataVersion,
    DwsBusinessMonthly,
    DwsCategoryMonthly,
    DwsCustomerCohort,
    DwsDeliveryStage,
    DwsReceivableAging,
    DwsRepMonthly,
    DwsRepurchaseInterval,
    DwsWorkforceMonthly,
)
from apps.core.constants import DELIVERY_STAGE_SEQUENCE, CostTypeChoices
from apps.core.utils import safe_div
from apps.masterdata.models import Employee
from apps.sales.models import (
    DeliveryStage,
    LaborHour,
    Order,
    OrderItem,
    OrderItemCost,
    Payment,
    PaymentTerm,
)

logger = logging.getLogger(__name__)

__all__ = [
    "EXCLUDED_STATUS",
    "STAGE_ORDER",
    "rebuild_business_monthly",
    "rebuild_category_monthly",
    "rebuild_customer_cohort",
    "rebuild_repurchase_interval",
    "rebuild_delivery_stage",
    "rebuild_receivable_aging",
    "rebuild_workforce_monthly",
    "rebuild_rep_monthly",
    "rebuild_all",
]

ZERO = Decimal("0")
DEC2 = Decimal("0.01")
DEC4 = Decimal("0.0001")

# 有效期口径：已取消订单不进任何经营口径。
# 集中定义一次，避免各处手写 exclude(order_status="已取消") 时漏掉某一处，
# 那会导致「成交额」在两张报表里差一个已取消订单的金额。
EXCLUDED_STATUS = ["已取消"]

# 交付阶段顺序索引：直接复用 constants 里的顺序定义，保证阶段序号与业务一致
STAGE_ORDER = {stage: index for index, stage in enumerate(DELIVERY_STAGE_SEQUENCE)}

# 客户队列的最大月数：42 个月（3 年半）。再往外样本量为 0，只会制造噪声行。
COHORT_MAX_MONTH_INDEX = 42


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def _period(order) -> str:
    """把订单的期间拼成 ``YYYY-MM``。

    用 ``order_year`` / ``order_month`` 而不是 ``order_date``：
    这两列在导入时已落库，直接取值免去 MySQL 日期函数运算，
    也让「订单表里的年份」与「看板上的年份」永远一致（不必考虑时区）。
    """
    year = order.order_year
    month = order.order_month
    if not year or not month:
        return ""
    return "{:04d}-{:02d}".format(int(year), int(month))


def _q2(value) -> Decimal:
    """金额规整到分。"""
    if value is None:
        return ZERO
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(DEC2, rounding=ROUND_HALF_UP)


def _q4(value) -> Decimal:
    """比率规整到 4 位小数（``DecimalField(max_digits=6, decimal_places=4)``）。"""
    if value is None:
        return ZERO
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(DEC4, rounding=ROUND_HALF_UP)


def _ratio(numerator, denominator) -> Decimal:
    """比率：分母为 0 或缺失时返回 0，并统一量化到 4 位。

    与 ``safe_div`` 的分工：``safe_div`` 返回 ``None`` 表示「算不出来」，
    而 DWS 表是物理存储，必须有确定值，因此在存储边界把 ``None`` 收敛为 0。
    """
    value = safe_div(numerator, denominator)
    if value is None:
        return ZERO
    return _q4(value)


def _d(value) -> Decimal:
    """把任意数值安全转 ``Decimal``，None → 0。"""
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _truncate_table(model) -> None:
    """清空只读 DWS 表（走 ``_base_manager``，见模块 docstring）。"""
    model._base_manager.all().delete()


def _month_diff(start: tuple, end: tuple) -> int:
    """月份差：``(2025, 3) → (2025, 7)`` 得 4。"""
    return (end[0] * 12 + end[1]) - (start[0] * 12 + start[1])


def _order_month_tuple(order):
    """订单的 ``(year, month)``；缺失时返回 ``None``，由调用方跳过。"""
    if not order.order_year or not order.order_month:
        return None
    return int(order.order_year), int(order.order_month)


def _as_of_date() -> date:
    """数据截止日；配置缺失时退回项目约定的 2026-08-09。"""
    value = getattr(settings, "DATA_AS_OF_DATE", None)
    if isinstance(value, date):
        return value
    raw = getattr(settings, "DATA_AS_OF", None)
    if isinstance(raw, str) and raw.strip():
        try:
            return date.fromisoformat(raw.strip())
        except ValueError:
            pass
    return date(2026, 8, 9)


def _version_token(table_name: str, row_count: int, now) -> str:
    """数据版本指纹：``md5(表名 + 行数 + 构建时刻)`` 的前 8 位十六进制。

    为什么不用自增 id：看板要把版本号显示给老板看，并要求「这张图是哪次构建出的」。
    指纹同时表达「变了没有」和「什么时候变的」，且两张表指纹不同，
    便于排查「只重建了一半」这类事故。
    """
    raw = "{}{}{}".format(table_name, row_count, now.isoformat())
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]


def _record_version(table_name: str, layer: str, row_count: int, max_event_date) -> str:
    """写一条 ``DataVersion`` 并返回版本指纹。"""
    now = timezone.now()
    token = _version_token(table_name, row_count, now)
    DataVersion._base_manager.update_or_create(
        table_name=table_name,
        defaults={
            "layer": layer,
            "row_count": row_count,
            "max_event_date": max_event_date,
            "version_token": token,
        },
    )
    return token


def _iter_valid_orders():
    """有效期订单（排除已取消）。"""
    return Order.objects.exclude(order_status__in=EXCLUDED_STATUS).order_by("order_date")


def _load_items() -> tuple:
    """一次扫描明细与成本，返回 ``(items_by_order, cost_by_item, item_meta)``。

    三个都来自同一次读取，保证：
    ``Σ 行成本 == Σ 六类 OrderItemCost``。
    如果一边读 ``OrderItem.total_cost`` 一边读成本明细，两者一旦不同步，
    就会出现「毛利」和「成本结构」对不上的经典事故。

    ``item_meta`` 是 ``{item_id: {'order_id','category','product_code','quantity',
    'line_amount','gross_profit'}}`` 的轻量索引，供按成本类型二次归集时使用，，
    避免为了拿这几个字段再做一次全表扫描。
    """
    cost_by_item: dict = defaultdict(Decimal)
    for item_id, amount in OrderItemCost.objects.values_list("item_id", "amount"):
        if item_id is None:
            continue
        cost_by_item[item_id] += _d(amount)

    items_by_order: dict = defaultdict(list)
    item_meta: dict = {}
    queryset = OrderItem.objects.select_related("product").only(
        "id",
        "order_id",
        "product_id",
        "product__product_code",
        "category",
        "quantity",
        "line_amount",
        "gross_profit",
        "total_cost",
    )
    for item in queryset.iterator(chunk_size=1000):
        items_by_order[item.order_id].append(item)
        item_meta[item.id] = {
            "order_id": item.order_id,
            "category": item.category or "",
            "product_code": (getattr(item.product, "product_code", "") or "")
            if item.product_id
            else "",
            "quantity": int(item.quantity or 0),
        }
    return items_by_order, cost_by_item, item_meta


def _item_cost(item, cost_by_item) -> Decimal:
    """单条明细的成本：优先取成本明细合计，其次行上的 ``total_cost``，
    最后用「行金额 - 毛利」反推。三级回退是为了在成本生成器尚未执行时，
    毛利仍然算得出来（而不是整表毛利为 0）。"""
    cost = cost_by_item.get(item.id, ZERO)
    if cost != 0:
        return cost
    if item.total_cost is not None:
        return _d(item.total_cost)
    return _d(item.line_amount) - _d(item.gross_profit)


def _item_profit(item, cost_by_item) -> Decimal:
    """单条明细毛利：优先用已落库的 ``gross_profit``，缺失时现算。"""
    if item.gross_profit is not None:
        return _d(item.gross_profit)
    return _d(item.line_amount) - _item_cost(item, cost_by_item)


# ---------------------------------------------------------------------------
# ① 月度经营宽表：期间 × 渠道 × 品类
# ---------------------------------------------------------------------------
@transaction.atomic
def rebuild_business_monthly() -> dict:
    """重建 ``DwsBusinessMonthly``（粒度：期间 × 渠道 × 品类）。

    口径（与看板文案必须一致）：
    - 有效期：``order_status != 已取消``；
    - ``revenue`` = Σ ``final_amount``（含运费、已扣让利），是「成交额」唯一口径；
    - ``discount_amount`` = Σ ``total_amount × discount``（让利金额，正数表示让掉的钱）；
    - ``gross_profit`` = Σ 该订单明细毛利，``total_cost`` = Σ 明细成本；
    - ``customer_count`` 为**去重成交客户数**；``new_customer_count`` 是该月内
      客户首次成交（``first_order_date`` 落在该月）的人数；
    - ``avg_order_value`` = revenue / order_count；``gross_margin`` = 毛利 / revenue。

    维度取 ``Order.channel`` 与 ``Order.dominant_category``（主品类），
    而不是明细里的品类：渠道归因与订单级让利都发生在订单粒度，
    用主品类能把一笔订单完整归到一格。若按明细品类拆行，
    「订单数」会虚增，而订单级让利又无法在行间拆分，，这是本项目刻意回避的口径陷阱。
    """
    items_by_order, cost_by_item, _item_meta = _load_items()

    stats: dict = {}
    customers_by_group: dict = defaultdict(set)
    new_customers_by_group: dict = defaultdict(set)
    max_event_date = None

    orders = _iter_valid_orders().select_related("customer").only(
        "id",
        "order_year",
        "order_month",
        "order_date",
        "channel",
        "dominant_category",
        "customer_id",
        "customer__first_order_date",
        "final_amount",
        "total_amount",
        "discount",
    )
    for order in orders.iterator(chunk_size=1000):
        period = _period(order)
        if not period:
            continue
        key = (period, order.channel or "", order.dominant_category or "")

        gross_profit = ZERO
        total_cost = ZERO
        for item in items_by_order.get(order.id, ()):
            gross_profit += _item_profit(item, cost_by_item)
            total_cost += _item_cost(item, cost_by_item)

        revenue = _d(order.final_amount)
        discount_amount = _q2(_d(order.total_amount) * _d(order.discount))

        slot = stats.setdefault(
            key,
            {
                "order_count": 0,
                "revenue": ZERO,
                "gross_profit": ZERO,
                "total_cost": ZERO,
                "discount_amount": ZERO,
            },
        )
        slot["order_count"] += 1
        slot["revenue"] += revenue
        slot["gross_profit"] += gross_profit
        slot["total_cost"] += total_cost
        slot["discount_amount"] += discount_amount

        if order.customer_id is not None:
            customers_by_group[key].add(order.customer_id)
            first_order_date = getattr(order.customer, "first_order_date", None)
            if (
                first_order_date is not None
                and order.order_date is not None
                and (first_order_date.year, first_order_date.month)
                == (order.order_date.year, order.order_date.month)
            ):
                new_customers_by_group[key].add(order.customer_id)

        if order.order_date is not None and (
            max_event_date is None or order.order_date > max_event_date
        ):
            max_event_date = order.order_date

    rows = []
    for (period, channel, category), slot in sorted(stats.items()):
        order_count = slot["order_count"]
        revenue = slot["revenue"]
        gross_profit = slot["gross_profit"]
        rows.append(
            DwsBusinessMonthly(
                period=period,
                year=int(period[:4]),
                month=int(period[5:7]),
                channel=channel,
                category=category,
                order_count=order_count,
                customer_count=len(customers_by_group.get((period, channel, category), ())),
                new_customer_count=len(
                    new_customers_by_group.get((period, channel, category), ())
                ),
                revenue=_q2(revenue),
                gross_profit=_q2(gross_profit),
                total_cost=_q2(slot["total_cost"]),
                discount_amount=_q2(slot["discount_amount"]),
                avg_order_value=_q2(safe_div(revenue, order_count, default=ZERO)),
                gross_margin=_ratio(gross_profit, revenue),
            )
        )

    _truncate_table(DwsBusinessMonthly)
    if rows:
        DwsBusinessMonthly._base_manager.bulk_create(rows, batch_size=1000)
    token = _record_version("dws_business_monthly", "dws", len(rows), max_event_date)
    logger.info("rebuild_business_monthly: rows=%s token=%s", len(rows), token)
    return {"table": "dws_business_monthly", "rows": len(rows), "version_token": token}


# ---------------------------------------------------------------------------
# ② 品类 × 产品 月表
# ---------------------------------------------------------------------------
@transaction.atomic
def rebuild_category_monthly() -> dict:
    """重建 ``DwsCategoryMonthly``（粒度：期间 × 品类 × 产品编码）。

    口径：
    - ``qty`` = Σ ``quantity``（樘/套，不是平方米）；
    - ``revenue`` = Σ ``line_amount``（明细折前口径；与订单口径 ``final_amount``
      的差额是订单级折扣减去运费，所以品类页的金额会比首页成交额略高）；
    - 五类成本分别落列，不合并成一项，，「铜门到底是材料贵还是人工贵」
      正是品类利润分析要回答的问题；
    - ``gross_margin`` = 毛利 / 收入；``avg_discount`` 取该格所涉及订单的
      折扣率**按订单去重后的均值**（一单多行不能把折扣权重放大）。
    """
    items_by_order, cost_by_item, item_meta = _load_items()

    order_meta: dict = {}
    max_event_date = None
    for order in _iter_valid_orders().only(
        "id", "order_year", "order_month", "order_date", "discount"
    ):
        period = _period(order)
        if not period:
            continue
        order_meta[order.id] = {"period": period, "discount": _d(order.discount)}
        if order.order_date is not None and (
            max_event_date is None or order.order_date > max_event_date
        ):
            max_event_date = order.order_date

    grouped: dict = {}
    discounts_by_group: dict = defaultdict(set)

    for order_id, lines in items_by_order.items():
        meta = order_meta.get(order_id)
        if meta is None:
            continue  # 已取消订单
        period = meta["period"]
        for item in lines:
            key = (period, item.category or "", item_meta.get(item.id, {}).get("product_code", ""))
            bucket = grouped.setdefault(
                key,
                {
                    "qty": 0,
                    "revenue": ZERO,
                    "material": ZERO,
                    "labor": ZERO,
                    "manufacturing": ZERO,
                    "logistics": ZERO,
                    "installation": ZERO,
                    "gross_profit": ZERO,
                    "total_cost": ZERO,
                },
            )
            bucket["qty"] += int(item.quantity or 0)
            bucket["revenue"] += _d(item.line_amount)
            bucket["gross_profit"] += _item_profit(item, cost_by_item)
            bucket["total_cost"] += _item_cost(item, cost_by_item)
            discounts_by_group[key].add(meta["discount"])

    # 成本按类型落到各列：需要再走一遍成本明细（成本明细是明细粒度）。
    # 键必须是库里的真实值（中文成本类型），不是英文列名：之前写成
    # {"material": "material", ...}，而 OrderItemCost.cost_type 存的是
    # 材料成本 / 人工成本 / ...，get() 永远返回 None，五类成本列全部落成 0。
    # 表现是品类利润页「材料成本占收入」一直是 0.0%，而毛利率又是对的，
    # 属于最不容易被发现的那类静默错误。
    cost_columns = {
        CostTypeChoices.MATERIAL.value: "material",
        CostTypeChoices.LABOR.value: "labor",
        CostTypeChoices.MANUFACTURING.value: "manufacturing",
        CostTypeChoices.LOGISTICS.value: "logistics",
        CostTypeChoices.INSTALLATION.value: "installation",
    }
    for item_id, cost_type, amount in OrderItemCost.objects.values_list(
        "item_id", "cost_type", "amount"
    ):
        field = cost_columns.get(cost_type)
        if field is None:
            continue  # 售后成本单列在 OrderItemCost 里，不计入五类标准成本
        meta = item_meta.get(item_id)
        if meta is None:
            continue
        order = order_meta.get(meta["order_id"])
        if order is None:
            continue
        key = (order["period"], meta["category"], meta["product_code"])
        bucket = grouped.get(key)
        if bucket is None:
            continue
        bucket[field] += _d(amount)

    rows = []
    for (period, category, product_code), bucket in sorted(grouped.items()):
        revenue = bucket["revenue"]
        gross_profit = bucket["gross_profit"]
        if gross_profit == 0 and bucket["total_cost"] > 0:
            gross_profit = revenue - bucket["total_cost"]
        stored_discounts = discounts_by_group.get((period, category, product_code), set())
        avg_discount = ZERO
        if stored_discounts:
            avg_discount = _q4(sum(stored_discounts) / Decimal(len(stored_discounts)))
        rows.append(
            DwsCategoryMonthly(
                period=period,
                category=category,
                product_code=product_code,
                qty=bucket["qty"],
                revenue=_q2(revenue),
                material_cost=_q2(bucket["material"]),
                labor_cost=_q2(bucket["labor"]),
                manufacturing_cost=_q2(bucket["manufacturing"]),
                logistics_cost=_q2(bucket["logistics"]),
                installation_cost=_q2(bucket["installation"]),
                gross_profit=_q2(gross_profit),
                gross_margin=_ratio(gross_profit, revenue),
                avg_discount=avg_discount,
            )
        )

    _truncate_table(DwsCategoryMonthly)
    if rows:
        DwsCategoryMonthly._base_manager.bulk_create(rows, batch_size=1000)
    token = _record_version("dws_category_monthly", "dws", len(rows), max_event_date)
    logger.info("rebuild_category_monthly: rows=%s", len(rows))
    return {"table": "dws_category_monthly", "rows": len(rows), "version_token": token}


# ---------------------------------------------------------------------------
# ③ 客户留存队列
# ---------------------------------------------------------------------------
@transaction.atomic
def rebuild_customer_cohort() -> dict:
    """重建 ``DwsCustomerCohort``（粒度：首单月队列 × 第 N 月）。

    口径：
    - **队列** = 客户**首单月份**（不是注册月份：注册不等于真实成交，
      用注册月会得到一条毫无意义的「注册后自然留存」曲线）；
    - ``month_index`` = 距今月数，0 表示首单当月；
    - ``customer_count`` = 队列总人数；``active_count`` = 第 N 月有成交的人数；
    - ``retention_rate`` = active / 队列总人数（0~1，4 位小数）；
    - ``month_index`` 上界 = min(42, 数据截止月 - 队列月)，避免出现
      「37 人的队列在第 60 个月」这种样本量为 0 的噪声行。

    客户级去重是必须的：一个客户当月下两单，若按订单计数，留存率会超过 1。
    """
    as_of = _as_of_date()
    as_of_tuple = (as_of.year, as_of.month)

    orders = (
        Order.objects.exclude(order_status__in=EXCLUDED_STATUS)
        .filter(customer_id__isnull=False)
        .only("id", "customer_id", "order_year", "order_month", "order_date", "final_amount")
        .order_by("order_date")
    )

    cohort_by_customer: dict = {}
    max_event_date = None
    for order in orders.iterator(chunk_size=1000):
        month_tuple = _order_month_tuple(order)
        if month_tuple is None:
            continue
        current = cohort_by_customer.get(order.customer_id)
        if current is None or month_tuple < current:
            cohort_by_customer[order.customer_id] = month_tuple
        if order.order_date is not None and (
            max_event_date is None or order.order_date > max_event_date
        ):
            max_event_date = order.order_date

    cohort_totals: dict = defaultdict(int)
    for customer_id, cohort in cohort_by_customer.items():
        cohort_totals[cohort] += 1

    active_sets: dict = defaultdict(set)
    revenue_by_slot: dict = defaultdict(Decimal)
    for order in orders.iterator(chunk_size=1000):
        month_tuple = _order_month_tuple(order)
        if month_tuple is None:
            continue
        cohort = cohort_by_customer.get(order.customer_id)
        if cohort is None:
            continue
        index = _month_diff(cohort, month_tuple)
        active_sets[(cohort, index)].add(order.customer_id)
        revenue_by_slot[(cohort, index)] += _d(order.final_amount)

    rows = []
    for (cohort, index), customers in sorted(active_sets.items()):
        max_index = _month_diff(cohort, as_of_tuple)
        if index < 0 or index > COHORT_MAX_MONTH_INDEX or index > max(max_index, 0):
            continue
        total = cohort_totals.get(cohort, 0)
        if total <= 0:
            continue
        rows.append(
            DwsCustomerCohort(
                cohort_month="{:04d}-{:02d}".format(cohort[0], cohort[1]),
                month_index=index,
                customer_count=total,
                active_count=len(customers),
                retention_rate=_ratio(len(customers), total),
                revenue=_q2(revenue_by_slot.get((cohort, index), ZERO)),
            )
        )

    _truncate_table(DwsCustomerCohort)
    if rows:
        DwsCustomerCohort._base_manager.bulk_create(rows, batch_size=1000)
    token = _record_version("dws_customer_cohort", "dws", len(rows), max_event_date)
    logger.info("rebuild_customer_cohort: cohorts=%s rows=%s", len(cohort_totals), len(rows))
    return {
        "table": "dws_customer_cohort",
        "rows": len(rows),
        "cohorts": len(cohort_totals),
        "version_token": token,
    }


# ---------------------------------------------------------------------------
# ④ 复购间隔
# ---------------------------------------------------------------------------
@transaction.atomic
def rebuild_repurchase_interval() -> dict:
    """重建 ``DwsRepurchaseInterval``（粒度：品类 × 渠道 + 一条全量行）。

    为什么额外写一条 ``category="" & channel=""`` 的全量行：
    细分维度下样本量太小（某个品类可能只有十几对间隔），只给细分会让页面上
    「平均复购间隔」抖动得没法用；客户运营需要一个稳定的整体基准线。

    口径：``interval_days`` = 同一客户相邻两单下单日间隔的**平均天数**；
    ``customer_count`` = 产生过复购（≥2 单）的客户数，而不是间隔对的数量，，
    否则一个下单 20 次的大客户能顶掉十几个真实客户，把均值拉向自己。
    """
    orders = (
        Order.objects.exclude(order_status__in=EXCLUDED_STATUS)
        .filter(customer_id__isnull=False, order_date__isnull=False)
        .only("id", "customer_id", "order_date", "dominant_category", "channel")
        .order_by("customer_id", "order_date")
    )

    per_customer: dict = defaultdict(list)
    max_event_date = None
    for order in orders.iterator(chunk_size=1000):
        per_customer[order.customer_id].append(order)
        if max_event_date is None or order.order_date > max_event_date:
            max_event_date = order.order_date

    all_intervals = []
    repeat_customers = 0
    grouped_intervals: dict = defaultdict(list)
    grouped_customers: dict = defaultdict(set)

    for customer_id, customer_orders in per_customer.items():
        if len(customer_orders) < 2:
            continue
        repeat_customers += 1
        for previous, current in zip(customer_orders, customer_orders[1:]):
            gap = (current.order_date - previous.order_date).days
            if gap <= 0:
                continue
            all_intervals.append(gap)
            # 一笔复购同时体现「上一单的品类渠道」与「本单的品类渠道」，
            # 两边都记一次，避免只归属到其中一侧导致维度漏样本。
            for key in (
                (previous.dominant_category or "", previous.channel or ""),
                (current.dominant_category or "", current.channel or ""),
            ):
                grouped_intervals[key].append(gap)
                grouped_customers[key].add(customer_id)

    rows = []
    if all_intervals:
        rows.append(
            DwsRepurchaseInterval(
                category="",
                channel="",
                interval_days=int(round(statistics.fmean(all_intervals))),
                customer_count=repeat_customers,
            )
        )
    for key in sorted(grouped_intervals.keys()):
        intervals = grouped_intervals[key]
        if not intervals:
            continue
        rows.append(
            DwsRepurchaseInterval(
                category=key[0],
                channel=key[1],
                interval_days=int(round(statistics.fmean(intervals))),
                customer_count=len(grouped_customers.get(key, ())),
            )
        )

    _truncate_table(DwsRepurchaseInterval)
    if rows:
        DwsRepurchaseInterval._base_manager.bulk_create(rows, batch_size=1000)
    token = _record_version("dws_repurchase_interval", "dws", len(rows), max_event_date)
    logger.info("rebuild_repurchase_interval: rows=%s", len(rows))
    return {"table": "dws_repurchase_interval", "rows": len(rows), "version_token": token}


# ---------------------------------------------------------------------------
# ⑤ 交付阶段
# ---------------------------------------------------------------------------
@transaction.atomic
def rebuild_delivery_stage() -> dict:
    """重建 ``DwsDeliveryStage``（粒度：年 × 品类 × 阶段）。

    瓶颈指数公式（本项目自定义，指标字典里必须同步写明）：

        bottleneck_score = (该阶段平均耗时 ÷ 同年同品类各阶段平均耗时之和) × 超时率

    拆成两个因子相乘，是因为单独任一因子都给不出有用结论：
    - **耗时占比** 衡量「这个阶段有多重」。生产天然占大头，排第一不是瓶颈，
      而是工艺决定的常态；
    - **超时率** 衡量「这个阶段有多不可控」。单看它，接单确认也会上榜
      （超时 1 天也算超时，实际影响微乎其微）。
    只有又重又不可控的阶段才配叫瓶颈，，两者相乘后，结论会稳定落在
    「排产」「物流」上，与车间体感一致。

    - ``median_duration_days`` 用中位数：一单特大工程门能把平均值拉飞，
      中位数不会；
    - ``delay_count`` = 该阶段 ``delayed_days > 0`` 的订单数；
      ``avg_delay_days`` 只在延期样本内求平均（不混入 0，否则被稀释）。
    """
    as_of_year = _as_of_date().year

    durations: dict = defaultdict(list)
    delay_durations: dict = defaultdict(list)
    order_sets: dict = defaultdict(set)
    delay_sets: dict = defaultdict(set)

    stage_rows = DeliveryStage.objects.select_related("order").only(
        "id",
        "order_id",
        "stage",
        "stage_order",
        "duration_days",
        "delayed_days",
        "order__order_year",
        "order__dominant_category",
        "order__order_status",
    )
    for stage in stage_rows.iterator(chunk_size=1000):
        order = stage.order
        if order is None or order.order_status in EXCLUDED_STATUS:
            continue
        year = order.order_year
        if not year or int(year) > as_of_year:
            # 跨年计划产生的未来年度阶段记录不参与交付分析
            continue
        key = (int(year), order.dominant_category or "", stage.stage)
        duration = int(stage.duration_days or 0)
        durations[key].append(duration)
        order_sets[key].add(stage.order_id)
        delayed = int(stage.delayed_days or 0)
        if delayed > 0:
            delay_sets[key].add(stage.order_id)
            delay_durations[key].append(delayed)

    # 同年同品类「各阶段平均耗时之和」作为占比分母
    category_avg_sum: dict = defaultdict(Decimal)
    for (year, category, _stage), values in durations.items():
        if values:
            category_avg_sum[(year, category)] += _d(statistics.fmean(values))

    rows = []
    for (year, category, stage), values in sorted(durations.items()):
        order_count = len(order_sets[(year, category, stage)])
        if order_count == 0 or not values:
            continue
        avg_duration = _d(statistics.fmean(values))
        delay_count = len(delay_sets.get((year, category, stage), ()))
        delay_rate = _ratio(delay_count, order_count)
        avg_delay = ZERO
        delayed_values = delay_durations.get((year, category, stage))
        if delayed_values:
            avg_delay = _q2(_d(statistics.fmean(delayed_values)))
        share = safe_div(avg_duration, category_avg_sum.get((year, category), ZERO), default=ZERO)
        score = _d(share) * delay_rate
        rows.append(
            DwsDeliveryStage(
                year=year,
                category=category,
                stage=stage,
                stage_order=int(STAGE_ORDER.get(stage, 99)),
                order_count=order_count,
                avg_duration_days=_q2(avg_duration),
                median_duration_days=_q2(_d(statistics.median(values))),
                delay_count=delay_count,
                delay_rate=delay_rate,
                avg_delay_days=avg_delay,
                bottleneck_score=_q4(score),
            )
        )

    _truncate_table(DwsDeliveryStage)
    if rows:
        DwsDeliveryStage._base_manager.bulk_create(rows, batch_size=1000)
    token = _record_version("dws_delivery_stage", "dws", len(rows), None)
    logger.info("rebuild_delivery_stage: rows=%s", len(rows))
    return {"table": "dws_delivery_stage", "rows": len(rows), "version_token": token}


# ---------------------------------------------------------------------------
# ⑥ 应收账款账龄
# ---------------------------------------------------------------------------
@transaction.atomic
def rebuild_receivable_aging() -> dict:
    """重建 ``DwsReceivableAging``（粒度：as_of_date × 账龄桶 × 客户）。

    口径：
    - ``as_of_date`` 一律取 ``settings.DATA_AS_OF_DATE``。账龄是「截至某天的状态」，
      不允许各页面各取一天，否则同一客户在两张报表里的账龄会不一样；
    - ``outstanding_amount`` = Σ 未收金额；``order_count`` = 未结清订单数
      （未收 > 0.01 元），已结清单不进本表；
    - ``max_overdue_days`` = 该客户在该桶内的最大逾期天数，用于回答「先催谁」；
    - 客户缺失的应收跳过：没有主体的应收无法催收，属于数据质量问题，
      交给 DQ 的规则去暴露，而不是在这里编一个「未知客户」。
    """
    as_of = _as_of_date()

    stats: dict = {}
    max_event_date = None
    terms = (
        PaymentTerm.objects.filter(outstanding_amount__gt=Decimal("0.01"))
        .select_related("order")
        .only(
            "id",
            "outstanding_amount",
            "overdue_days",
            "aging_bucket",
            "order__customer_id",
            "order__order_status",
            "order__order_date",
        )
    )
    for term in terms.iterator(chunk_size=1000):
        order = term.order
        if order is None or order.customer_id is None:
            continue
        if order.order_status in EXCLUDED_STATUS:
            continue
        key = (term.aging_bucket or "0_30", order.customer_id)
        slot = stats.setdefault(key, {"order_count": 0, "outstanding": ZERO, "max_overdue": 0})
        slot["order_count"] += 1
        slot["outstanding"] += _d(term.outstanding_amount)
        overdue = int(term.overdue_days or 0)
        if overdue > slot["max_overdue"]:
            slot["max_overdue"] = overdue
        if order.order_date is not None and (
            max_event_date is None or order.order_date > max_event_date
        ):
            max_event_date = order.order_date

    rows = []
    for key in sorted(stats.keys()):
        bucket, customer_id = key
        slot = stats[key]
        rows.append(
            DwsReceivableAging(
                as_of_date=as_of,
                aging_bucket=bucket,
                customer_id=customer_id,
                order_count=slot["order_count"],
                outstanding_amount=_q2(slot["outstanding"]),
                max_overdue_days=slot["max_overdue"],
            )
        )

    _truncate_table(DwsReceivableAging)
    if rows:
        DwsReceivableAging._base_manager.bulk_create(rows, batch_size=1000)
    total = sum((row.outstanding_amount for row in rows), ZERO)
    token = _record_version("dws_receivable_aging", "dws", len(rows), max_event_date)
    logger.info("rebuild_receivable_aging: rows=%s 未收合计=%s", len(rows), total)
    return {
        "table": "dws_receivable_aging",
        "rows": len(rows),
        "outstanding_total": total,
        "version_token": token,
    }


# ---------------------------------------------------------------------------
# ⑦ 人效
# ---------------------------------------------------------------------------
@transaction.atomic
def rebuild_workforce_monthly() -> dict:
    """重建 ``DwsWorkforceMonthly``（粒度：月 × 部门）。

    **产值分摊假设（必须写清，否则「人均产值」无法解释）**

    制造业的「部门产值」并不是真实可归属的收入：生产部不直接卖门，
    但没有它的工时投入就没有收入。因此本项目采用
    **「按部门工时占比分摊全公司当月产值」**：

        revenue_allocated(dept)      = 公司当月成交额 × 部门工时 ÷ 公司总工时
        gross_profit_allocated(dept) = 公司当月毛利   × 部门工时 ÷ 公司总工时

    这个假设的适用边界（写进指标字典的 caveat）：
    - 它衡量的是「部门占用了多少企业资源」，而不是「部门创造了多少收入」，
      因此**只能**用来回答「人力投在哪、投产比如何」，**不能**用于考核部门业绩；
    - 本项目的 ``LaborHour.order`` 未按订单归集，所以只能用「工时占比」这种
      近似；若将来补齐订单级工时应改为直接归集，因为那才是真实可归因的口径。

    其他口径：
    - ``headcount`` = 月末在职人数（``hire_date <= 月末`` 且 ``leave_date``
      为空或 ≥ 月初）。用月末快照而不是「当月出现过的人」，避免月中入职离职
      把人头虚高；
    - ``labor_cost`` = Σ 在职员工 ``monthly_salary``；
    - ``labor_cost_ratio`` = 人力成本 ÷ 分摊产值，衡量该部门的人力密集度。
    """
    items_by_order, cost_by_item, _item_meta = _load_items()

    company_stats: dict = {}
    for order in _iter_valid_orders().only(
        "id", "order_year", "order_month", "final_amount"
    ):
        period = _period(order)
        if not period:
            continue
        slot = company_stats.setdefault(period, {"revenue": ZERO, "profit": ZERO})
        slot["revenue"] += _d(order.final_amount)
        for item in items_by_order.get(order.id, ()):
            slot["profit"] += _item_profit(item, cost_by_item)

    employees = list(Employee.objects.all())
    employee_by_id = {employee.id: employee for employee in employees}

    hours_by_month_department: dict = defaultdict(Decimal)
    total_hours_by_period: dict = defaultdict(Decimal)
    max_event_date = None
    for employee_id, work_date, hours in LaborHour.objects.values_list(
        "employee_id", "work_date", "hours"
    ):
        employee = employee_by_id.get(employee_id)
        if employee is None or work_date is None:
            continue
        period = "{:04d}-{:02d}".format(work_date.year, work_date.month)
        hours_by_month_department[(period, employee.department)] += _d(hours)
        total_hours_by_period[period] += _d(hours)
        if max_event_date is None or work_date > max_event_date:
            max_event_date = work_date

    periods = sorted(set(company_stats.keys()) | set(total_hours_by_period.keys()))
    headcount_by_month_department: dict = defaultdict(int)
    salary_by_month_department: dict = defaultdict(Decimal)
    for period in periods:
        year, month = int(period[:4]), int(period[5:7])
        month_start = date(year, month, 1)
        month_end = date(year, month, monthrange(year, month)[1])
        for employee in employees:
            hire_date = employee.hire_date
            if hire_date is not None and hire_date > month_end:
                continue
            leave_date = employee.leave_date
            if leave_date is not None and leave_date < month_start:
                continue
            key = (period, employee.department)
            headcount_by_month_department[key] += 1
            salary_by_month_department[key] += _d(employee.monthly_salary)

    rows = []
    for period in periods:
        company = company_stats.get(period, {"revenue": ZERO, "profit": ZERO})
        total_hours = total_hours_by_period.get(period, ZERO)
        departments = sorted(
            {key[1] for key in hours_by_month_department.keys() if key[0] == period}
            | {key[1] for key in headcount_by_month_department.keys() if key[0] == period}
        )
        for department in departments:
            hours = hours_by_month_department.get((period, department), ZERO)
            headcount = headcount_by_month_department.get((period, department), 0)
            labor_cost = salary_by_month_department.get((period, department), ZERO)
            share = _d(safe_div(hours, total_hours, default=ZERO)) if total_hours > 0 else ZERO
            revenue_allocated = company["revenue"] * share
            profit_allocated = company["profit"] * share
            rows.append(
                DwsWorkforceMonthly(
                    period=period,
                    department=department,
                    headcount=headcount,
                    labor_cost=_q2(labor_cost),
                    revenue_allocated=_q2(revenue_allocated),
                    gross_profit_allocated=_q2(profit_allocated),
                    total_hours=_q2(hours),
                    revenue_per_head=_q2(safe_div(revenue_allocated, headcount, default=ZERO)),
                    profit_per_head=_q2(safe_div(profit_allocated, headcount, default=ZERO)),
                    labor_cost_ratio=_ratio(labor_cost, revenue_allocated),
                )
            )

    _truncate_table(DwsWorkforceMonthly)
    if rows:
        DwsWorkforceMonthly._base_manager.bulk_create(rows, batch_size=1000)
    token = _record_version("dws_workforce_monthly", "dws", len(rows), max_event_date)
    logger.info("rebuild_workforce_monthly: rows=%s", len(rows))
    return {"table": "dws_workforce_monthly", "rows": len(rows), "version_token": token}


# ---------------------------------------------------------------------------
# ⑧ 业务员月度业绩
# ---------------------------------------------------------------------------
@transaction.atomic
def rebuild_rep_monthly() -> dict:
    """重建 ``DwsRepMonthly``（粒度：月 × 业务员）。

    口径：
    - 订单类指标均为有效期订单；
    - ``revenue`` = Σ ``final_amount``；``gross_profit`` = Σ 明细毛利；
    - ``collection_amount`` 按**付款日**所在月归集，不按订单月，，
      回款的时间属性必须用付款日，否则 DSO 与 13 周现金流预测会整体失真；
    - ``avg_discount`` = 该月订单折扣率均值；
    - ``on_time_rate`` = 准时订单数 ÷ **有交付结论的订单数**，
      分母排除 ``is_on_time = -1``（未交付/不适用）。
      若把新单混进分母，老业务员的准时率会被自己的在途订单拖低，
      这是准时率口径最常见的翻车点。
    """
    items_by_order, cost_by_item, _item_meta = _load_items()

    stats: dict = {}
    customers_by_slot: dict = defaultdict(set)
    new_customers_by_slot: dict = defaultdict(set)
    delivered_by_slot: dict = defaultdict(int)
    ontime_by_slot: dict = defaultdict(int)
    discounts_by_slot: dict = defaultdict(list)
    max_event_date = None

    orders = _iter_valid_orders().select_related("customer").only(
        "id",
        "order_year",
        "order_month",
        "order_date",
        "sales_rep_id",
        "customer_id",
        "customer__first_order_date",
        "final_amount",
        "discount",
        "is_on_time",
    )
    for order in orders.iterator(chunk_size=1000):
        period = _period(order)
        if not period or order.sales_rep_id is None:
            continue
        key = (period, order.sales_rep_id)
        profit = ZERO
        for item in items_by_order.get(order.id, ()):
            profit += _item_profit(item, cost_by_item)
        slot = stats.setdefault(key, {"order_count": 0, "revenue": ZERO, "gross_profit": ZERO})
        slot["order_count"] += 1
        slot["revenue"] += _d(order.final_amount)
        slot["gross_profit"] += profit

        if order.customer_id is not None:
            customers_by_slot[key].add(order.customer_id)
            first_order_date = getattr(order.customer, "first_order_date", None)
            if (
                first_order_date is not None
                and order.order_date is not None
                and first_order_date == order.order_date
            ):
                new_customers_by_slot[key].add(order.customer_id)
        if order.is_on_time in (0, 1):
            delivered_by_slot[key] += 1
            if order.is_on_time == 1:
                ontime_by_slot[key] += 1
        discounts_by_slot[key].append(_d(order.discount))
        if order.order_date is not None and (
            max_event_date is None or order.order_date > max_event_date
        ):
            max_event_date = order.order_date

    collection_by_slot: dict = defaultdict(Decimal)
    for rep_id, pay_date, amount in Payment.objects.filter(is_refund=False).values_list(
        "order__sales_rep_id", "payment_date", "amount"
    ):
        if rep_id is None or pay_date is None:
            continue
        key = ("{:04d}-{:02d}".format(pay_date.year, pay_date.month), rep_id)
        collection_by_slot[key] += _d(amount)
        if max_event_date is None or pay_date > max_event_date:
            max_event_date = pay_date

    rows = []
    for key in sorted(stats.keys()):
        period, rep_id = key
        slot = stats[key]
        discounts = discounts_by_slot.get(key, [])
        avg_discount = ZERO
        if discounts:
            avg_discount = _q4(sum(discounts) / Decimal(len(discounts)))
        rows.append(
            DwsRepMonthly(
                period=period,
                sales_rep_id=rep_id,
                order_count=slot["order_count"],
                customer_count=len(customers_by_slot.get(key, ())),
                new_customer_count=len(new_customers_by_slot.get(key, ())),
                revenue=_q2(slot["revenue"]),
                gross_profit=_q2(slot["gross_profit"]),
                collection_amount=_q2(collection_by_slot.get(key, ZERO)),
                avg_discount=avg_discount,
                on_time_rate=_ratio(ontime_by_slot.get(key, 0), delivered_by_slot.get(key, 0)),
            )
        )

    _truncate_table(DwsRepMonthly)
    if rows:
        DwsRepMonthly._base_manager.bulk_create(rows, batch_size=1000)
    token = _record_version("dws_rep_monthly", "dws", len(rows), max_event_date)
    logger.info("rebuild_rep_monthly: rows=%s", len(rows))
    return {"table": "dws_rep_monthly", "rows": len(rows), "version_token": token}


# ---------------------------------------------------------------------------
# 全量重建
# ---------------------------------------------------------------------------
REBUILD_STEPS = (
    ("business_monthly", rebuild_business_monthly),
    ("category_monthly", rebuild_category_monthly),
    ("customer_cohort", rebuild_customer_cohort),
    ("repurchase_interval", rebuild_repurchase_interval),
    ("delivery_stage", rebuild_delivery_stage),
    ("receivable_aging", rebuild_receivable_aging),
    ("workforce_monthly", rebuild_workforce_monthly),
    ("rep_monthly", rebuild_rep_monthly),
)


def rebuild_all(tables: list | None = None) -> dict:
    """按顺序重建全部（或指定）DWS 表，返回 ``{步骤名: 统计}``。

    ``tables=["business_monthly"]`` 之类可只重建单表：全量重建约十几秒，
    但排查某个页面口径时「只跑我关心的那张表」能省一大半时间。

    顺序有依赖：``business_monthly`` 必须在前，因为
    ``generators.gen_sales_targets()`` 会优先读它作为目标锚点。
    """
    selected = REBUILD_STEPS
    if tables:
        wanted = set(tables)
        selected = tuple(step for step in REBUILD_STEPS if step[0] in wanted)
        if not selected:
            raise ValueError("没有匹配的重建步骤：{}".format(tables))

    result = {}
    for name, func in selected:
        result[name] = func()
    result["_summary"] = {
        "steps": len(selected),
        "rows": sum(step.get("rows", 0) for step in result.values() if isinstance(step, dict)),
    }
    return result
