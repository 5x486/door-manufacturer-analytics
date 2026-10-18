"""只读查询封装（selectors）。

定位：**视图与 API 唯一允许取数的地方**。
视图负责分页、权限、渲染；取数与口径计算全部落在这里，原因有三：

1. **一处口径**：同一个「准时交付率」在页面、导出、接口三处若各写一遍，
   迟早会出现三个数字（而且没人知道哪个对）；
2. **可缓存**：函数入参只有 :class:`AnalyticsFilters` 与少量标量，
   天然适合按 ``filters.cache_key()`` 缓存；
3. **可测试**：没有 request、没有模板，直接构造 filters 就能断言数字。

DWS 优先原则
------------
每个函数都**优先查 ``dws_*`` 预聚合表**，仅在两种情况下回退 fact 表现算：
- 预聚合表还没构建（``DataVersion`` 里没有该表，或表为空）；
- 筛选条件超出了预聚合表的维度（例如按「客户类型 / 订单类型 / 门店」筛选，
  而 ``DwsBusinessMonthly`` 只有期间 × 渠道 × 品类三个维度）。
回退时函数内部逻辑会显式走另一条分支，并在返回值里带 ``source`` 字段，
**绝不静默混用两种口径**，，静默混用是分析系统最危险的失效模式。

返回值约定（重要）
------------------
- **金额**：一律返回 ``Decimal``（与库表字段一致）。需要进 JSON / 图表时由
  调用方转 float，转换点唯一，避免 float 误差在多层聚合里累积。
- **比率**：一律返回 ``float``，取值 ``0~1``。**全项目统一**，
  展示时再乘 100。只有 ``None`` 表示「无法计算」（分母为 0）。
- **计数**：``int``。
- 明细类函数返回 ``list[dict]``，聚合类函数返回 ``dict``。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db.models import Avg, Count, F, Q, Sum

from apps.analytics.models import (
    DwsBusinessMonthly,
    DwsCategoryMonthly,
    DwsCustomerCohort,
    DwsDeliveryStage,
    DwsReceivableAging,
    DwsRepMonthly,
    DwsRepurchaseInterval,
    DwsWorkforceMonthly,
    ModelVersion,
    ScoringResult,
)
# 这里刻意**不用** only()。原因：only() 会把未列出的外键也 defer 掉，而后续一旦有 select_related 需要跟随这些外键，Django 在编译查询时就抛
# FieldError: cannot be both deferred and traversed using select_related。
# 这个错误跟有没有数据无关，只在真实请求或测试首次编译查询时爆出来，排查成本很高。
# 本项目明细量在万行级，ORM 多取几个字段的代价远低于一类隐蔽 bug 的风险。
from apps.analytics.services.filters import AnalyticsFilters
from apps.core.constants import AGING_BUCKET_LIST, CHANNEL_ACQUISITION_COST, SEGMENT_VALUES
from apps.core.utils import safe_div
from apps.dq.models import DQResult, DQRun
from apps.masterdata.models import Customer, SalesRep
from apps.sales.models import (
    AfterSales,
    DeliveryStage,
    Order,
    OrderItem,
    OrderItemCost,
    Payment,
    PaymentTerm,
)

logger = logging.getLogger(__name__)

__all__ = [
    "get_valid_orders",
    "get_done_orders",
    "get_items",
    "get_kpi_summary",
    "get_monthly_trend",
    "get_yearly_summary",
    "get_channel_performance",
    "get_category_structure",
    "get_customer_segments",
    "get_cohort_matrix",
    "get_churn_risk_list",
    "get_delivery_performance",
    "get_stage_bottleneck",
    "get_after_sales_analysis",
    "get_receivables",
    "get_cashflow_weekly",
    "get_rep_ranking",
    "get_store_comparison",
    "get_region_distribution",
    "get_profit_by_sku",
    "get_profit_quadrant",
    "get_discount_margin_relation",
    "get_customer_360",
    "get_data_quality_summary",
    "get_model_overview",
]

ZERO = Decimal("0")
DEC2 = Decimal("0.01")

# 账龄分桶的唯一口径：库里的值是中文标签（0-30天 / 31-60天 / 61-90天 / 90天以上），
# 见 core.constants.AgingBucketChoices。API 对外仍用 0_30 / 31_60 这类编码，
# 进选择器前统一映射成标签，避免「页面按标签写、接口按编码查」两套键各查一半。
AGING_BUCKET_ORDER = list(AGING_BUCKET_LIST)
AGING_BUCKET_OVERDUE = tuple(AGING_BUCKET_LIST[1:])
AGING_CODE_TO_LABEL = {
    "0_30": AGING_BUCKET_ORDER[0],
    "31_60": AGING_BUCKET_ORDER[1],
    "61_90": AGING_BUCKET_ORDER[2],
    "over_90": AGING_BUCKET_ORDER[3],
}


def normalize_aging_bucket(value):
    """把 API 传来的编码（over_90）或标签（90天以上）统一成标签。"""
    if not value:
        return None
    return AGING_CODE_TO_LABEL.get(str(value).strip(), str(value).strip())

# 预聚合表缺维度时无法覆盖的筛选条件：出现任意一个就回退 fact 表现算
UNSUPPORTED_DWS_FILTERS = (
    "customer_types",
    "order_types",
    "stores",
    "segments",
    "date_from",
    "date_to",
)

# 周期比较支持的最大天数窗口（用于自定义区间求「上一期」）
MAX_PERIOD_DAYS = 400

# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _q2(value) -> Decimal:
    """金额规整到分。"""
    if value is None:
        return ZERO
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(DEC2, rounding=ROUND_HALF_UP)

def _d(value) -> Decimal:
    """任意数值 → Decimal（None → 0）。"""
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))

def _ratio(numerator, denominator):
    """比率：分母为 0 / 缺失返回 ``None``（表示「算不出来」）。

    与 ETL 层不同：DWS 是物理存储必须有值，而 selector 是展示层，
    ``None`` 在模板里会渲染成「，」，比 0 更诚实，，0% 会被误读为
    「本期为 0」，而实际可能只是没有样本。
    """
    value = safe_div(numerator, denominator)
    if value is None:
        return None
    return float(value)

def _as_of_date() -> date:
    """数据截止日。"""
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

def _month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)

def _shift_month(period: str, delta: int) -> str:
    """``"2025-03"`` 平移月份，如 ``delta=-12`` 得 ``"2024-03"``。"""
    if not period or "-" not in period:
        return ""
    year, month = int(period[:4]), int(period[5:7])
    total = year * 12 + (month - 1) + delta
    new_year, new_month = divmod(total, 12)
    return "{:04d}-{:02d}".format(new_year, new_month + 1)

def _has_dws_rows(model) -> bool:
    """预聚合表是否有数据（用 ``exists()`` 而不是 ``count()``，便宜得多）。"""
    try:
        return model._base_manager.exists()
    except Exception:  # pragma: no cover - 表不存在时回退
        return False

def _dws_usable(filters: AnalyticsFilters) -> bool:
    """判断当前筛选条件能否完全用 DWS 表表达。"""
    if any(getattr(filters, name) for name in UNSUPPORTED_DWS_FILTERS):
        return False
    return _has_dws_rows(DwsBusinessMonthly)

def _period_filter(filters: AnalyticsFilters) -> Q | None:
    """把筛选条件翻译成 ``DwsBusinessMonthly.period`` 上的过滤条件。

    返回 ``None`` 表示「不按期间限制」（例如自定义了一个跨多月的区间）。
    """
    if filters.period:
        if "-" in filters.period:
            return Q(period=filters.period)
        return Q(period__startswith=filters.period)
    if filters.years:
        condition = Q(period__startswith=str(filters.years[0]))
        for year in filters.years[1:]:
            condition |= Q(period__startswith=str(year))
        return condition
    return None

def _dimension_filter(filters: AnalyticsFilters) -> Q:
    """构建 DWS 表上的渠道 / 品类条件。**空列表 = 不限**，因此这里只在非空时叠加。"""
    condition = Q()
    if filters.channels:
        condition &= Q(channel__in=filters.channels)
    if filters.categories:
        condition &= Q(category__in=filters.categories)
    return condition

def _period_label(filters: AnalyticsFilters) -> tuple:
    """返回 ``(period_keys, granularity)``。

    ``granularity`` 取 ``"year"`` / ``"month"`` / ``"custom"`` / ``"all"``，
    决定同比环比取哪个相邻期间。
    """
    start, end = filters.resolved_date_range()
    if filters.period:
        if "-" in filters.period:
            return [filters.period], "month"
        return [filters.period], "year"
    if filters.years and len(filters.years) == 1 and not filters.period:
        if start == date(filters.years[0], 1, 1) and end == date(filters.years[0], 12, 31):
            return [str(filters.years[0])], "year"
    if start and end:
        return [], "custom"
    return [], "all"

def _range_totals(start: date, end: date, filters: AnalyticsFilters) -> dict:
    """按 DWS 表汇总给定日期区间的核心金额（用于同比/环比）。

    同比环比必须用**同一口径**重算区间金额，而不是拿「上一个 period 字符串」
    去查，，用户可能只筛了 3 月上半月，这时「上一期」是 2 月上半月。
    """
    if not start or not end or (end - start).days > MAX_PERIOD_DAYS:
        return {}
    condition = _dimension_filter(filters)
    rows = DwsBusinessMonthly._base_manager.filter(condition)
    totals = {"revenue": ZERO, "gross_profit": ZERO, "order_count": 0}
    for row in rows.iterator(chunk_size=2000):
        period = row.period or ""
        if "-" not in period:
            continue
        year, month = int(period[:4]), int(period[5:7])
        month_start, month_end = date(year, month, 1), _month_end(year, month)
        if month_end < start or month_start > end:
            continue
        totals["revenue"] += _d(row.revenue)
        totals["gross_profit"] += _d(row.gross_profit)
        totals["order_count"] += int(row.order_count or 0)
    return totals

def _fact_totals(start: date, end: date, filters: AnalyticsFilters) -> dict:
    """fact 表口径的区间汇总（自定义区间或含 DWS 不支持的维度时使用）。"""
    orders = filters.apply_orders(get_valid_orders(filters))
    if start:
        orders = orders.filter(order_date__gte=start)
    if end:
        orders = orders.filter(order_date__lte=end)
    revenue = orders.aggregate(total=Sum("final_amount"))["total"] or ZERO
    order_count = orders.count()
    profit = (
        filters.apply_items(OrderItem.objects.all())
        .filter(order__in=orders)
        .aggregate(total=Sum("gross_profit"))["total"]
        or ZERO
    )
    return {
        "revenue": _d(revenue),
        "gross_profit": _d(profit),
        "order_count": order_count,
    }

def _collections(start: date | None, end: date | None, filters: AnalyticsFilters) -> Decimal:
    """区间回款额（非退款付款，按付款日归集）。

    回款与成交额是两个时间轴：成交按订单日、回款按付款日。
    这就是为什么本函数单独成体，而不是复用订单的筛选逻辑。
    """
    queryset = Payment.objects.filter(is_refund=False)
    if start is not None:
        queryset = queryset.filter(payment_date__gte=start)
    if end is not None:
        queryset = queryset.filter(payment_date__lte=end)
    if filters.channels:
        queryset = queryset.filter(order__channel__in=filters.channels)
    if filters.categories:
        queryset = queryset.filter(order__dominant_category__in=filters.categories)
    if filters.customer_types:
        queryset = queryset.filter(order__customer_type__in=filters.customer_types)
    if filters.order_types:
        queryset = queryset.filter(order__order_type__in=filters.order_types)
    if filters.years:
        queryset = queryset.filter(payment_date__year__in=filters.years)
    total = queryset.aggregate(total=Sum("amount"))["total"]
    return _d(total)

def _on_time_stats(filters: AnalyticsFilters) -> dict:
    """准时交付统计。分母只含 ``is_on_time in (0, 1)``。

    排除 -1（未交付/不适用）是本项目最重要的口径细节之一：
    把在途订单算进分母，准时率会随订单积压虚假下降。
    """
    queryset = filters.apply_orders(get_valid_orders(filters)).filter(is_on_time__in=(0, 1))
    total = queryset.count()
    on_time = queryset.filter(is_on_time=1).count()
    return {
        "sample_orders": total,
        "on_time_orders": on_time,
        "on_time_rate": _ratio(on_time, total),
    }

def _default_repurchase_interval() -> int | None:
    """客户级平均复购间隔（``category='' & channel=''`` 的全量行）。"""
    row = (
        DwsRepurchaseInterval._base_manager.filter(category="", channel="")

        .first()
    )
    if row is not None and row.interval_days:
        return int(row.interval_days)
    if not _has_dws_rows(DwsRepurchaseInterval):
        # 回退：直接按客户相邻两单现算（数据量小，可接受）
        gaps = []
        previous_by_customer = {}
        orders = (
            Order.objects.exclude(order_status="已取消")
            .filter(customer_id__isnull=False, order_date__isnull=False)
            .order_by("customer_id", "order_date")
            .values_list("customer_id", "order_date")
        )
        for customer_id, order_date in orders.iterator(chunk_size=2000):
            previous = previous_by_customer.get(customer_id)
            if previous is not None:
                gap = (order_date - previous).days
                if gap > 0:
                    gaps.append(gap)
            previous_by_customer[customer_id] = order_date
        if gaps:
            return int(round(sum(gaps) / len(gaps)))
    return None

# ---------------------------------------------------------------------------
# 基础 QuerySet
# ---------------------------------------------------------------------------
def get_valid_orders(filters: AnalyticsFilters):
    """有效订单（排除已取消）并套用筛选条件。"""
    queryset = Order.objects.exclude(order_status="已取消").select_related(
        "customer", "store", "sales_rep"
    )
    return filters.apply_orders(queryset)

def get_done_orders(filters: AnalyticsFilters):
    """已完成订单（有实际交付日）。交付类指标一律基于它计算。"""
    return get_valid_orders(filters).filter(
        order_status="已完成", actual_delivery_date__isnull=False
    )

def get_items(filters: AnalyticsFilters):
    """订单明细（已套用筛选条件，含订单与产品关联）。"""
    queryset = OrderItem.objects.select_related("order", "product").exclude(
        order__order_status="已取消"
    )
    return filters.apply_items(queryset)

# ---------------------------------------------------------------------------
# KPI 总览
# ---------------------------------------------------------------------------
def get_kpi_summary(filters: AnalyticsFilters) -> dict:
    """经营总览 KPI。

    返回字段与 ``metrics.METRIC_DEFINITIONS`` 的 code 一一对应（``metric_code``），
    这样页面上的小问号（口径说明）可以直接按 code 取定义，不必再维护一份映射。

    实现策略：
    - 能用 DWS 表时，金额类指标全部走预聚合（快且与趋势图口径一致）；
    - 准时率、回款额必须查 fact 表（DWS 没有这两个维度的聚合）；
    - 同比/环比用 :func:`_range_totals` 按「同样的天数窗口」重算，
      而不是简单地取「上月」，这样自定义区间也能得到正确答案。
    """
    start, end = filters.resolved_date_range()
    period_keys, granularity = _period_label(filters)
    use_dws = _dws_usable(filters)

    revenue = ZERO
    gross_profit = ZERO
    order_count = 0
    customer_count = 0
    new_customer_count = 0
    source = "dws_business_monthly" if use_dws else "fact"

    if use_dws:
        rows = DwsBusinessMonthly._base_manager.all()
        period_condition = _period_filter(filters)
        if period_condition is not None:
            rows = rows.filter(period_condition)
        elif start and end:
            # 自定义区间：按年月字符串过滤（DWS 是按月聚合，做不到日内精度）
            allowed = set()
            cursor = date(start.year, start.month, 1)
            while cursor <= end:
                allowed.add("{:04d}-{:02d}".format(cursor.year, cursor.month))
                cursor = date(cursor.year + (1 if cursor.month == 12 else 0),
                              1 if cursor.month == 12 else cursor.month + 1, 1)
            rows = rows.filter(period__in=sorted(allowed))
        rows = rows.filter(_dimension_filter(filters))
        aggregated = rows.aggregate(
            revenue=Sum("revenue"),
            gross_profit=Sum("gross_profit"),
            orders=Sum("order_count"),
            customers=Sum("customer_count"),
            new_customers=Sum("new_customer_count"),
        )
        revenue = _d(aggregated["revenue"])
        gross_profit = _d(aggregated["gross_profit"])
        order_count = int(aggregated["orders"] or 0)
        customer_count = int(aggregated["customers"] or 0)
        new_customer_count = int(aggregated["new_customers"] or 0)
    else:
        orders = get_valid_orders(filters)
        aggregated = orders.aggregate(revenue=Sum("final_amount"))
        revenue = _d(aggregated["revenue"])
        order_count = orders.count()
        customer_count = orders.values("customer_id").distinct().count()
        gross_profit = _d(
            filters.apply_items(OrderItem.objects.all())
            .filter(order__in=orders)
            .aggregate(total=Sum("gross_profit"))["total"]
        )
        new_customer_count = orders.filter(
            customer__first_order_date=F("order_date")
        ).values("customer_id").distinct().count()

    # 准时率与回款额：无论走哪条路径都查 fact 表
    on_time = _on_time_stats(filters)
    collection_amount = _collections(start, end, filters)

    # 目标达成率：把「年」目标与「月」目标分开求和后合并
    target_value = ZERO
    for value in period_keys:
        period_type = "month" if "-" in value else "year"
        total = SalesTargetSum(value, period_type, granularity)
        target_value += total
    achievement = _ratio(revenue, target_value) if target_value > 0 else None

    # 同比 / 环比
    yoy = qoq = None
    if start and end:
        span = (end - start).days + 1
        previous_end = start - timedelta(days=1)
        previous_start = previous_end - timedelta(days=span - 1)
        if granularity == "year":
            yoy_start = date(start.year - 1, start.month, start.day)
            yoy_end = date(end.year - 1, end.month, end.day)
        else:
            yoy_start = date(start.year - 1, start.month, start.day)
            yoy_end = date(end.year - 1, end.month, end.day)
        yoy_totals = (
            _range_totals(yoy_start, yoy_end, filters) if use_dws else _fact_totals(yoy_start, yoy_end, filters)
        )
        qoq_totals = (
            _range_totals(previous_start, previous_end, filters)
            if use_dws
            else _fact_totals(previous_start, previous_end, filters)
        )
        yoy = _ratio(revenue - _d(yoy_totals.get("revenue", ZERO)), yoy_totals.get("revenue", ZERO))
        qoq = _ratio(revenue - _d(qoq_totals.get("revenue", ZERO)), qoq_totals.get("revenue", ZERO))

    margin = _ratio(gross_profit, revenue)
    return {
        "source": source,
        "period": filters.period or ("/".join(str(year) for year in filters.years) or "全量"),
        "granularity": granularity,
        "date_from": start.isoformat() if start else None,
        "date_to": end.isoformat() if end else None,
        "revenue": _q2(revenue),
        "order_count": order_count,
        "avg_order_value": _q2(_d(safe_div(revenue, order_count, default=ZERO))),
        "customer_count": customer_count,
        "new_customer_count": new_customer_count,
        "on_time_rate": on_time["on_time_rate"],
        "delivery_sample_orders": on_time["sample_orders"],
        "gross_profit": _q2(gross_profit),
        "gross_margin": margin,
        "collection_amount": _q2(collection_amount),
        "collection_rate": _ratio(collection_amount, revenue),
        "target_value": _q2(target_value),
        "target_achievement": achievement,
        "yoy": yoy,
        "qoq": qoq,
        "metric_codes": [
            "M_OV_001", "M_OV_002", "M_OV_003", "M_OV_004", "M_OV_005",
            "M_OV_006", "M_OV_007", "M_OV_008", "M_CW_001", "M_DS_001",
        ],
    }

def SalesTargetSum(period_value: str, period_type: str, granularity: str) -> Decimal:
    """汇总「全公司 / 成交额」目标值。

    只取 ``target_scope='全公司'`` 且 ``target_key='ALL'``，
    否则会把渠道、业务员、品类目标一并算进来，目标值直接翻好几倍。
    （函数名保留大写开头是为了在调用点一眼看出它是「业务口径取数」。
    这是本项目唯一一处违反 snake_case 的地方，旧代码多处引用，改名成本高于收益。）
    """
    from apps.sales.models import SalesTarget

    if not period_value:
        return ZERO
    if granularity == "year":
        period_type = "year"
        period_value = period_value[:4]
    elif granularity == "month":
        period_type = "month"
    queryset = SalesTarget.objects.filter(
        target_scope="全公司",
        target_key="ALL",
        metric="成交额",
        period_type=period_type,
        period=period_value,
    )
    total = queryset.aggregate(total=Sum("target_value"))["total"]
    return _d(total)

# ---------------------------------------------------------------------------
# 趋势与结构
# ---------------------------------------------------------------------------
def get_monthly_trend(filters: AnalyticsFilters, metric: str = "revenue", grain: str = "month") -> list:
    """月度/季度/年度趋势。

    ``metric`` 取 ``revenue`` / ``gross_profit`` / ``order_count`` /
    ``customer_count`` / ``avg_order_value`` 之一；
    ``grain`` 取 ``month`` / ``quarter`` / ``year``。

    优先读 :class:`DwsBusinessMonthly`（一次查询拿全），
    仅在筛选条件超出其维度时回退 fact 表逐月聚合。
    """
    records: list = []
    if _dws_usable(filters):
        rows = DwsBusinessMonthly._base_manager.all()
        period_condition = _period_filter(filters)
        if period_condition is not None:
            rows = rows.filter(period_condition)
        rows = rows.filter(_dimension_filter(filters))
        buckets: dict = defaultdict(lambda: {"revenue": ZERO, "gross_profit": ZERO, "orders": 0, "customers": 0})
        for row in rows.iterator(chunk_size=2000):
            period = row.period or ""
            if "-" not in period:
                continue
            key = _grain_key(period, grain)
            slot = buckets[key]
            slot["revenue"] += _d(row.revenue)
            slot["gross_profit"] += _d(row.gross_profit)
            slot["orders"] += int(row.order_count or 0)
            slot["customers"] += int(row.customer_count or 0)
        for key in sorted(buckets.keys()):
            slot = buckets[key]
            records.append(
                {
                    "period": key,
                    "revenue": _q2(slot["revenue"]),
                    "gross_profit": _q2(slot["gross_profit"]),
                    "gross_margin": _ratio(slot["gross_profit"], slot["revenue"]),
                    "order_count": slot["orders"],
                    "customer_count": slot["customers"],
                    "avg_order_value": _q2(_d(safe_div(slot["revenue"], slot["orders"], default=ZERO))),
                }
            )
        source = "dws_business_monthly"
    else:
        orders = get_valid_orders(filters)
        buckets = defaultdict(lambda: {"revenue": ZERO, "orders": 0, "customers": set()})
        profit_by_order = {
            row["order_id"]: _d(row["total"])
            for row in filters.apply_items(OrderItem.objects.all())
            .filter(order__in=orders)
            .values("order_id")
            .annotate(total=Sum("gross_profit"))
        }
        for order in orders:
            if not order.order_year or not order.order_month:
                continue
            key = _grain_key("{:04d}-{:02d}".format(order.order_year, order.order_month), grain)
            slot = buckets[key]
            slot["revenue"] += _d(order.final_amount)
            slot["orders"] += 1
            slot["customers"].add(order.customer_id)
            slot["profit"] = slot.get("profit", ZERO) + profit_by_order.get(order.id, ZERO)
        for key in sorted(buckets.keys()):
            slot = buckets[key]
            profit = slot.get("profit", ZERO)
            records.append(
                {
                    "period": key,
                    "revenue": _q2(slot["revenue"]),
                    "gross_profit": _q2(profit),
                    "gross_margin": _ratio(profit, slot["revenue"]),
                    "order_count": slot["orders"],
                    "customer_count": len(slot["customers"]),
                    "avg_order_value": _q2(_d(safe_div(slot["revenue"], slot["orders"], default=ZERO))),
                }
            )
        source = "fact"

    # 加上同比列（与上一年同 key 比较），便于趋势图叠加双线
    by_period = {record["period"]: record for record in records}
    for record in records:
        previous_key = _previous_year_key(record["period"])
        previous = by_period.get(previous_key)
        record["previous_revenue"] = previous["revenue"] if previous else None
        record["yoy"] = (
            _ratio(record["revenue"] - previous["revenue"], previous["revenue"]) if previous else None
        )
        record["metric_value"] = record.get(metric, record["revenue"])
        record["source"] = source
    return records

def _grain_key(period: str, grain: str) -> str:
    """把 ``YYYY-MM`` 归到目标粒度：月保持原样，季度转 ``YYYY-Qn``，年份取前 4 位。"""
    if grain == "year":
        return period[:4]
    if grain == "quarter":
        month = int(period[5:7])
        return "{}-Q{}".format(period[:4], (month - 1) // 3 + 1)
    return period

def _previous_year_key(key: str) -> str:
    """``YYYY-MM`` / ``YYYY-Qn`` / ``YYYY`` 的去年同期 key。"""
    if len(key) >= 4 and key[:4].isdigit():
        return "{}{}".format(int(key[:4]) - 1, key[4:])
    return ""

def get_yearly_summary(filters: AnalyticsFilters) -> list:
    """年度汇总（年份升序）。"""
    trend = get_monthly_trend(filters, grain="year")
    result = []
    for index, record in enumerate(trend):
        previous = trend[index - 1] if index > 0 else None
        result.append(
            {
                "year": record["period"],
                "revenue": record["revenue"],
                "gross_profit": record["gross_profit"],
                "gross_margin": record["gross_margin"],
                "order_count": record["order_count"],
                "customer_count": record["customer_count"],
                "avg_order_value": record["avg_order_value"],
                "yoy": record["yoy"],
                "previous_revenue": record["previous_revenue"],
                "qoq": _ratio(
                    record["revenue"] - previous["revenue"], previous["revenue"]
                )
                if previous
                else None,
            }
        )
    return result

def get_channel_performance(filters: AnalyticsFilters) -> list:
    """渠道绩效：成交额、订单数、客户数、毛利、获客成本与 ROI。

    获客成本用的 ``CHANNEL_ACQUISITION_COST``（元/线索）是**模拟设定值**，
    文档里必须标注：真实系统应接 CRM 的渠道费用表。
    ROI 用「毛利 ÷ 营销费用」而不是成交额，避免把高客单低毛利的渠道算成好渠道。
    """
    buckets: dict = defaultdict(
        lambda: {"revenue": ZERO, "profit": ZERO, "orders": 0, "customers": set(), "new_customers": 0, "discount": ZERO}
    )
    if _dws_usable(filters):
        rows = DwsBusinessMonthly._base_manager.all()
        period_condition = _period_filter(filters)
        if period_condition is not None:
            rows = rows.filter(period_condition)
        if filters.categories:
            rows = rows.filter(category__in=filters.categories)
        for row in rows.iterator(chunk_size=2000):
            channel = row.channel or ""
            slot = buckets[channel]
            slot["revenue"] += _d(row.revenue)
            slot["profit"] += _d(row.gross_profit)
            slot["orders"] += int(row.order_count or 0)
            slot["customers_count"] = slot.get("customers_count", 0) + int(row.customer_count or 0)
            slot["new_customers"] += int(row.new_customer_count or 0)
            slot["discount"] += _d(row.discount_amount)
        source = "dws_business_monthly"
    else:
        orders = get_valid_orders(filters)
        profit_by_order = {
            row["order_id"]: _d(row["total"])
            for row in filters.apply_items(OrderItem.objects.all())
            .filter(order__in=orders)
            .values("order_id")
            .annotate(total=Sum("gross_profit"))
        }
        for order in orders:
            channel = order.channel or ""
            slot = buckets[channel]
            slot["revenue"] += _d(order.final_amount)
            slot["profit"] += profit_by_order.get(order.id, ZERO)
            slot["orders"] += 1
            slot["customers"].add(order.customer_id)
            slot["discount"] += _q2(_d(order.total_amount) * _d(order.discount))
            if (
                order.customer is not None
                and order.customer.first_order_date is not None
                and order.order_date is not None
                and order.customer.first_order_date == order.order_date
            ):
                slot["new_customers"] += 1
        source = "fact"

    total_revenue = sum((slot["revenue"] for slot in buckets.values()), ZERO)
    records = []
    for channel, slot in sorted(buckets.items(), key=lambda item: -item[1]["revenue"]):
        new_customers = slot["new_customers"]
        lead_cost = _d(CHANNEL_ACQUISITION_COST.get(channel, 0)) if CHANNEL_ACQUISITION_COST else ZERO
        # 线索数 = 新增客户数 × 3（假设 3 条线索成交 1 单，属本项目的估算口径）
        leads = new_customers * 3
        marketing_cost = lead_cost * leads
        customers_count = slot.get("customers_count") or len(slot.get("customers", ()) or ())
        records.append(
            {
                "channel": channel,
                "channel_label": channel,
                "revenue": _q2(slot["revenue"]),
                "gross_profit": _q2(slot["profit"]),
                "gross_margin": _ratio(slot["profit"], slot["revenue"]),
                "order_count": slot["orders"],
                "customer_count": customers_count,
                "new_customer_count": new_customers,
                "revenue_share": _ratio(slot["revenue"], total_revenue),
                "avg_order_value": _q2(_d(safe_div(slot["revenue"], slot["orders"], default=ZERO))),
                "discount_amount": _q2(slot["discount"]),
                "discount_rate": _ratio(slot["discount"], slot["revenue"] + slot["discount"]),
                "marketing_cost": _q2(marketing_cost),
                "cost_per_customer": _q2(_d(safe_div(marketing_cost, new_customers, default=ZERO)))
                if new_customers
                else None,
                "roi": _ratio(slot["profit"], marketing_cost) if marketing_cost > 0 else None,
                "source": source,
                "caveat": "线索数与营销费用为估算值（按新增客户 ×3 线索、渠道单价来源 constants）",
            }
        )
    return records

def get_category_structure(filters: AnalyticsFilters) -> list:
    """品类结构：收入、毛利、成本五项、占比与单位面积造价。"""

    def _empty_bucket() -> dict:
        """新建一个品类聚合格。

        字段里同时保留「成本五项」与「面积」：DWS 路径算不出面积
        （预聚合表没有这一列），fact 回退路径可以，因此统一初始化为 0，
        缺失时 ``unit_price_per_m2`` 返回 ``None`` 而不是 0。
        """
        return {
            "revenue": ZERO,
            "profit": ZERO,
            "qty": 0,
            "material": ZERO,
            "labor": ZERO,
            "manufacturing": ZERO,
            "logistics": ZERO,
            "installation": ZERO,
            "discount": ZERO,
            "orders": 0,
            "area": ZERO,
        }

    buckets: dict = defaultdict(_empty_bucket)
    use_dws = _has_dws_rows(DwsCategoryMonthly) and not any(
        getattr(filters, name) for name in ("customer_types", "order_types", "stores", "segments")
    )
    if use_dws:
        rows = DwsCategoryMonthly._base_manager.all()
        if filters.period:
            if "-" in filters.period:
                rows = rows.filter(period=filters.period)
            else:
                rows = rows.filter(period__startswith=filters.period)
        elif filters.years:
            condition = Q(period__startswith=str(filters.years[0]))
            for year in filters.years[1:]:
                condition |= Q(period__startswith=str(year))
            rows = rows.filter(condition)
        if filters.categories:
            rows = rows.filter(category__in=filters.categories)
        for row in rows.iterator(chunk_size=2000):
            slot = buckets[row.category or ""]
            slot["qty"] += int(row.qty or 0)
            slot["revenue"] += _d(row.revenue)
            slot["profit"] += _d(row.gross_profit)
            slot["material"] += _d(row.material_cost)
            slot["labor"] += _d(row.labor_cost)
            slot["manufacturing"] += _d(row.manufacturing_cost)
            slot["logistics"] += _d(row.logistics_cost)
            slot["installation"] += _d(row.installation_cost)
            slot["discount"] += _d(row.avg_discount)
        source = "dws_category_monthly"
    else:
        items = get_items(filters).select_related("order")
        cost_by_item = defaultdict(Decimal)
        for item_id, cost_type, amount in OrderItemCost.objects.values_list("item_id", "cost_type", "amount"):
            cost_by_item[(item_id, cost_type)] += _d(amount)
        for item in items.iterator(chunk_size=1000):
            slot = buckets[item.category or ""]
            slot["qty"] += int(item.quantity or 0)
            slot["revenue"] += _d(item.line_amount)
            slot["profit"] += _d(item.gross_profit)
            slot["material"] += cost_by_item.get((item.id, "material"), ZERO)
            slot["labor"] += cost_by_item.get((item.id, "labor"), ZERO)
            slot["manufacturing"] += cost_by_item.get((item.id, "manufacturing"), ZERO)
            slot["logistics"] += cost_by_item.get((item.id, "logistics"), ZERO)
            slot["installation"] += cost_by_item.get((item.id, "installation"), ZERO)
            slot["area"] += _d(item.area_m2)
        source = "fact"

    total_revenue = sum((slot["revenue"] for slot in buckets.values()), ZERO)
    records = []
    for category, slot in sorted(buckets.items(), key=lambda item: -item[1]["revenue"]):
        total_cost = (
            slot["material"] + slot["labor"] + slot["manufacturing"]
            + slot["logistics"] + slot["installation"]
        )
        records.append(
            {
                "category": category,
                "category_label": category,
                "qty": slot["qty"],
                "revenue": _q2(slot["revenue"]),
                "revenue_share": _ratio(slot["revenue"], total_revenue),
                "gross_profit": _q2(slot["profit"]),
                "gross_margin": _ratio(slot["profit"], slot["revenue"]),
                "total_cost": _q2(total_cost),
                "material_cost": _q2(slot["material"]),
                "labor_cost": _q2(slot["labor"]),
                "manufacturing_cost": _q2(slot["manufacturing"]),
                "logistics_cost": _q2(slot["logistics"]),
                "installation_cost": _q2(slot["installation"]),
                "material_ratio": _ratio(slot["material"], total_cost),
                "unit_price_per_m2": _q2(_d(safe_div(slot["revenue"], slot["area"], default=ZERO)))
                if slot["area"] > 0
                else None,
                "source": source,
            }
        )

    # CR3 集中度：收入前三的品类占比
    top3 = sum((record["revenue"] for record in records[:3]), ZERO)
    for record in records:
        record["cr3_basis"] = _ratio(top3, total_revenue)
    return records

# ---------------------------------------------------------------------------
# 客户价值
# ---------------------------------------------------------------------------
def get_customer_segments(filters: AnalyticsFilters) -> list:
    """RFM 客群分布。

    口径：客群来自 ``Customer.segment``（由 ml 应用回写），
    本函数只做统计不重新判定，，保证「看板上的分群」与「营销名单」永远一致。
    """
    queryset = filters.apply_customers(Customer.objects.all())
    buckets: dict = {
        segment: {"customers": 0, "monetary": ZERO, "recency": [], "frequency": [], "spend": ZERO}
        for segment in SEGMENT_VALUES
    }
    for row in queryset.values("segment").annotate(
        customers=Count("id"),
        monetary=Sum("total_spend"),
        avg_recency=Avg("recency_days"),
        avg_frequency=Avg("frequency"),
    ):
        segment = row["segment"] or "未分群"
        bucket = buckets.setdefault(
            segment, {"customers": 0, "monetary": ZERO, "recency": [], "frequency": [], "spend": ZERO}
        )
        bucket["customers"] = int(row["customers"] or 0)
        bucket["monetary"] = _d(row["monetary"])
        bucket["avg_recency"] = float(row["avg_recency"]) if row["avg_recency"] is not None else None
        bucket["avg_frequency"] = float(row["avg_frequency"]) if row["avg_frequency"] is not None else None

    total_customers = sum(bucket["customers"] for bucket in buckets.values())
    total_monetary = sum((bucket["monetary"] for bucket in buckets.values()), ZERO)
    records = []
    for segment, bucket in buckets.items():
        if not bucket["customers"] and not bucket["monetary"]:
            continue
        records.append(
            {
                "segment": segment,
                "segment_label": segment,
                "customer_count": bucket["customers"],
                "customer_share": _ratio(bucket["customers"], total_customers),
                "total_spend": _q2(bucket["monetary"]),
                "monetary_share": _ratio(bucket["monetary"], total_monetary),
                "avg_spend": _q2(_d(safe_div(bucket["monetary"], bucket["customers"], default=ZERO))),
                "avg_recency_days": bucket.get("avg_recency"),
                "avg_frequency": bucket.get("avg_frequency"),
            }
        )
    records.sort(key=lambda record: -record["total_spend"])
    return records

def get_cohort_matrix(filters: AnalyticsFilters, max_index: int = 12) -> dict:
    """客户留存矩阵（队列 × 第 N 月）。

    返回结构：``{"cohorts": [{"cohort_month":..., "customer_count":..., "retention": [r0, r1, ...]}],
    "max_index": 12}``。
    数组里有 ``None`` 的格子表示「该队列还没到这个月」，，必须是 ``None`` 而不是 0，
    否则热力图会把「还没到」涂成「留存为 0」，这是个极易犯的可视化错误。
    """
    rows = DwsCustomerCohort._base_manager.all()
    if filters.years:
        condition = Q(cohort_month__startswith=str(filters.years[0]))
        for year in filters.years[1:]:
            condition |= Q(cohort_month__startswith=str(year))
        rows = rows.filter(condition)
    elif filters.period:
        rows = rows.filter(cohort_month__startswith=filters.period[:4])

    cohort_totals: dict = {}
    retention: dict = defaultdict(dict)
    for row in rows.iterator(chunk_size=2000):
        cohort_totals[row.cohort_month] = int(row.customer_count or 0)
        if row.month_index is None or row.month_index > max_index:
            continue
        retention[row.cohort_month][int(row.month_index)] = float(row.retention_rate or 0)

    cohorts = []
    for cohort_month in sorted(cohort_totals.keys()):
        series = []
        for index in range(max_index + 1):
            value = retention.get(cohort_month, {}).get(index)
            series.append(value)
        cohorts.append(
            {
                "cohort_month": cohort_month,
                "customer_count": cohort_totals[cohort_month],
                "retention": series,
            }
        )

    # 平均留存曲线：只对「存在该 index 的队列」求平均，避免被未到达格子拉低
    average_curve = []
    for index in range(max_index + 1):
        values = [
            retention[cohort][index] for cohort in retention if index in retention[cohort]
        ]
        average_curve.append(round(sum(values) / len(values), 4) if values else None)
    return {
        "cohorts": cohorts,
        "average_retention": average_curve,
        "max_index": max_index,
        "source": "dws_customer_cohort",
    }

def get_churn_risk_list(filters: AnalyticsFilters, limit: int = 100) -> list:
    """流失预警名单（规则法）。

    规则（写进指标字典 M_CV_006）：
        monetary 在全体客户的 **Top 30%** 之上，
        且 ``recency_days`` > **平均复购间隔 × 2**。

    为什么用规则而不是模型：规则可解释、可复现，业务拿到名单就能直接打电话，
    而且能说清「为什么他在名单里」。模型（``ScoringResult``）用于更精细的排序，
    两者冲突时以规则为准做触达。
    """
    interval = _default_repurchase_interval()
    if not interval:
        interval = 180  # 兜底：门业半年无单即视为沉默
    threshold_days = int(interval * 2)

    queryset = filters.apply_customers(Customer.objects.all())
    total_customers = queryset.count()
    top_slice = max(1, int(total_customers * 0.30))
    monetary_values = list(
        queryset.exclude(monetary__isnull=True)
        .order_by("-monetary")
        .values_list("monetary", flat=True)[:top_slice]
    )
    if not monetary_values:
        return []
    # 分界值取 Top30% 切片里的最小值：它天然把「Top30% 以外」的客户排除在外，
    # 因此不需要再对 monetary 做一次全量排序（那在 MySQL 上是昂贵的 filesort）。
    cutoff = _d(monetary_values[-1])

    risk_queryset = queryset.filter(
        monetary__gte=cutoff, recency_days__gt=threshold_days
    ).order_by("-monetary")[:limit]

    records = []
    for customer in risk_queryset:
        records.append(
            {
                "customer_code": customer.customer_code,
                "customer_name": customer.customer_name,
                "customer_type": customer.customer_type,
                "channel": customer.channel,
                "segment": customer.segment,
                "member_level": customer.member_level,
                "monetary": _q2(_d(customer.monetary)),
                "frequency": int(customer.frequency or customer.total_orders or 0),
                "recency_days": int(customer.recency_days or 0),
                "last_order_date": customer.last_order_date.isoformat()
                if customer.last_order_date
                else None,
                "risk_level": "高" if (customer.recency_days or 0) > threshold_days * 1.5 else "中",
                "reason": "距上次下单 {} 天，超过平均复购间隔（{} 天）的 2 倍".format(
                    int(customer.recency_days or 0), interval
                ),
            }
        )
    return records

# ---------------------------------------------------------------------------
# 交付与售后
# ---------------------------------------------------------------------------
def get_delivery_performance(filters: AnalyticsFilters) -> dict:
    """交付绩效总览：准时率、平均/中位交付周期、延期分布与主责归因。"""
    orders = get_valid_orders(filters)
    delivered = orders.filter(is_on_time__in=(0, 1))
    total = delivered.count()
    on_time = delivered.filter(is_on_time=1).count()

    durations = [
        int(value) for value in delivered.exclude(actual_lead_days__isnull=True)
        .values_list("actual_lead_days", flat=True)
    ]
    durations.sort()
    median = None
    if durations:
        middle = len(durations) // 2
        median = (
            durations[middle]
            if len(durations) % 2
            else round((durations[middle - 1] + durations[middle]) / 2, 1)
        )

    delay_by_reason: dict = defaultdict(int)
    # .values(...).annotate(...) 迭代出来的每一项是 dict，不是 (key, value) 元组。
    # 之前写成 ``for reason, count in ...`` 会按字典的键解包，拿到 ("delay_reason", "rows")，
    # 再 int("rows") 直接抛 ValueError；外层 safe() 把异常吞掉，页面就只剩一条
    # 「交付绩效取数失败」的提示。这类错误不会 500，但会让整块数据消失。
    for row in (
        orders.filter(is_on_time=0)
        .values("delay_reason")
        .annotate(rows=Count("id"))
    ):
        delay_by_reason[row.get("delay_reason") or "未标注"] += int(row.get("rows") or 0)

    delay_days = [
        int(value) for value in orders.filter(is_on_time=0)
        .exclude(delay_days__isnull=True)
        .values_list("delay_days", flat=True)
    ]

    return {
        "sample_orders": total,
        "on_time_orders": on_time,
        "delayed_orders": total - on_time,
        "on_time_rate": _ratio(on_time, total),
        "avg_lead_days": round(sum(durations) / len(durations), 1) if durations else None,
        "median_lead_days": median,
        "max_lead_days": max(durations) if durations else None,
        "avg_delay_days": round(sum(delay_days) / len(delay_days), 1) if delay_days else None,
        "delay_reason_distribution": [
            {"reason": reason, "order_count": count}
            for reason, count in sorted(delay_by_reason.items(), key=lambda item: -item[1])
        ],
        "source": "fact",
    }

def get_stage_bottleneck(filters: AnalyticsFilters, year: int | None = None) -> list:
    """交付阶段瓶颈排名（按 ``bottleneck_score`` 降序）。

    ``bottleneck_score = 阶段耗时占比 × 超时率``，公式与理由见
    ``dws.DwsDeliveryStage`` 与指标字典 M_DS_004。
    """
    rows = DwsDeliveryStage._base_manager.all()
    if year:
        rows = rows.filter(year=year)
    elif filters.years:
        rows = rows.filter(year__in=filters.years)
    if filters.categories:
        rows = rows.filter(category__in=filters.categories)

    buckets: dict = defaultdict(
        lambda: {
            "orders": 0, "delay_orders": 0, "duration_sum": ZERO, "score_sum": ZERO,
            "delay_sum": ZERO, "stage_order": 99, "years": set(),
        }
    )
    count = 0
    for row in rows.iterator(chunk_size=2000):
        count += 1
        bucket = buckets[row.stage]
        orders = int(row.order_count or 0)
        bucket["orders"] += orders
        bucket["delay_orders"] += int(row.delay_count or 0)
        bucket["duration_sum"] += _d(row.avg_duration_days) * orders
        bucket["delay_sum"] += _d(row.avg_delay_days) * int(row.delay_count or 0)
        bucket["score_sum"] += _d(row.bottleneck_score)
        bucket["stage_order"] = min(bucket["stage_order"], int(row.stage_order or 99))
        bucket["years"].add(row.year)

    if not count:
        # 回退：直接用阶段明细现算（DWS 未构建时仍能出图）
        return _stage_bottleneck_from_fact(filters)

    records = []
    for stage, bucket in buckets.items():
        orders = bucket["orders"] or 1
        records.append(
            {
                "stage": stage,
                "stage_order": bucket["stage_order"],
                "order_count": bucket["orders"],
                "avg_duration_days": round(float(bucket["duration_sum"] / orders), 2),
                "delay_count": bucket["delay_orders"],
                "delay_rate": _ratio(bucket["delay_orders"], bucket["orders"]),
                "avg_delay_days": round(
                    float(bucket["delay_sum"] / bucket["delay_orders"]), 2
                )
                if bucket["delay_orders"]
                else 0.0,
                "bottleneck_score": round(float(bucket["score_sum"] / max(len(bucket["years"]), 1)), 4),
                "source": "dws_delivery_stage",
            }
        )
    records.sort(key=lambda record: -record["bottleneck_score"])
    return records

def _stage_bottleneck_from_fact(filters: AnalyticsFilters) -> list:
    """fact 表回退实现：与 DWS 同一公式，保证两条路径结论一致。"""
    stage_orders = filters.apply_orders(get_valid_orders(filters))
    rows = (
        DeliveryStage.objects.filter(order__in=stage_orders)
        .values("stage", "stage_order")
        .annotate(
            order_count=Count("order_id", distinct=True),
            avg_duration=Avg("duration_days"),
            delay_count=Count("id", filter=Q(delayed_days__gt=0)),
            avg_delay=Avg("delayed_days", filter=Q(delayed_days__gt=0)),
        )
    )
    raw = list(rows)
    total_avg = sum((float(row["avg_duration"] or 0) for row in raw), 0.0) or 1.0
    records = []
    for row in raw:
        avg_duration = float(row["avg_duration"] or 0)
        delay_rate = float(row["delay_count"] or 0) / (row["order_count"] or 1)
        records.append(
            {
                "stage": row["stage"],
                "stage_order": int(row["stage_order"] or 99),
                "order_count": int(row["order_count"] or 0),
                "avg_duration_days": round(avg_duration, 2),
                "delay_count": int(row["delay_count"] or 0),
                "delay_rate": round(delay_rate, 4),
                "avg_delay_days": round(float(row["avg_delay"] or 0), 2),
                "bottleneck_score": round((avg_duration / total_avg) * delay_rate, 4),
                "source": "fact",
            }
        )
    records.sort(key=lambda record: -record["bottleneck_score"])
    return records

def get_after_sales_analysis(filters: AnalyticsFilters) -> dict:
    """售后分析：按类型 / 品类 / 责任方 / 状态分布，以及售后成本率。

    售后成本率的分母用「同筛选条件下的成交额」，而不是全公司成交额，，
    否则按品类筛选时，成本率会被其他品类的收入摊薄。
    """
    valid_orders = get_valid_orders(filters)
    queryset = AfterSales.objects.filter(order__in=valid_orders).select_related("order", "customer")

    start, end = filters.resolved_date_range()
    if start is not None:
        queryset = queryset.filter(report_date__gte=start)
    if end is not None:
        queryset = queryset.filter(report_date__lte=end)
    if filters.categories:
        queryset = queryset.filter(category__in=filters.categories)

    total_orders = valid_orders.count()
    order_count = queryset.count()
    cost_total = _d(queryset.aggregate(total=Sum("cost"))["total"])
    revenue = _d(valid_orders.aggregate(total=Sum("final_amount"))["total"])

    def _group(field_name: str) -> list:
        rows = (
            queryset.values(field_name)
            .annotate(
                order_count=Count("id"),
                cost=Sum("cost"),
                avg_handling=Avg("handling_days"),
            )
            .order_by("-order_count")
        )
        return [
            {
                "key": row[field_name] or "未标注",
                "order_count": int(row["order_count"] or 0),
                "cost": _q2(_d(row["cost"])),
                "avg_handling_days": round(float(row["avg_handling"]), 1)
                if row["avg_handling"] is not None
                else None,
                "share": _ratio(row["order_count"], order_count),
            }
            for row in rows
        ]

    quality_types = {"尺寸误差", "门体变形", "五金损坏", "色差"}
    quality_count = sum(
        1 for value in queryset.values_list("after_type", flat=True) if value in quality_types
    )

    return {
        "after_sales_count": order_count,
        "valid_order_count": total_orders,
        "after_sales_rate": _ratio(order_count, total_orders),
        "total_cost": _q2(cost_total),
        "cost_rate": _ratio(cost_total, revenue),
        "avg_cost": _q2(_d(safe_div(cost_total, order_count, default=ZERO))),
        "avg_handling_days": round(
            float(queryset.aggregate(value=Avg("handling_days"))["value"] or 0), 1
        ),
        "quality_event_count": quality_count,
        "quality_event_ratio": _ratio(quality_count, order_count),
        "by_type": _group("after_type"),
        "by_category": _group("category"),
        "by_status": _group("status"),
        "by_responsibility": _group("responsibility"),
        "source": "fact",
    }

# ---------------------------------------------------------------------------
# 回款与现金
# ---------------------------------------------------------------------------
def get_receivables(filters: AnalyticsFilters, bucket: str | None = None, limit: int = 200) -> dict:
    """应收账款：账龄分布 + 明细清单（按未收金额降序）。

    账龄一律以 ``settings.DATA_AS_OF_DATE`` 为切点，，脱离切点的账龄没有意义，
    因此返回值里始终带上 ``as_of_date``。
    """
    as_of = _as_of_date()
    bucket = normalize_aging_bucket(bucket)
    rows = DwsReceivableAging._base_manager.all()
    source = "dws_receivable_aging"
    if not _has_dws_rows(DwsReceivableAging):
        source = "fact"
        rows = []

    bucket_totals: dict = defaultdict(lambda: {"amount": ZERO, "orders": 0, "customers": set()})
    customer_totals: dict = defaultdict(lambda: {"amount": ZERO, "orders": 0, "max_overdue": 0, "buckets": set()})

    if source == "dws_receivable_aging":
        customers = {c.id: c for c in Customer.objects.all()}
        for row in rows.iterator(chunk_size=2000):
            slot = bucket_totals[row.aging_bucket or AGING_BUCKET_ORDER[0]]
            slot["amount"] += _d(row.outstanding_amount)
            slot["orders"] += int(row.order_count or 0)
            slot["customers"].add(row.customer_id)
            detail = customer_totals[row.customer_id]
            detail["amount"] += _d(row.outstanding_amount)
            detail["orders"] += int(row.order_count or 0)
            detail["max_overdue"] = max(detail["max_overdue"], int(row.max_overdue_days or 0))
            detail["buckets"].add(row.aging_bucket or AGING_BUCKET_ORDER[0])
            detail["customer"] = customers.get(row.customer_id)
    else:
        terms = (
            PaymentTerm.objects.filter(outstanding_amount__gt=Decimal("0.01"))
            .select_related("order", "order__customer")

        )
        for term in terms.iterator(chunk_size=1000):
            order = term.order
            if order is None or order.customer_id is None or order.order_status == "已取消":
                continue
            slot = bucket_totals[term.aging_bucket or AGING_BUCKET_ORDER[0]]
            slot["amount"] += _d(term.outstanding_amount)
            slot["orders"] += 1
            slot["customers"].add(order.customer_id)
            detail = customer_totals[order.customer_id]
            detail["amount"] += _d(term.outstanding_amount)
            detail["orders"] += 1
            detail["max_overdue"] = max(detail["max_overdue"], int(term.overdue_days or 0))
            detail["buckets"].add(term.aging_bucket or AGING_BUCKET_ORDER[0])
            detail["customer"] = getattr(order, "customer", None)

    total_amount = sum((slot["amount"] for slot in bucket_totals.values()), ZERO)
    ordering = AGING_BUCKET_ORDER
    bucket_list = []
    for key in ordering:
        slot = bucket_totals.get(key)
        if slot is None:
            bucket_list.append(
                {"aging_bucket": key, "amount": ZERO, "order_count": 0, "customer_count": 0, "share": None}
            )
            continue
        bucket_list.append(
            {
                "aging_bucket": key,
                "amount": _q2(slot["amount"]),
                "order_count": slot["orders"],
                "customer_count": len(slot["customers"]),
                "share": _ratio(slot["amount"], total_amount),
            }
        )

    overdue_amount = sum(
        (slot["amount"] for key, slot in bucket_totals.items() if key in AGING_BUCKET_OVERDUE),
        ZERO,
    )
    over_90 = bucket_totals.get(AGING_BUCKET_ORDER[-1], {}).get("amount", ZERO)

    details = []
    for customer_id, detail in sorted(customer_totals.items(), key=lambda item: -item[1]["amount"]):
        if bucket and bucket not in detail["buckets"]:
            continue
        customer = detail.get("customer")
        if customer is None:
            continue
        details.append(
            {
                "customer_id": customer_id,
                "customer_code": customer.customer_code,
                "customer_name": customer.customer_name,
                "customer_type": customer.customer_type,
                "member_level": customer.member_level,
                "outstanding_amount": _q2(detail["amount"]),
                "order_count": detail["orders"],
                "max_overdue_days": detail["max_overdue"],
                "aging_buckets": sorted(detail["buckets"]),
                "contact_masked": customer.phone,
            }
        )
        if len(details) >= limit:
            break

    return {
        "as_of_date": as_of.isoformat(),
        "source": source,
        "total_outstanding": _q2(total_amount),
        "overdue_amount": _q2(overdue_amount),
        "overdue_ratio": _ratio(overdue_amount, total_amount),
        "over_90_amount": _q2(over_90),
        "customer_count": len(customer_totals),
        "buckets": bucket_list,
        "details": details,
    }

def get_cashflow_weekly(filters: AnalyticsFilters, weeks: int = 13) -> list:
    """未来 13 周回款预测（基于历史回款行为分布）。

    **假设（必须展示给用户，写在页面上）**
    ------------------------------------
    1. 应收账款余额（``DwsReceivableAging``）在未来 13 周内按**恒定周回收率**
       逐周回笼。恒定回收率 = 过去 13 周实际回款额 ÷ 期末应收余额，
       并收敛到 1.5%~8% 的合理区间（数据异常时不让预测飞出天际）。
    2. 忽略客户的经营突变、主动催收加强与季节性（门业 12 月~次年 1 月回款偏慢）。
    3. 预测只含**已发生的应收**，不含未来新签合同的回款。

    它是「排产与备料的现金参考」，不是财务预算数字。
    """
    as_of = _as_of_date()
    total_outstanding = ZERO
    if _has_dws_rows(DwsReceivableAging):
        total_outstanding = _d(
            DwsReceivableAging._base_manager.aggregate(total=Sum("outstanding_amount"))["total"]
        )
    else:
        total_outstanding = _d(
            PaymentTerm.objects.filter(outstanding_amount__gt=Decimal("0.01")).aggregate(
                total=Sum("outstanding_amount")
            )["total"]
        )

    history_start = as_of - timedelta(weeks=max(13, weeks))
    historical = _d(
        Payment.objects.filter(is_refund=False, payment_date__gte=history_start, payment_date__lte=as_of)
        .aggregate(total=Sum("amount"))["total"]
    )
    weekly_rate = 0.03
    if total_outstanding > 0 and historical > 0:
        weeks_of_history = max(13, weeks)
        weekly_rate = float(historical / total_outstanding) / weeks_of_history
        weekly_rate = max(0.015, min(0.08, weekly_rate))

    records = []
    remaining = total_outstanding
    for index in range(1, weeks + 1):
        week_start = as_of + timedelta(days=7 * (index - 1) + 1)
        week_end = week_start + timedelta(days=6)
        expected = _q2(remaining * Decimal(str(round(weekly_rate, 6))))
        remaining = remaining - expected
        records.append(
            {
                "week_index": index,
                "week_label": "W{}".format(index),
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
                "expected_amount": expected,
                "remaining_outstanding": _q2(remaining if remaining > 0 else ZERO),
            }
        )
    return records

# ---------------------------------------------------------------------------
# 业务员 / 门店 / 区域
# ---------------------------------------------------------------------------
def get_rep_ranking(filters: AnalyticsFilters, limit: int | None = None) -> list:
    """业务员业绩排名（默认按成交额降序）。

    优先读 ``DwsRepMonthly``；缺表时回退 fact 表现算。
    """
    records = []
    if _has_dws_rows(DwsRepMonthly):
        rows = filters.apply_rep_perf(DwsRepMonthly._base_manager.all()).select_related("sales_rep")
        buckets: dict = defaultdict(
            lambda: {
                "revenue": ZERO, "profit": ZERO, "collection": ZERO, "orders": 0,
                "customers": 0, "new_customers": 0, "on_time_sum": 0.0, "on_time_rows": 0,
                "discount_sum": 0.0, "discount_rows": 0,
            }
        )
        for row in rows.iterator(chunk_size=2000):
            bucket = buckets[row.sales_rep_id]
            bucket["rep_name"] = getattr(row.sales_rep, "rep_name", "") if row.sales_rep_id else ""
            bucket["revenue"] += _d(row.revenue)
            bucket["profit"] += _d(row.gross_profit)
            bucket["collection"] += _d(row.collection_amount)
            bucket["orders"] += int(row.order_count or 0)
            bucket["customers"] += int(row.customer_count or 0)
            bucket["new_customers"] += int(row.new_customer_count or 0)
            if row.on_time_rate is not None:
                bucket["on_time_sum"] += float(row.on_time_rate)
                bucket["on_time_rows"] += 1
            if row.avg_discount is not None:
                bucket["discount_sum"] += float(row.avg_discount)
                bucket["discount_rows"] += 1
        for rep_id, bucket in buckets.items():
            records.append(
                {
                    "sales_rep_id": rep_id,
                    "rep_name": bucket.get("rep_name", ""),
                    "revenue": _q2(bucket["revenue"]),
                    "gross_profit": _q2(bucket["profit"]),
                    "gross_margin": _ratio(bucket["profit"], bucket["revenue"]),
                    "collection_amount": _q2(bucket["collection"]),
                    "order_count": bucket["orders"],
                    "customer_count": bucket["customers"],
                    "new_customer_count": bucket["new_customers"],
                    "avg_order_value": _q2(_d(safe_div(bucket["revenue"], bucket["orders"], default=ZERO))),
                    "on_time_rate": round(bucket["on_time_sum"] / bucket["on_time_rows"], 4)
                    if bucket["on_time_rows"]
                    else None,
                    "avg_discount": round(bucket["discount_sum"] / bucket["discount_rows"], 4)
                    if bucket["discount_rows"]
                    else None,
                    "source": "dws_rep_monthly",
                }
            )
    else:
        orders = get_valid_orders(filters).filter(sales_rep__isnull=False)
        profit_by_order = {
            row["order_id"]: _d(row["total"])
            for row in filters.apply_items(OrderItem.objects.all())
            .filter(order__in=orders)
            .values("order_id")
            .annotate(total=Sum("gross_profit"))
        }
        buckets = defaultdict(
            lambda: {"revenue": ZERO, "profit": ZERO, "orders": 0, "customers": set(), "on_time": 0, "samples": 0}
        )
        # 不能把 "sales_rep__rep_name" 写进 only()：Django 禁止同一字段既被
        # select_related 关联又被 defer（会抛 "cannot be both deferred and
        # traversed using select_related"）。关联字段由 select_related 一次取回。
        for order in orders.select_related("sales_rep"):
            bucket = buckets[order.sales_rep_id]
            bucket["rep_name"] = order.sales_rep.rep_name if order.sales_rep else ""
            bucket["revenue"] += _d(order.final_amount)
            bucket["profit"] += profit_by_order.get(order.id, ZERO)
            bucket["orders"] += 1
            bucket["customers"].add(order.customer_id)
            if order.is_on_time in (0, 1):
                bucket["samples"] += 1
                if order.is_on_time == 1:
                    bucket["on_time"] += 1
        for rep_id, bucket in buckets.items():
            records.append(
                {
                    "sales_rep_id": rep_id,
                    "rep_name": bucket.get("rep_name", ""),
                    "revenue": _q2(bucket["revenue"]),
                    "gross_profit": _q2(bucket["profit"]),
                    "gross_margin": _ratio(bucket["profit"], bucket["revenue"]),
                    "collection_amount": ZERO,
                    "order_count": bucket["orders"],
                    "customer_count": len(bucket["customers"]),
                    "new_customer_count": 0,
                    "avg_order_value": _q2(_d(safe_div(bucket["revenue"], bucket["orders"], default=ZERO))),
                    "on_time_rate": _ratio(bucket["on_time"], bucket["samples"]),
                    "avg_discount": None,
                    "source": "fact",
                }
            )
    records.sort(key=lambda record: -record["revenue"])
    return records[:limit] if limit else records

def get_store_comparison(filters: AnalyticsFilters) -> list:
    """门店对比：成交额、订单数、客户数、毛利、准时率、人均成交额。"""
    orders = get_valid_orders(filters).filter(store__isnull=False)
    profit_by_order = {
        row["order_id"]: _d(row["total"])
        for row in filters.apply_items(OrderItem.objects.all())
        .filter(order__in=orders)
        .values("order_id")
        .annotate(total=Sum("gross_profit"))
    }
    rep_count = {
        row["store_id"]: row["rows"]
        for row in SalesRep.objects.exclude(store__isnull=True)
        .values("store_id")
        .annotate(rows=Count("id"))
    }

    buckets: dict = defaultdict(
        lambda: {"orders": 0, "revenue": ZERO, "profit": ZERO, "customers": set(), "on_time": 0, "samples": 0}
    )
    for order in orders.select_related("store"):
        bucket = buckets[order.store_id]
        bucket["store_name"] = order.store.store_name if order.store else ""
        bucket["city"] = order.store.city if order.store else ""
        bucket["province"] = order.store.province if order.store else ""
        bucket["orders"] += 1
        bucket["revenue"] += _d(order.final_amount)
        bucket["profit"] += profit_by_order.get(order.id, ZERO)
        bucket["customers"].add(order.customer_id)
        if order.is_on_time in (0, 1):
            bucket["samples"] += 1
            if order.is_on_time == 1:
                bucket["on_time"] += 1

    total_revenue = sum((bucket["revenue"] for bucket in buckets.values()), ZERO)
    records = []
    for store_id, bucket in buckets.items():
        reps = rep_count.get(store_id, 0)
        records.append(
            {
                "store_id": store_id,
                "store_name": bucket.get("store_name", ""),
                "province": bucket.get("province", ""),
                "city": bucket.get("city", ""),
                "revenue": _q2(bucket["revenue"]),
                "revenue_share": _ratio(bucket["revenue"], total_revenue),
                "gross_profit": _q2(bucket["profit"]),
                "gross_margin": _ratio(bucket["profit"], bucket["revenue"]),
                "order_count": bucket["orders"],
                "customer_count": len(bucket["customers"]),
                "avg_order_value": _q2(_d(safe_div(bucket["revenue"], bucket["orders"], default=ZERO))),
                "on_time_rate": _ratio(bucket["on_time"], bucket["samples"]),
                "sales_rep_count": reps,
                "revenue_per_rep": _q2(_d(safe_div(bucket["revenue"], reps, default=ZERO))) if reps else None,
            }
        )
    records.sort(key=lambda record: -record["revenue"])
    return records

def get_region_distribution(filters: AnalyticsFilters) -> list:
    """区域（省 × 市）分布：客户数与成交额，用于判断市场集中度与拓展空间。"""
    orders = get_valid_orders(filters).select_related("customer")
    buckets: dict = defaultdict(lambda: {"revenue": ZERO, "orders": 0, "customers": set()})
    for order in orders.iterator(chunk_size=1000):
        customer = order.customer
        province = getattr(customer, "province", None) or "未标注"
        city = getattr(customer, "city", None) or "未标注"
        bucket = buckets[(province, city)]
        bucket["revenue"] += _d(order.final_amount)
        bucket["orders"] += 1
        bucket["customers"].add(order.customer_id)

    total_revenue = sum((bucket["revenue"] for bucket in buckets.values()), ZERO)
    records = []
    for (province, city), bucket in buckets.items():
        records.append(
            {
                "province": province,
                "city": city,
                "region_label": "{}{}".format(province, city),
                "revenue": _q2(bucket["revenue"]),
                "revenue_share": _ratio(bucket["revenue"], total_revenue),
                "order_count": bucket["orders"],
                "customer_count": len(bucket["customers"]),
                "avg_order_value": _q2(_d(safe_div(bucket["revenue"], bucket["orders"], default=ZERO))),
            }
        )
    records.sort(key=lambda record: -record["revenue"])
    return records

# ---------------------------------------------------------------------------
# 利润分析
# ---------------------------------------------------------------------------
def get_profit_by_sku(filters: AnalyticsFilters, limit: int = 20) -> list:
    """SKU 毛利排名。

    同时给出「总收入排名」与「毛利排名」，因为这两个榜的差异本身就是结论：
    卖得最多的不一定最赚钱。
    """
    items = get_items(filters).select_related("product")
    buckets: dict = defaultdict(
        lambda: {"revenue": ZERO, "cost": ZERO, "profit": ZERO, "qty": 0, "product_name": "", "category": ""}
    )
    for item in items.iterator(chunk_size=1000):
        code = item.product.product_code if item.product_id else item.product_name or "未标注"
        bucket = buckets[code]
        bucket["product_name"] = item.product_name or (
            item.product.product_name if item.product_id else ""
        )
        bucket["category"] = item.category or ""
        bucket["revenue"] += _d(item.line_amount)
        bucket["profit"] += _d(item.gross_profit)
        bucket["cost"] += _d(item.total_cost)
        bucket["qty"] += int(item.quantity or 0)

    records = []
    for code, bucket in buckets.items():
        records.append(
            {
                "product_code": code,
                "product_name": bucket["product_name"],
                "category": bucket["category"],
                "qty": bucket["qty"],
                "revenue": _q2(bucket["revenue"]),
                "total_cost": _q2(bucket["cost"]),
                "gross_profit": _q2(bucket["profit"]),
                "gross_margin": _ratio(bucket["profit"], bucket["revenue"]),
                "unit_price": _q2(_d(safe_div(bucket["revenue"], bucket["qty"], default=ZERO))),
            }
        )
    records.sort(key=lambda record: -record["gross_profit"])
    ranked = records[:limit]

    # 收入排名前 N，用于页面上与毛利排名对照
    revenue_ranked = sorted(records, key=lambda record: -record["revenue"])[:limit]
    for index, record in enumerate(ranked, start=1):
        record["profit_rank"] = index
    return {"by_profit": ranked, "by_revenue": revenue_ranked}

def get_profit_quadrant(filters: AnalyticsFilters) -> dict:
    """营收占比 vs 毛利占比 四象限。

    分界线的取法与理由：
    - 营收分界 = 平均营收占比（即 ``1 / SKU 数``），高于它意味着该 SKU 占了
      超过平均份额的收入；
    - 毛利分界同理 = 平均毛利占比。
    用「平均占比」而不是「中位数」做分界，是因为四象限的语义就是
    「高于/低于平均贡献」；用中位数会让象限划分随样本数漂移，不利于跨期比较。
    """
    payload = get_profit_by_sku(filters, limit=200)
    records = payload["by_profit"]
    if not records:
        return {"quadrants": {}, "sku_count": 0, "thresholds": {}}

    total_revenue = sum((record["revenue"] for record in records), ZERO)
    total_profit = sum((record["gross_profit"] for record in records), ZERO)
    sku_count = len(records)
    # 分界线取「平均占比」= 1 / SKU 数：高于它意味着该 SKU 占了超过平均份额。
    # 用平均而不是中位数，是因为四象限的语义就是「高于/低于平均贡献」；
    # 用中位数会让象限划分随样本数漂移，不利于跨期比较。
    revenue_line = float(Decimal("1") / Decimal(sku_count))
    profit_line = float(Decimal("1") / Decimal(sku_count))

    quadrants = {
        "star": {"name": "明星产品（高收入 · 高毛利）", "note": "要保产能、保质量，是利润支柱。", "items": []},
        "volume": {"name": "跑量产品（高收入 · 低毛利）", "note": "撑规模但吃利润，优先谈材料成本与提价。", "items": []},
        "niche": {"name": "小众精品（低收入 · 高毛利）", "note": "有溢价能力，值得加大推广做爆。", "items": []},
        "drag": {"name": "待优化（低收入 · 低毛利）", "note": "既不赚钱也不走量，考虑下架或改款。", "items": []},
    }
    for record in records:
        revenue_share = float(safe_div(record["revenue"], total_revenue, default=ZERO) or 0)
        profit_share = float(safe_div(record["gross_profit"], total_profit, default=ZERO) or 0)
        entry = {
            "product_code": record["product_code"],
            "product_name": record["product_name"],
            "category": record["category"],
            "revenue": record["revenue"],
            "gross_profit": record["gross_profit"],
            "gross_margin": record["gross_margin"],
            "revenue_share": round(revenue_share, 4),
            "profit_share": round(profit_share, 4),
        }
        if revenue_share >= revenue_line and profit_share >= profit_line:
            quadrants["star"]["items"].append(entry)
        elif revenue_share >= revenue_line:
            quadrants["volume"]["items"].append(entry)
        elif profit_share >= profit_line:
            quadrants["niche"]["items"].append(entry)
        else:
            quadrants["drag"]["items"].append(entry)

    for quadrant in quadrants.values():
        quadrant["count"] = len(quadrant["items"])
        quadrant["items"] = quadrant["items"][:50]

    return {
        "quadrants": quadrants,
        "sku_count": sku_count,
        "thresholds": {"revenue_share": round(revenue_line, 4), "profit_share": round(profit_line, 4)},
        "total_revenue": _q2(total_revenue),
        "total_profit": _q2(total_profit),
    }

def get_discount_margin_relation(filters: AnalyticsFilters) -> list:
    """折扣率与毛利率的关系（按折扣区间分桶）。

    结论通常是「折扣每让 1 个点，毛利率掉得更多」，，因为成本不随折扣下降。
    这条曲线的斜率就是「降价换量」是否划算的量化依据，
    也是「折扣侵蚀率」（M_CP_005）的落地解释。
    """
    buckets = [
        ("无折扣", Decimal("0"), Decimal("0")),
        ("0~3%", Decimal("0"), Decimal("0.03")),
        ("3~5%", Decimal("0.03"), Decimal("0.05")),
        ("5~8%", Decimal("0.05"), Decimal("0.08")),
        ("8~12%", Decimal("0.08"), Decimal("0.12")),
        ("12%以上", Decimal("0.12"), Decimal("1")),
    ]
    orders = get_valid_orders(filters)
    profit_by_order = {
        row["order_id"]: _d(row["total"])
        for row in filters.apply_items(OrderItem.objects.all())
        .filter(order__in=orders)
        .values("order_id")
        .annotate(total=Sum("gross_profit"))
    }

    stats = {label: {"orders": 0, "revenue": ZERO, "profit": ZERO, "discount": ZERO} for label, _low, _high in buckets}
    for order in orders.iterator(chunk_size=1000):
        discount = _d(order.discount)
        label = None
        for candidate_label, low, high in buckets:
            if low <= discount < high:
                label = candidate_label
                break
        if label is None:
            label = buckets[-1][0]
        slot = stats[label]
        slot["orders"] += 1
        slot["revenue"] += _d(order.final_amount)
        slot["profit"] += profit_by_order.get(order.id, ZERO)
        slot["discount"] += _q2(_d(order.total_amount) * discount)

    records = []
    for label, _low, _high in buckets:
        slot = stats[label]
        if not slot["orders"]:
            continue
        records.append(
            {
                "bucket": label,
                "order_count": slot["orders"],
                "revenue": _q2(slot["revenue"]),
                "gross_profit": _q2(slot["profit"]),
                "gross_margin": _ratio(slot["profit"], slot["revenue"]),
                "discount_amount": _q2(slot["discount"]),
                "avg_order_value": _q2(_d(safe_div(slot["revenue"], slot["orders"], default=ZERO))),
            }
        )
    return records

# ---------------------------------------------------------------------------
# 客户 360
# ---------------------------------------------------------------------------
def get_customer_360(customer_code: str) -> dict:
    """单客户全景视图。

    返回 6 个区块，与页面上的 6 个卡片一一对应：
    ``profile`` / ``value_summary`` / ``orders`` / ``payments`` /
    ``after_sales`` / ``timeline`` / ``risk_flags``。

    ``timeline`` 把订单、付款、售后三类事件按时间合并排序，，
    这是排查「这个客户为什么突然不来了」最有效的一屏：
    能看到「交付延期 → 售后工单 → 未再下单」这条完整因果链。
    """
    customer = Customer.objects.filter(customer_code=str(customer_code).strip()).first()
    if customer is None:
        return {"found": False, "customer_code": customer_code}

    orders = list(
        Order.objects.filter(customer=customer)
        .select_related("store", "sales_rep")
        .order_by("-order_date")
    )
    valid_orders = [order for order in orders if order.order_status != "已取消"]
    order_ids = [order.id for order in orders]

    items = list(
        OrderItem.objects.filter(order_id__in=order_ids).values(
            "order_id", "product_name", "category", "quantity", "line_amount", "gross_profit", "area_m2"
        )
    )
    items_by_order = defaultdict(list)
    for item in items:
        items_by_order[item["order_id"]].append(item)

    payments = list(
        Payment.objects.filter(order_id__in=order_ids).order_by("-payment_date")
    )
    after_sales = list(
        AfterSales.objects.filter(Q(customer=customer) | Q(order_id__in=order_ids)).order_by("-report_date")
    )
    terms = list(PaymentTerm.objects.filter(order_id__in=order_ids))

    revenue = sum((_d(order.final_amount) for order in valid_orders), ZERO)
    profit = sum((_d(item["gross_profit"]) for item in items), ZERO)
    paid = sum((_d(payment.amount) for payment in payments if not payment.is_refund), ZERO)
    refunded = sum((_d(payment.amount) for payment in payments if payment.is_refund), ZERO)
    outstanding = sum((_d(term.outstanding_amount) for term in terms), ZERO)
    max_overdue = max((int(term.overdue_days or 0) for term in terms), default=0)
    after_cost = sum((_d(row.cost) for row in after_sales), ZERO)

    timeline = []
    for order in orders:
        timeline.append(
            {
                "date": order.order_date.isoformat() if order.order_date else None,
                "type": "order",
                "title": "下单 {}".format(order.order_no),
                "detail": "成交额 {} 元 · 状态 {}".format(
                    _q2(_d(order.final_amount)), order.order_status or "未标注"
                ),
                "amount": _q2(_d(order.final_amount)),
            }
        )
        if order.actual_delivery_date:
            timeline.append(
                {
                    "date": order.actual_delivery_date.isoformat(),
                    "type": "delivery",
                    "title": "交付 {}".format(order.order_no),
                    "detail": "准时" if order.is_on_time == 1 else "延期 {} 天".format(order.delay_days or 0),
                    "amount": None,
                }
            )
    for payment in payments:
        timeline.append(
            {
                "date": payment.payment_date.isoformat() if payment.payment_date else None,
                "type": "payment",
                "title": "{} {}".format("退款" if payment.is_refund else "回款", payment.stage or ""),
                "detail": "{} · {}".format(payment.payment_method or "", payment.payment_no),
                "amount": _q2(_d(payment.amount)),
            }
        )
    for row in after_sales:
        timeline.append(
            {
                "date": row.report_date.isoformat() if row.report_date else None,
                "type": "after_sales",
                "title": "售后 {}".format(row.after_type or ""),
                "detail": "品类 {} · 状态 {} · 处理 {} 天".format(
                    row.category or "未标注", row.status or "未标注", row.handling_days or 0
                ),
                "amount": _q2(_d(row.cost)),
            }
        )
    timeline.sort(key=lambda event: event["date"] or "", reverse=True)

    interval = _default_repurchase_interval() or 180
    risk_flags = []
    if outstanding > Decimal("0.01"):
        risk_flags.append(
            {
                "level": "高" if max_overdue > 90 else "中",
                "flag": "应收未收",
                "detail": "未收 {} 元，最长逾期 {} 天".format(_q2(outstanding), max_overdue),
            }
        )
    if (customer.recency_days or 0) > interval * 2:
        risk_flags.append(
            {
                "level": "中",
                "flag": "流失风险",
                "detail": "距上次下单 {} 天，超过平均复购间隔（{} 天）的 2 倍".format(
                    int(customer.recency_days or 0), interval
                ),
            }
        )
    if after_sales:
        risk_flags.append(
            {
                "level": "低" if len(after_sales) <= 2 else "中",
                "flag": "售后关注",
                "detail": "工单 {} 单，成本 {} 元".format(len(after_sales), _q2(after_cost)),
            }
        )
    if not risk_flags:
        risk_flags.append({"level": "低", "flag": "正常", "detail": "暂无风险信号"})

    return {
        "found": True,
        "profile": {
            "customer_code": customer.customer_code,
            "customer_name": customer.customer_name,
            "customer_type": customer.customer_type,
            "gender": customer.gender,
            "age": customer.age,
            "phone": customer.phone,
            "province": customer.province,
            "city": customer.city,
            "address": customer.address,
            "channel": customer.channel,
            "member_level": customer.member_level,
            "register_date": customer.register_date.isoformat() if customer.register_date else None,
            "is_lead": customer.is_lead,
            "segment": customer.segment,
            "rfm_score": customer.rfm_score,
            "kmeans_cluster": customer.kmeans_cluster,
        },
        "value_summary": {
            "revenue": _q2(revenue),
            "gross_profit": _q2(profit),
            "gross_margin": _ratio(profit, revenue),
            "order_count": len(valid_orders),
            "cancelled_count": len(orders) - len(valid_orders),
            "avg_order_value": _q2(_d(safe_div(revenue, len(valid_orders), default=ZERO))),
            "paid_amount": _q2(paid),
            "refund_amount": _q2(refunded),
            "outstanding_amount": _q2(outstanding),
            "max_overdue_days": max_overdue,
            "first_order_date": customer.first_order_date.isoformat() if customer.first_order_date else None,
            "last_order_date": customer.last_order_date.isoformat() if customer.last_order_date else None,
            "recency_days": customer.recency_days,
            "frequency": customer.frequency,
            "monetary": _q2(_d(customer.monetary)) if customer.monetary is not None else None,
        },
        "orders": [
            {
                "order_no": order.order_no,
                "order_date": order.order_date.isoformat() if order.order_date else None,
                "order_status": order.order_status,
                "channel": order.channel,
                "order_type": order.order_type,
                "dominant_category": order.dominant_category,
                "final_amount": _q2(_d(order.final_amount)),
                "discount": float(order.discount or 0),
                "is_on_time": order.is_on_time,
                "delay_days": order.delay_days,
                "store_name": order.store.store_name if order.store else None,
                "sales_rep_name": order.sales_rep.rep_name if order.sales_rep else None,
                "items": [
                    {
                        "product_name": item["product_name"],
                        "category": item["category"],
                        "quantity": item["quantity"],
                        "line_amount": _q2(_d(item["line_amount"])),
                        "gross_profit": _q2(_d(item["gross_profit"])),
                        "area_m2": float(item["area_m2"]) if item["area_m2"] is not None else None,
                    }
                    for item in items_by_order.get(order.id, ())
                ],
            }
            for order in orders
        ],
        "payments": [
            {
                "payment_no": payment.payment_no,
                "payment_date": payment.payment_date.isoformat() if payment.payment_date else None,
                "stage": payment.stage,
                "amount": _q2(_d(payment.amount)),
                "payment_method": payment.payment_method,
                "is_refund": payment.is_refund,
            }
            for payment in payments
        ],
        "after_sales": [
            {
                "after_no": row.after_no,
                "report_date": row.report_date.isoformat() if row.report_date else None,
                "after_type": row.after_type,
                "category": row.category,
                "cost": _q2(_d(row.cost)),
                "status": row.status,
                "handling_days": row.handling_days,
            }
            for row in after_sales
        ],
        "timeline": timeline[:50],
        "risk_flags": risk_flags,
    }

# ---------------------------------------------------------------------------
# 数据质量与模型
# ---------------------------------------------------------------------------
def get_data_quality_summary() -> dict:
    """最近一次数据质量检查的摘要。

    没有跑过检查时返回 ``{"has_run": False}``，让页面显示「尚未执行校验」
    而不是显示一堆 0，，0 会被误读成「完美通过」。
    """
    run = DQRun.objects.order_by("-created_at").first()
    if run is None:
        return {"has_run": False, "message": "尚未执行数据质量校验"}

    results = (
        DQResult.objects.filter(run=run)
        .select_related("rule")
        # 排序字段必须跟着外键改名一起改：DQResult.check 已重命名为 rule
        # （原因见 apps/dq/models.py：check 与 Django 内置的 Model.check() 冲突）。
        # 漏改一处就会让整个数据质量页走「取数失败」降级分支，而且日志里
        # 只提示 FieldError，看不出是改名没跟上的连带影响。
        .order_by("rule__order", "id")
    )
    details = []
    failed_rules = []
    warned_rules = []
    for result in results:
        item = {
            "rule_code": result.rule.rule_code if result.rule else "",
            "rule_name": result.rule.rule_name if result.rule else "",
            "category": result.rule.category if result.rule else "",
            "severity": result.rule.severity if result.rule else "",
            "status": result.status,
            "checked_rows": result.checked_rows,
            "issue_rows": result.issue_rows,
            "issue_ratio": float(result.issue_ratio or 0),
            "message": result.message,
            "sample_keys": (result.sample_keys or [])[:10],
        }
        details.append(item)
        if result.status == "fail":
            failed_rules.append(item)
        elif result.status == "warn":
            warned_rules.append(item)

    return {
        "has_run": True,
        "run_id": run.id,
        "trigger": run.trigger,
        "data_as_of": run.data_as_of.isoformat() if run.data_as_of else None,
        "created_at": run.created_at.isoformat() if getattr(run, "created_at", None) else None,
        "total_checks": run.total_checks,
        "passed": run.passed,
        "warned": run.warned,
        "failed": run.failed,
        "duration_ms": run.duration_ms,
        "is_healthy": run.is_healthy,
        "results": details,
        "failed_rules": failed_rules,
        "warned_rules": warned_rules,
    }

def get_model_overview() -> dict:
    """模型在线情况：各任务的活跃版本、评估指标与最新评分结果。"""
    versions = list(ModelVersion.objects.order_by("task", "-version"))
    active = [version for version in versions if version.is_active]
    results = (
        ScoringResult.objects.select_related("model_version").order_by("-created_at")[:200]
    )

    def _version_payload(version) -> dict:
        return {
            "task": version.task,
            "version": version.version,
            "algo": version.algo,
            "is_active": version.is_active,
            "train_period_start": version.train_period_start.isoformat()
            if version.train_period_start
            else None,
            "train_period_end": version.train_period_end.isoformat()
            if version.train_period_end
            else None,
            "sample_count": version.sample_count,
            "feature_count": version.feature_count,
            "auc": float(version.auc) if version.auc is not None else None,
            "ks": float(version.ks) if version.ks is not None else None,
            "mae": float(version.mae) if version.mae is not None else None,
            "mape": float(version.mape) if version.mape is not None else None,
            "r2": float(version.r2) if version.r2 is not None else None,
            "baseline_name": version.baseline_name,
            "baseline_mae": float(version.baseline_mae) if version.baseline_mae is not None else None,
            "baseline_auc": float(version.baseline_auc) if version.baseline_auc is not None else None,
            "feature_importance": version.feature_importance,
            "artifact_path": version.artifact_path,
            "note": version.note,
        }

    return {
        "version_count": len(versions),
        "active_count": len(active),
        "tasks": sorted({version.task for version in versions if version.task}),
        "active_versions": [_version_payload(version) for version in active],
        "all_versions": [_version_payload(version) for version in versions],
        "scoring_results": [
            {
                "task": result.model_version.task if result.model_version_id else "",
                "subject_type": result.subject_type,
                "subject_key": result.subject_key,
                "score": float(result.score) if result.score is not None else None,
                "predicted_label": result.predicted_label,
                "predicted_value": float(result.predicted_value)
                if result.predicted_value is not None
                else None,
                "top_factors": result.top_factors,
                "explanation": result.explanation,
            }
            for result in results
        ],
    }
