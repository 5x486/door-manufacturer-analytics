"""数据质量校验引擎。

一句话定位：**在看板出结论之前，先回答「这些数据配不配被相信」。**

设计要点
--------
1. **规则元数据在 ``rules.py``，实现在这里**。本模块按 ``rule_code`` 把函数
   挂到元数据上（:data:`CHECKS`），因此「页面上展示的规则」与「实际跑的规则」
   不可能脱节，，脱节的数据质量系统比没有数据质量系统更危险。

2. **每条规则函数统一返回 dict**：``checked_rows`` / ``issue_rows`` /
   ``sample_keys`` / ``message`` / ``detail``。
   统一签名有两个好处：新增规则不用改引擎；结果可以直接序列化进
   ``DQResult.detail``（JSON 字段）供页面展开看样本。

3. **失败判定**：``issue_rows > threshold`` 才判 fail/warn。
   ``error`` 级 → ``fail``，``warning`` 级 → ``warn``，临界内 → ``pass``。
   为什么用「行数」而不是「比例」做阈值：行数对业务更直观
   （「有 3 张单子对不上」比「异常率 0.12%」好沟通），
   而且不会因为数据量翻倍而自动放宽标准。

4. **样本只取前 N 条**：``sample_keys`` 最多存 20 个业务键。
   目的是让人能立刻定位到具体单据，而不是把 3 万行异常灌进数据库。

5. **不修改任何数据**。校验引擎只读，，自动修复脏数据会掩盖上游问题，
   让同一个坑反复踩。修复动作留给人工或专门的清洗脚本。

6. **无 error 级失败 → 数据健康**（``DQRun.is_healthy``）。
   允许存在 warning：门厂的真实数据总有几笔抹零、几单亏损接单，
   把这类差异当阻断项会让流程失去可执行性。
"""

from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from apps.core.constants import CATEGORY_LIST
from apps.core.utils import safe_div
from apps.dq.models import DQCheck, DQResult, DQRun
from apps.dq.rules import RULES
from apps.masterdata.models import Customer, Product
from apps.sales.models import (
    AfterSales,
    DeliveryStage,
    Order,
    OrderItem,
    OrderItemCost,
    Payment,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CHECKS",
    "CheckContext",
    "run_all_checks",
    "get_active_checks",
    "build_check_index",
]

# 样本上限：够定位问题，又不会把 DQResult.detail 撑爆
SAMPLE_LIMIT = 20

# 金额容差（元）：真实业务里手工抹零一般不超过 1 分钱
AMOUNT_TOLERANCE = Decimal("0.01")

DEC2 = Decimal("0.01")


def _q2(value) -> Decimal:
    if value is None:
        return Decimal("0")
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(DEC2, rounding=ROUND_HALF_UP)


def _as_of_date():
    """数据截止日：优先用 settings.DATA_AS_OF_DATE，其次解析 DATA_AS_OF 字符串。"""
    from datetime import date

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


# ---------------------------------------------------------------------------
# 上下文
# ---------------------------------------------------------------------------
class CheckContext:
    """一次校验运行共享的上下文（数据截止日、样本上限、预取集合）。

    为什么要有上下文对象而不是给每个函数传一堆参数：
    后续新增「需要跨表引用的规则」（例如「售后责任人与安装师傅一致性」）时，
    不必再改所有函数的签名，，引擎与规则之间的契约保持稳定。
    """

    def __init__(self, data_as_of=None, sample_limit: int = SAMPLE_LIMIT):
        self.data_as_of = data_as_of or _as_of_date()
        self.sample_limit = sample_limit
        self._category_list = None
        self._valid_order_ids = None

    @property
    def category_list(self) -> list:
        """品类值域（延迟加载，避免每条规则都查一次配置）。"""
        if self._category_list is None:
            self._category_list = list(CATEGORY_LIST) if CATEGORY_LIST else []
        return self._category_list

    @property
    def valid_order_ids(self) -> set:
        """有效期订单主键集合（延迟加载）。"""
        if self._valid_order_ids is None:
            self._valid_order_ids = set(
                Order.objects.exclude(order_status="已取消").values_list("id", flat=True)
            )
        return self._valid_order_ids


def _result(checked_rows, issue_rows, sample_keys, message, detail=None):
    """统一构造规则返回值。"""
    return {
        "checked_rows": int(checked_rows or 0),
        "issue_rows": int(issue_rows or 0),
        "sample_keys": [str(key) for key in list(sample_keys or [])][:SAMPLE_LIMIT],
        "message": message,
        "detail": detail or {},
    }


# ---------------------------------------------------------------------------
# 通用检查实现
# ---------------------------------------------------------------------------
def _check_duplicate_key(model, key_field, label):
    """主键唯一性的通用实现。

    用 ``values(key).annotate(count).filter(count__gt=1)`` 一次查出所有重复键，
    比逐行比更省事，也比 ``distinct()`` 更明确地表达「重复」语义。
    """
    duplicates = (
        model.objects.values(key_field)
        .annotate(cnt=Count("id"))
        .filter(cnt__gt=1)
        .order_by("-cnt")
    )
    total_rows = model.objects.count()
    issue_rows = duplicates.count()
    samples = [
        "{}×{}".format(row[key_field], row["cnt"])
        for row in duplicates[:SAMPLE_LIMIT]
    ]
    return _result(
        total_rows,
        issue_rows,
        samples,
        "{}共 {} 行，重复 {} 行".format(label, total_rows, issue_rows),
        detail={"duplicate_keys": samples[:10]},
    )


def _check_null_foreign_key(model, fk_field, label, key_field):
    """外键悬空的通用实现。

    ``fk__isnull=True`` 覆盖两种情况：外键列为 NULL，以及外键指向了
    不存在的父行（Django 在 real DB 上有约束时不会出现后者，但 CSV 直接导入
    或用 ``raw=True`` 保存时会出现）。因此这里同时查 NULL。
    """
    total_rows = model.objects.count()
    broken = model.objects.filter(**{"{}__isnull".format(fk_field): True})
    issue_rows = broken.count()
    samples = [str(value) for value in broken.values_list(key_field, flat=True)[:SAMPLE_LIMIT]]
    return _result(
        total_rows,
        issue_rows,
        samples,
        "{}共 {} 行，外键缺失 {} 行".format(label, total_rows, issue_rows),
        detail={"missing_{}".format(fk_field): samples[:10]},
    )


# ---------------------------------------------------------------------------
# 各规则实现
# ---------------------------------------------------------------------------
def check_pk_orders_no(ctx):
    """订单号唯一。"""
    return _check_duplicate_key(Order, "order_no", "订单")


def check_pk_items_key(ctx):
    """订单明细唯一（业务键 item_no）。"""
    return _check_duplicate_key(OrderItem, "item_no", "订单明细")


def check_pk_payments_no(ctx):
    """付款流水号唯一。"""
    return _check_duplicate_key(Payment, "payment_no", "付款流水")


def check_pk_customers_code(ctx):
    """客户编码唯一。"""
    return _check_duplicate_key(Customer, "customer_code", "客户")


def check_pk_products_code(ctx):
    """产品编码唯一。"""
    return _check_duplicate_key(Product, "product_code", "产品")


def check_fk_order_customer(ctx):
    """订单 → 客户外键完整。"""
    return _check_null_foreign_key(Order, "customer", "订单", "order_no")


def check_fk_item_order(ctx):
    """明细 → 订单外键完整。"""
    return _check_null_foreign_key(OrderItem, "order", "订单明细", "item_no")


def check_fk_item_product(ctx):
    """明细 → 产品外键完整。"""
    return _check_null_foreign_key(OrderItem, "product", "订单明细", "item_no")


def check_fk_payment_order(ctx):
    """付款 → 订单外键完整。"""
    return _check_null_foreign_key(Payment, "order", "付款流水", "payment_no")


def check_fk_after_order(ctx):
    """售后 → 订单外键完整。"""
    return _check_null_foreign_key(AfterSales, "order", "售后工单", "after_no")


def check_amount_final_formula(ctx):
    """订单终价 = 总额 × (1 - 让利) + 运费。

    用 ``F`` 表达式把比较放在数据库里做，避免把 2400 行拉到 Python 里逐行比。
    容差 0.01 元：真实业务中的手工抹零可解释，不算数据问题。

    注意必须用 ``ExpressionWrapper(output_field=DecimalField)`` 包一层：
    未指定输出类型时 Django 会让表达式在 ``FloatField`` 上求值，
    浮点误差会造出大量 1e-13 级别的「伪差异」，让这条规则永远告警。
    """
    from django.db.models import DecimalField, ExpressionWrapper

    total_rows = Order.objects.count()
    expected_expr = ExpressionWrapper(
        F("total_amount") * (Decimal("1") - F("discount")) + F("freight"),
        output_field=DecimalField(max_digits=18, decimal_places=4),
    )
    diff_expr = ExpressionWrapper(
        F("final_amount") - expected_expr,
        output_field=DecimalField(max_digits=18, decimal_places=4),
    )
    queryset = (
        Order.objects.filter(total_amount__isnull=False, final_amount__isnull=False)
        .annotate(_diff=diff_expr)
        .filter(Q(_diff__gt=AMOUNT_TOLERANCE) | Q(_diff__lt=-AMOUNT_TOLERANCE))
    )
    issue_rows = queryset.count()
    samples = list(
        queryset.values_list("order_no", "final_amount", "total_amount", "discount", "freight")[
            :SAMPLE_LIMIT
        ]
    )
    return _result(
        total_rows,
        issue_rows,
        [row[0] for row in samples],
        "订单 {} 行，终价公式不符 {} 行（容差 {} 元）".format(total_rows, issue_rows, AMOUNT_TOLERANCE),
        detail={
            "samples": [
                {
                    "order_no": row[0],
                    "final_amount": str(row[1]),
                    "total_amount": str(row[2]),
                    "discount": str(row[3]),
                    "freight": str(row[4]),
                }
                for row in samples[:10]
            ]
        },
    )


def check_amount_item_sum(ctx):
    """明细合计 = 订单总额。

    逐订单比较而非全表求和：全表求和相等可能是两侧错误互相抵消的结果，
    逐单比较才能真正发现漏行。
    """
    groups = (
        Order.objects.exclude(order_status="已取消")
        .values("order_no", "total_amount")
        .annotate(item_sum=Sum("items__line_amount"))
        .filter(item_sum__isnull=False)
    )
    total_rows = Order.objects.count()
    samples = []
    issue_rows = 0
    for row in groups.iterator(chunk_size=500):
        expected = _q2(row["total_amount"])
        actual = _q2(row["item_sum"])
        if abs(expected - actual) > AMOUNT_TOLERANCE:
            issue_rows += 1
            if len(samples) < SAMPLE_LIMIT:
                samples.append(
                    "{}: 订单{} / 明细{}".format(row["order_no"], expected, actual)
                )
    return _result(
        total_rows,
        issue_rows,
        samples,
        "校验 {} 行订单，明细合计与订单总额不符 {} 行".format(total_rows, issue_rows),
        detail={"samples": samples[:10]},
    )


def check_amount_cost_positive(ctx):
    """成本金额非负（含订单行成本）。"""
    cost_rows = OrderItemCost.objects.count()
    negative_costs = OrderItemCost.objects.filter(amount__lt=0)
    negative_items = OrderItem.objects.filter(
        Q(total_cost__lt=0) | Q(gross_profit__gt=F("line_amount"))
    )
    issue_rows = negative_costs.count() + negative_items.count()
    samples = ["cost:{}".format(value) for value in negative_costs.values_list("id", flat=True)[:10]]
    samples += [
        "item:{}".format(value) for value in negative_items.values_list("item_no", flat=True)[:10]
    ]
    return _result(
        cost_rows,
        issue_rows,
        samples,
        "成本明细 {} 行，异常 {} 行（负成本或毛利超过行金额）".format(cost_rows, issue_rows),
        detail={"negative_cost_rows": issue_rows},
    )


def check_amount_payment_positive(ctx):
    """付款金额方向规范：非退款为正、退款不为正。"""
    total_rows = Payment.objects.count()
    broken = Payment.objects.filter(
        Q(is_refund=False, amount__lte=0) | Q(is_refund=True, amount__gt=0)
    )
    issue_rows = broken.count()
    samples = list(broken.values_list("payment_no", "stage", "amount", "is_refund")[:SAMPLE_LIMIT])
    return _result(
        total_rows,
        issue_rows,
        [row[0] for row in samples],
        "付款 {} 行，方向异常 {} 行".format(total_rows, issue_rows),
        detail={
            "samples": [
                {"payment_no": row[0], "stage": row[1], "amount": str(row[2]), "is_refund": row[3]}
                for row in samples[:10]
            ]
        },
    )


def check_date_delivery_after_order(ctx):
    """实际交付日不早于下单日。"""
    total_rows = Order.objects.filter(actual_delivery_date__isnull=False).count()
    broken = Order.objects.filter(
        actual_delivery_date__isnull=False,
        actual_delivery_date__lt=F("order_date"),
    )
    issue_rows = broken.count()
    samples = list(broken.values_list("order_no", "order_date", "actual_delivery_date")[:SAMPLE_LIMIT])
    return _result(
        total_rows,
        issue_rows,
        [row[0] for row in samples],
        "已交付订单 {} 行，交付早于下单 {} 行".format(total_rows, issue_rows),
        detail={
            "samples": [
                {"order_no": row[0], "order_date": str(row[1]), "delivered": str(row[2])}
                for row in samples[:10]
            ]
        },
    )


def check_date_payment_after_order(ctx):
    """付款日不早于下单日。"""
    total_rows = Payment.objects.filter(payment_date__isnull=False).count()
    broken = Payment.objects.filter(
        payment_date__isnull=False,
        order__order_date__isnull=False,
        payment_date__lt=F("order__order_date"),
    )
    issue_rows = broken.count()
    samples = list(
        broken.values_list("payment_no", "payment_date", "order__order_date")[:SAMPLE_LIMIT]
    )
    return _result(
        total_rows,
        issue_rows,
        [row[0] for row in samples],
        "付款 {} 行，付款早于下单 {} 行".format(total_rows, issue_rows),
        detail={
            "samples": [
                {"payment_no": row[0], "payment_date": str(row[1]), "order_date": str(row[2])}
                for row in samples[:10]
            ]
        },
    )


def check_date_no_future(ctx):
    """不得存在晚于数据截止日的业务日期。"""
    as_of = ctx.data_as_of
    total_rows = Order.objects.count()
    broken = Order.objects.filter(
        Q(order_date__gt=as_of) | Q(actual_delivery_date__gt=as_of)
    )
    broken_payments = Payment.objects.filter(payment_date__gt=as_of)
    issue_rows = broken.count() + broken_payments.count()
    samples = [
        "{}({})".format(row[0], row[1])
        for row in broken.values_list("order_no", "order_date")[:10]
    ]
    samples += [
        "{}({})".format(row[0], row[1])
        for row in broken_payments.values_list("payment_no", "payment_date")[:10]
    ]
    return _result(
        total_rows + Payment.objects.count(),
        issue_rows,
        samples,
        "数据截止 {}，超出截止日的记录 {} 行".format(as_of, issue_rows),
        detail={"data_as_of": str(as_of), "future_rows": issue_rows},
    )


def check_date_stage_sequence(ctx):
    """同一订单的交付阶段实际日期应随阶段序号递增。"""
    stages: dict = {}
    total_rows = 0
    for order_id, stage_order, actual_date, stage in DeliveryStage.objects.values_list(
        "order_id", "stage_order", "actual_date", "stage"
    ):
        total_rows += 1
        stages.setdefault(order_id, []).append((stage_order, actual_date, stage))

    issue_rows = 0
    samples = []
    for order_id, rows in stages.items():
        rows.sort(key=lambda item: item[0])
        previous_date = None
        previous_stage = None
        for _order, actual_date, stage in rows:
            if previous_date is not None and actual_date is not None and actual_date < previous_date:
                issue_rows += 1
                if len(samples) < SAMPLE_LIMIT:
                    samples.append(
                        "order#{} {} → {}".format(order_id, previous_stage, stage)
                    )
                break
            if actual_date is not None:
                previous_date = actual_date
                previous_stage = stage
    return _result(
        total_rows,
        issue_rows,
        samples,
        "阶段记录 {} 行，日期倒挂订单 {} 单".format(total_rows, issue_rows),
        detail={"inverted_orders": issue_rows},
    )


def check_biz_cancelled_no_delivery(ctx):
    """已取消订单不得有实际交付日或准时标记。"""
    cancelled = Order.objects.filter(order_status="已取消")
    total_rows = cancelled.count()
    broken = cancelled.filter(
        Q(actual_delivery_date__isnull=False) | ~Q(is_on_time=-1)
    )
    issue_rows = broken.count()
    samples = list(broken.values_list("order_no", "order_status", "is_on_time")[:SAMPLE_LIMIT])
    return _result(
        total_rows,
        issue_rows,
        [row[0] for row in samples],
        "取消订单 {} 行，带交付结论 {} 行".format(total_rows, issue_rows),
        detail={"samples": [{"order_no": row[0], "status": row[1], "on_time": row[2]} for row in samples[:10]]},
    )


def check_biz_valid_order_has_item(ctx):
    """有效订单必须有明细。"""
    valid_queryset = Order.objects.exclude(order_status="已取消")
    orders_without_items = valid_queryset.annotate(item_rows=Count("items")).filter(item_rows=0)
    total_rows = valid_queryset.count()
    issue_rows = orders_without_items.count()
    samples = [
        str(value) for value in orders_without_items.values_list("order_no", flat=True)[:SAMPLE_LIMIT]
    ]
    return _result(
        total_rows,
        issue_rows,
        samples,
        "有效订单 {} 行，零明细 {} 行".format(total_rows, issue_rows),
        detail={"orders_without_items": samples[:10]},
    )


def check_biz_on_time_rate(ctx):
    """准时交付率区间校验。

    这条规则的「异常行」不是某几行数据，而是整个比率越界，
    因此 ``issue_rows`` 用 1/0 表达「越界 / 未越界」，
    ``detail`` 里放实际比率供页面直接展示。分母排除 ``is_on_time = -1``
    （未交付/不适用），否则在途订单会把准时率拉低。
    """
    queryset = Order.objects.filter(is_on_time__in=(0, 1))
    total_rows = queryset.count()
    on_time_rows = queryset.filter(is_on_time=1).count()
    rate = safe_div(on_time_rows, total_rows)
    message = "有效交付样本 {} 行，准时率 {}".format(total_rows, rate)
    issue_rows = 0
    details = {"on_time_rate": None, "sample_rows": total_rows}
    if rate is None:
        message += "（样本不足，跳过判定）"
    else:
        rate_float = float(rate)
        details["on_time_rate"] = round(rate_float, 4)
        if rate_float < 0 or rate_float > 1:
            issue_rows = 1
            message += "（越界）"
        elif total_rows >= 30 and rate_float < 0.60:
            # 低于 60% 不是数据错误，而是经营红灯：规则告警本身就是给管理层的信号
            issue_rows = 1
            message += "（低于经营预警线 60%）"
    return _result(total_rows, issue_rows, [], message, detail=details)


def check_biz_gross_margin_range(ctx):
    """单行毛利率落在 -20% ~ 60%。"""
    total_rows = OrderItem.objects.filter(line_amount__gt=0).count()
    queryset = OrderItem.objects.filter(line_amount__gt=0, total_cost__isnull=False).annotate(
        margin=F("gross_profit") / F("line_amount")
    )
    broken = queryset.filter(Q(margin__lt=Decimal("-0.20")) | Q(margin__gt=Decimal("0.60")))
    issue_rows = broken.count()
    samples = list(broken.values_list("item_no", "line_amount", "gross_profit")[:SAMPLE_LIMIT])
    return _result(
        total_rows,
        issue_rows,
        [row[0] for row in samples],
        "校验 {} 行明细，毛利率越界 {} 行".format(total_rows, issue_rows),
        detail={
            "samples": [
                {"item_no": row[0], "line_amount": str(row[1]), "gross_profit": str(row[2])}
                for row in samples[:10]
            ]
        },
    )


def check_biz_after_sales_rate(ctx):
    """售后工单率 < 15%。"""
    valid_orders = Order.objects.exclude(order_status="已取消").count()
    after_rows = AfterSales.objects.count()
    rate = safe_div(after_rows, valid_orders)
    issue_rows = 0
    message = "售后 {} 单 / 有效订单 {} 单".format(after_rows, valid_orders)
    details = {"after_sales_rate": None}
    if rate is None:
        message += "（无有效订单，跳过判定）"
    else:
        rate_float = float(rate)
        details["after_sales_rate"] = round(rate_float, 4)
        if rate_float >= 0.15:
            issue_rows = 1
            message += "，售后率 {:.2%} 超过 15% 阈值".format(rate_float)
        else:
            message += "，售后率 {:.2%}".format(rate_float)
    return _result(valid_orders, issue_rows, [], message, detail=details)


def check_biz_after_sales_category(ctx):
    """售后品类值域合法（捕捉「列名 product_id 实际存品类名」这类语义漂移）。"""
    total_rows = AfterSales.objects.count()
    allowed = ctx.category_list
    if not allowed:
        return _result(
            total_rows,
            0,
            [],
            "未定义品类枚举，跳过值域校验",
            detail={"skipped": True},
        )
    broken = AfterSales.objects.exclude(category__in=allowed)
    issue_rows = broken.count()
    samples = ["{}: {}".format(row[0], row[1]) for row in broken.values_list("after_no", "category")[:SAMPLE_LIMIT]]
    return _result(
        total_rows,
        issue_rows,
        samples,
        "售后 {} 行，品类越界 {} 行".format(total_rows, issue_rows),
        detail={"allowed_categories": allowed, "samples": samples[:10]},
    )


def check_biz_quantity_positive(ctx):
    """明细数量 ≥ 1。"""
    total_rows = OrderItem.objects.count()
    broken = OrderItem.objects.filter(Q(quantity__isnull=True) | Q(quantity__lt=1))
    issue_rows = broken.count()
    samples = list(broken.values_list("item_no", "quantity")[:SAMPLE_LIMIT])
    return _result(
        total_rows,
        issue_rows,
        [str(row[0]) for row in samples],
        "明细 {} 行，数量异常 {} 行".format(total_rows, issue_rows),
        detail={"samples": [{"item_no": row[0], "quantity": row[1]} for row in samples[:10]]},
    )


# ---------------------------------------------------------------------------
# 规则注册表
# ---------------------------------------------------------------------------
# rule_code → 实现函数。键必须与 rules.RULES 里的 rule_code 完全一致，
# 模块导入时会做一次一致性校验（见文件末尾 _validate_registry）。
_CHECK_FUNCS = {
    "PK_ORDERS_NO": check_pk_orders_no,
    "PK_ITEMS_KEY": check_pk_items_key,
    "PK_PAYMENTS_NO": check_pk_payments_no,
    "PK_CUSTOMERS_CODE": check_pk_customers_code,
    "PK_PRODUCTS_CODE": check_pk_products_code,
    "FK_ORDER_CUSTOMER": check_fk_order_customer,
    "FK_ITEM_ORDER": check_fk_item_order,
    "FK_ITEM_PRODUCT": check_fk_item_product,
    "FK_PAYMENT_ORDER": check_fk_payment_order,
    "FK_AFTER_ORDER": check_fk_after_order,
    "AMT_FINAL_FORMULA": check_amount_final_formula,
    "AMT_ITEM_SUM": check_amount_item_sum,
    "AMT_COST_POSITIVE": check_amount_cost_positive,
    "AMT_PAYMENT_POSITIVE": check_amount_payment_positive,
    "DATE_DELIVERY_AFTER_ORDER": check_date_delivery_after_order,
    "DATE_PAYMENT_AFTER_ORDER": check_date_payment_after_order,
    "DATE_NO_FUTURE": check_date_no_future,
    "DATE_STAGE_SEQUENCE": check_date_stage_sequence,
    "BIZ_CANCELLED_NO_DELIVERY": check_biz_cancelled_no_delivery,
    "BIZ_VALID_ORDER_HAS_ITEM": check_biz_valid_order_has_item,
    "BIZ_ON_TIME_RATE": check_biz_on_time_rate,
    "BIZ_GROSS_MARGIN_RANGE": check_biz_gross_margin_range,
    "BIZ_AFTER_SALES_RATE": check_biz_after_sales_rate,
    "BIZ_AFTER_SALES_CATEGORY": check_biz_after_sales_category,
    "BIZ_QUANTITY_POSITIVE": check_biz_quantity_positive,
}


def _build_checks() -> list:
    """把元数据与实现合并成 :data:`CHECKS`。"""
    merged = []
    for index, meta in enumerate(RULES):
        func = _CHECK_FUNCS.get(meta["rule_code"])
        if func is None:
            # 编译期就暴露「规则没实现」，而不是运行到那一行才发现
            raise RuntimeError(
                "规则 {} 没有对应实现函数，请检查 _CHECK_FUNCS".format(meta["rule_code"])
            )
        item = dict(meta)
        item["func"] = func
        item["order"] = index
        merged.append(item)
    return merged


# 规则清单：每条含 rule_code / rule_name / category / target_table /
# description / severity / threshold / func
CHECKS = _build_checks()


def build_check_index() -> dict:
    """``{rule_code: 规则 dict}``，便于按 code 单测或单独重跑某条规则。"""
    return {check["rule_code"]: check for check in CHECKS}


def get_active_checks(category: str | None = None, rule_codes: list | None = None) -> list:
    """取要执行的规则子集。

    支持 ``category='金额对账'`` 或 ``rule_codes=['AMT_FINAL_FORMULA']``，
    用于「只跑某一类规则做调参」的场景，，全量 25 条规则在 MySQL 上约 2~5 秒，
    但调一条规则的阈值时没人愿意每次等全量。
    """
    selected = CHECKS
    if category:
        selected = [check for check in selected if check["category"] == category]
    if rule_codes:
        wanted = set(rule_codes)
        selected = [check for check in selected if check["rule_code"] in wanted]
    return list(selected)


# ---------------------------------------------------------------------------
# 运行入口
# ---------------------------------------------------------------------------
def _evaluate_status(severity: str, issue_rows: int, threshold: int) -> str:
    """判定单条规则状态：``pass`` / ``warn`` / ``fail``。

    ``issue_rows > threshold`` 才算异常；``error`` 级对应 ``fail``，
    ``warning`` 级对应 ``warn``。
    """
    if issue_rows <= threshold:
        return "pass"
    return "fail" if severity == "error" else "warn"


def _sample_ratio(issue_rows: int, checked_rows: int) -> Decimal:
    """异常率：分母为 0 时记 0（而不是空），便于 DQ 页面直接排序。"""
    ratio = safe_div(issue_rows, checked_rows)
    if ratio is None:
        return Decimal("0")
    return ratio.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _sync_check_master(checks: list) -> int:
    """把规则元数据同步进 ``DQCheck`` 表，返回同步条数。

    每次运行都同步一次而不是只在管理命令里同步：这样新增规则首次运行时
    就会自动出现在质量页面上，不需要任何人记得先去跑一条命令。
    """
    synced = 0
    for check in checks:
        DQCheck.objects.update_or_create(
            rule_code=check["rule_code"],
            defaults={
                "rule_name": check["rule_name"],
                "category": check["category"],
                "target_table": check["target_table"],
                "description": check["description"],
                "sql_text": check["sql_text"],
                "severity": check["severity"],
                "threshold": check["threshold"],
                "is_active": True,
                "order": check.get("order", 0),
            },
        )
        synced += 1
    return synced


def run_all_checks(
    trigger: str = "manual",
    category: str | None = None,
    rule_codes: list | None = None,
    sync_master: bool = True,
) -> DQRun:
    """执行规则并落库，返回 :class:`apps.dq.models.DQRun`。

    ``trigger``：``manual``（人手点） / ``import``（导入后自动） /
    ``scheduled``（定时跑批）。区分触发来源是为了排查「这条告警是谁引入的」，，
    导入后自动跑出的失败，责任在上游文件；定时跑的失败，责任在当天的数据。

    返回的 ``DQRun`` 上挂 ``.results``（DQResult 列表）与 ``.summary``，
    供命令直接打印，不必再查一次库。
    """
    checks = get_active_checks(category=category, rule_codes=rule_codes)
    if not checks:
        raise ValueError("没有匹配的校验规则：category={} rule_codes={}".format(category, rule_codes))

    started_at = timezone.now()
    ctx = CheckContext()

    if sync_master:
        try:
            _sync_check_master(checks)
        except Exception as exc:  # pragma: no cover - 同步失败不应阻断校验本身
            logger.warning("同步 DQCheck 主数据失败（不阻断校验）：%s", exc)

    # 逐条兜底 get_or_create：即使 sync_master=False 或同步过程出错，
    # DQResult.rule 也不会是 NULL——结果行挂不上规则，这条告警就成了死信。
    existing_checks = {}
    for check in checks:
        try:
            row, _created = DQCheck.objects.get_or_create(
                rule_code=check["rule_code"],
                defaults={
                    "rule_name": check["rule_name"],
                    "category": check["category"],
                    "target_table": check["target_table"],
                    "description": check["description"],
                    "sql_text": check["sql_text"],
                    "severity": check["severity"],
                    "threshold": check["threshold"],
                    "is_active": True,
                    "order": check.get("order", 0),
                },
            )
            existing_checks[check["rule_code"]] = row
        except Exception as exc:  # pragma: no cover
            logger.warning("规则 %s 主数据不可用：%s", check["rule_code"], exc)
            existing_checks[check["rule_code"]] = None

    payload = []  # (规则, 结果 dict, 状态)
    passed = warned = failed = 0
    has_error_failure = False

    for check in checks:
        try:
            outcome = check["func"](ctx)
        except Exception as exc:  # 单条规则异常不应让整次校验失败
            logger.exception("规则 %s 执行失败", check["rule_code"])
            outcome = _result(
                0,
                0,
                [],
                "规则执行异常：{}: {}".format(type(exc).__name__, exc),
                detail={"error": repr(exc)},
            )
            # 规则自身跑不起来，按 warning 处理并保留异常信息：
            # 直接判 fail 会淹没真实的数据问题，判 pass 又会掩盖引擎 bug。
            status = "warn"
            payload.append((check, outcome, status))
            warned += 1
            continue

        status = _evaluate_status(
            check["severity"], outcome["issue_rows"], int(check["threshold"] or 0)
        )
        payload.append((check, outcome, status))
        if status == "fail":
            failed += 1
            has_error_failure = True
        elif status == "warn":
            warned += 1
        else:
            passed += 1

    duration_ms = int((timezone.now() - started_at).total_seconds() * 1000)

    with transaction.atomic():
        run = DQRun.objects.create(
            trigger=trigger,
            data_as_of=ctx.data_as_of,
            total_checks=len(checks),
            passed=passed,
            warned=warned,
            failed=failed,
            duration_ms=duration_ms,
            # 无 error 级失败即视为数据健康：warning 级差异（手工抹零、
            # 个别亏损接单）在门厂是常态，不该阻断业务系统使用数据。
            is_healthy=not has_error_failure,
        )
        results = []
        for check, outcome, status in payload:
            results.append(
                DQResult(
                    run=run,
                    rule=existing_checks.get(check["rule_code"]),
                    status=status,
                    checked_rows=outcome["checked_rows"],
                    issue_rows=outcome["issue_rows"],
                    issue_ratio=_sample_ratio(outcome["issue_rows"], outcome["checked_rows"]),
                    message=outcome["message"],
                    sample_keys=outcome["sample_keys"],
                    detail=outcome["detail"],
                )
            )
        if results:
            DQResult.objects.bulk_create(results, batch_size=200)

    # 注意：不能直接对 Django 的反向关联赋值（run.results = [...]），
    # 关系描述符会抛 TypeError，要求改用 results.set()。这里挂私有属性即可，
    # 只用于命令打印，不参与持久化，语义清晰且不触发额外 SQL。
    run.result_rows = results
    run.summary = {
        "total": len(checks),
        "passed": passed,
        "warned": warned,
        "failed": failed,
        "is_healthy": run.is_healthy,
        "duration_ms": duration_ms,
        "data_as_of": str(ctx.data_as_of),
        "trigger": trigger,
    }
    logger.info(
        "run_all_checks: trigger=%s total=%s pass=%s warn=%s fail=%s healthy=%s %sms",
        trigger,
        len(checks),
        passed,
        warned,
        failed,
        run.is_healthy,
        duration_ms,
    )
    return run


# 模块自检：规则元数据与实现必须一一对应（导入即报错，不留到运行时）
if set(_CHECK_FUNCS) != {rule["rule_code"] for rule in RULES}:  # pragma: no cover
    _missing = {rule["rule_code"] for rule in RULES} - set(_CHECK_FUNCS)
    _extra = set(_CHECK_FUNCS) - {rule["rule_code"] for rule in RULES}
    raise RuntimeError("DQ 规则注册不一致：缺少实现 {}，多余实现 {}".format(_missing, _extra))
