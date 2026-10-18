"""统一筛选参数对象。

为什么需要一个筛选对象
------------------
每个分析页面都有同一排筛选器（年份、渠道、品类、客户类型…）。
如果让每个视图自己从 ``request.GET`` 里捞参数、自己拼 ``filter()``，
会出现三个后果：
1. **口径不一致**：A 页面「本年度」含 1 月 1 日当天，B 页面不含；
2. **缓存失效**：两个页面筛同一批数据，缓存 key 却不同，白白多算一遍；
3. **注入风险**：直接把用户输入拼进 ``**kwargs``，早晚会把非法字段名带进 ORM。

:class:`AnalyticsFilters` 把这三件事收在一处：解析（含合法性校验）、
应用（统一的 ``apply_*`` 方法）、缓存 key（规范化字符串）。

核心语义
--------
**空列表 = 不限**。这是最有用的约定，也是最多人写错的地方：
``channels=[]`` 必须被理解为「全部渠道」，而不是「没有渠道」。
因此所有 ``apply_*`` 方法内部都要先判空，，写成
``if self.channels: qs = qs.filter(channel__in=self.channels)``，
不能写成无条件 ``filter(channel__in=self.channels)``（后者在空列表时
会生成 ``IN ()``，在 MySQL 上语义上「全不放行」，页面直接空白）。

取值合法性一律「忽略而非报错」：筛选器是给人用的，
用户手改 URL 里的 ``channels=xxx`` 时应该回退到不过滤，
而不是甩一个 500 错误页面。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, fields
from datetime import date, timedelta

from apps.core.constants import (
    CATEGORY_VALUES,
    CHANNEL_VALUES,
    CUSTOMER_TYPE_VALUES,
    ORDER_TYPE_VALUES,
    SEGMENT_VALUES,
)

# 期间字符串：``2025`` 或 ``2025-03``
PERIOD_PATTERN = re.compile(r"^(\d{4})(?:-(\d{1,2}))?$")

# 单值筛选字段名 → 解析用的分隔符
MULTI_VALUE_FIELDS = (
    "years",
    "channels",
    "categories",
    "customer_types",
    "order_types",
    "stores",
    "segments",
)


def _split_values(raw) -> list:
    """把 ``"2024,2025"`` / ``["2024", " 2025 "]`` / ``None`` 统一成字符串列表。

    支持逗号（含中文逗号）分隔，因为这两种写法在手工改 URL 时都会出现。
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        raw_items = list(raw)
    else:
        raw_items = str(raw).replace("，", ",").split(",")
    cleaned = []
    for item in raw_items:
        text = str(item).strip()
        if text:
            cleaned.append(text)
    return cleaned


def _filter_allowed(values: list, allowed: list) -> list:
    """保留合法取值，静默丢弃非法值。

    ``allowed`` 为空（枚举未定义）时不做校验，，不能因为一个常量没配好，
    就让所有筛选功能一起失效。
    """
    if not allowed:
        return list(values)
    allowed_set = set(allowed)
    return [value for value in values if value in allowed_set]


def _parse_date(raw) -> date | None:
    """解析 ``YYYY-MM-DD``；失败返回 ``None``（当作未提供）。"""
    if not raw:
        return None
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _month_end(year: int, month: int) -> date:
    """某月最后一天（用于把 ``period`` 转成闭区间）。"""
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


@dataclass
class AnalyticsFilters:
    """一次分析的筛选条件。

    字段语义：
    - ``period``：``"2025"``（整年）或 ``"2025-03"``（整月）；与
      ``date_from`` / ``date_to`` 同时提供时，三者取交集（更严格的那个生效）；
    - ``years``：年份列表，用于下钻「多年同月对比」这类跨年视图；
    - 其余列表字段：**空列表表示不限**。
    """

    period: str = ""
    date_from: date | None = None
    date_to: date | None = None
    years: list = field(default_factory=list)
    channels: list = field(default_factory=list)
    categories: list = field(default_factory=list)
    customer_types: list = field(default_factory=list)
    order_types: list = field(default_factory=list)
    stores: list = field(default_factory=list)
    segments: list = field(default_factory=list)

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    @classmethod
    def from_request(cls, request) -> "AnalyticsFilters":
        """从 ``request.GET`` 解析筛选条件。

        支持：
        - ``?period=2025-03`` / ``?period=2025``
        - ``?years=2024,2025&channels=抖音短视频,门店到访``（也支持重复参数
          ``?channels=a&channels=b``）
        - ``?date_from=2025-01-01&date_to=2025-06-30``
        """
        params = getattr(request, "GET", request) or {}

        def get_value(key):
            if hasattr(params, "getlist"):
                values = params.getlist(key)
                if len(values) > 1:
                    return values
                if len(values) == 1:
                    return values[0]
                return None
            return params.get(key)

        return cls.from_mapping(
            {
                "period": get_value("period"),
                "date_from": get_value("date_from"),
                "date_to": get_value("date_to"),
                "years": get_value("years"),
                "channels": get_value("channels"),
                "categories": get_value("categories"),
                "customer_types": get_value("customer_types"),
                "order_types": get_value("order_types"),
                "stores": get_value("stores"),
                "segments": get_value("segments"),
            }
        )

    @classmethod
    def from_mapping(cls, mapping: dict) -> "AnalyticsFilters":
        """从普通 dict 构造（供 API 与脚本复用，不必伪造 request）。"""
        mapping = mapping or {}

        period_raw = str(mapping.get("period") or "").strip()
        period = ""
        years = []
        match = PERIOD_PATTERN.match(period_raw) if period_raw else None
        if match:
            year_text, month_text = match.group(1), match.group(2)
            if month_text:
                month = int(month_text)
                if 1 <= month <= 12:
                    period = "{}-{:02d}".format(year_text, month)
                    years = [int(year_text)]
            else:
                period = year_text
                years = [int(year_text)]

        explicit_years = []
        for value in _split_values(mapping.get("years")):
            digits = "".join(ch for ch in value if ch.isdigit())
            if len(digits) == 4:
                explicit_years.append(int(digits))
        if explicit_years:
            years = explicit_years

        return cls(
            period=period,
            date_from=_parse_date(mapping.get("date_from")),
            date_to=_parse_date(mapping.get("date_to")),
            years=sorted(set(years)),
            channels=_filter_allowed(_split_values(mapping.get("channels")), CHANNEL_VALUES),
            categories=_filter_allowed(_split_values(mapping.get("categories")), CATEGORY_VALUES),
            customer_types=_filter_allowed(
                _split_values(mapping.get("customer_types")), CUSTOMER_TYPE_VALUES
            ),
            order_types=_filter_allowed(_split_values(mapping.get("order_types")), ORDER_TYPE_VALUES),
            stores=_split_values(mapping.get("stores")),
            segments=_filter_allowed(_split_values(mapping.get("segments")), SEGMENT_VALUES),
        )

    # ------------------------------------------------------------------
    # 派生属性
    # ------------------------------------------------------------------
    @property
    def is_empty(self) -> bool:
        """是否没设任何条件（＝全量口径）。"""
        return not any(
            [
                self.period,
                self.date_from,
                self.date_to,
                self.years,
                self.channels,
                self.categories,
                self.customer_types,
                self.order_types,
                self.stores,
                self.segments,
            ]
        )

    def resolved_date_range(self) -> tuple:
        """把 period / years / date_from / date_to 归一成一个闭区间。

        返回 ``(start, end)``，任一端为 ``None`` 表示该端不限。
        优先级与合并规则：
        - ``period`` 提供年或年月，是最常用的入口；
        - ``date_from`` / ``date_to`` 与之取交集（更严格的一端生效）；
        - 只有 ``years`` 时用「最早年的 1 月 1 日 ~ 最晚年的 12 月 31 日」。
        这样页面上的「年份 + 自定义区间」组合不会互相覆盖。
        """
        start = self.date_from
        end = self.date_to
        if self.period:
            if "-" in self.period:
                year, month = int(self.period[:4]), int(self.period[5:7])
                period_start, period_end = date(year, month, 1), _month_end(year, month)
            else:
                year = int(self.period)
                period_start, period_end = date(year, 1, 1), date(year, 12, 31)
            start = period_start if start is None else max(start, period_start)
            end = period_end if end is None else min(end, period_end)
        elif self.years:
            year_start = date(min(self.years), 1, 1)
            year_end = date(max(self.years), 12, 31)
            start = year_start if start is None else max(start, year_start)
            end = year_end if end is None else min(end, year_end)
        return start, end

    # ------------------------------------------------------------------
    # 应用筛选
    # ------------------------------------------------------------------
    def apply_orders(self, qs):
        """筛选订单 QuerySet。

        - 日期用 ``order_date__range``（闭区间，与业务同事的口径一致）；
        - 品类走 ``dominant_category``（订单主品类），与 DWS 层口径保持一致，
          这样页面筛选结果与预聚合表能对上；
        - 门店筛选支持门店编码与门店名两种写法（URL 里人工输入更直观）；
        - 空列表一律跳过，见模块 docstring 的「空列表 = 不限」。
        """
        start, end = self.resolved_date_range()
        if start is not None:
            qs = qs.filter(order_date__gte=start)
        if end is not None:
            qs = qs.filter(order_date__lte=end)
        if self.years:
            qs = qs.filter(order_year__in=self.years)
        if self.channels:
            qs = qs.filter(channel__in=self.channels)
        if self.categories:
            qs = qs.filter(dominant_category__in=self.categories)
        if self.customer_types:
            qs = qs.filter(customer_type__in=self.customer_types)
        if self.order_types:
            qs = qs.filter(order_type__in=self.order_types)
        if self.stores:
            qs = qs.filter(_store_filter_q(self.stores))
        if self.segments:
            qs = qs.filter(customer__segment__in=self.segments)
        return qs

    def apply_items(self, qs):
        """筛选订单明细 QuerySet（维度条件穿透到所属订单）。"""
        start, end = self.resolved_date_range()
        if start is not None:
            qs = qs.filter(order__order_date__gte=start)
        if end is not None:
            qs = qs.filter(order__order_date__lte=end)
        if self.years:
            qs = qs.filter(order__order_year__in=self.years)
        if self.channels:
            qs = qs.filter(order__channel__in=self.channels)
        if self.categories:
            # 明细侧的品类用明细自身的 category（比订单主品类更精确）
            qs = qs.filter(category__in=self.categories)
        if self.customer_types:
            qs = qs.filter(order__customer_type__in=self.customer_types)
        if self.order_types:
            qs = qs.filter(order__order_type__in=self.order_types)
        if self.stores:
            qs = qs.filter(_store_filter_q(self.stores, prefix="order__"))
        if self.segments:
            qs = qs.filter(order__customer__segment__in=self.segments)
        return qs

    def apply_customers(self, qs):
        """筛选客户 QuerySet。

        注意：这里按**注册日期**筛选（``register_date``），
        而订单类是按下单日期。两者不是同一件事，，「2025 年新增客户」
        与「2025 年有成交的客户」人数不同。需要后者时用 ``apply_orders``
        再对 ``customer_id`` 去重，不要在这里混用。
        """
        start, end = self.resolved_date_range()
        if start is not None:
            qs = qs.filter(register_date__gte=start)
        if end is not None:
            qs = qs.filter(register_date__lte=end)
        if self.years:
            qs = qs.filter(register_date__year__in=self.years)
        if self.channels:
            qs = qs.filter(channel__in=self.channels)
        if self.customer_types:
            qs = qs.filter(customer_type__in=self.customer_types)
        if self.segments:
            qs = qs.filter(segment__in=self.segments)
        if self.stores:
            # 客户表没有门店字段，用「该客户下过单的门店」反查
            qs = qs.filter(orders__store_id__in=_store_identifiers(self.stores)).distinct()
        return qs

    def apply_rep_perf(self, qs):
        """筛选业务员月度业绩（``DwsRepMonthly``）QuerySet。

        ``period`` 支持 ``2025``（按前缀匹配该年全部月份）与
        ``2025-03``（精确匹配单月）两种写法，，页面上年份下拉框传的是
        ``2025``，而下钻到月度时传的是 ``2025-03``，同一条链路要能连贯工作。
        """
        from django.db.models import Q

        if self.period:
            if "-" in self.period:
                qs = qs.filter(period=self.period)
            else:
                qs = qs.filter(period__startswith=self.period)
        elif self.years:
            condition = Q(period__startswith=str(self.years[0]))
            for year in self.years[1:]:
                condition |= Q(period__startswith=str(year))
            qs = qs.filter(condition)
        return qs

    # ------------------------------------------------------------------
    # 序列化与缓存
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        """规范化为可 JSON 序列化的 dict（日期转 ISO 字符串）。"""
        payload = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, date):
                payload[item.name] = value.isoformat()
            elif isinstance(value, list):
                payload[item.name] = list(value)
            else:
                payload[item.name] = value
        return payload

    def cache_key(self, prefix: str = "anl") -> str:
        """稳定的缓存 key。

        规范化规则（这是「稳定」的全部含义）：
        1. 字典按字段名排序；
        2. 列表先排序再去重（``channels=a,b`` 与 ``channels=b,a`` 是同一组条件）；
        3. 空值（``""`` / ``None`` / ``[]``）统一省略，不参与摘要；
        4. 最后取 md5 前 16 位，，足够短，也足够避免碰撞。

        这样写的好处：两个页面用同一组条件取数时命中同一份缓存，
        不会出现「同一批数据被算两次、两次结果还略有差别」的情况。
        """
        payload = {}
        for key, value in sorted(self.to_dict().items()):
            if value in ("", None, [], {}):
                continue
            if isinstance(value, list):
                payload[key] = sorted(set(str(item) for item in value))
            else:
                payload[key] = str(value)
        raw = "&".join("{}={}".format(key, payload[key]) for key in sorted(payload))
        digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]
        return "{}:{}".format(prefix, digest)

    def describe(self) -> str:
        """人类可读的条件描述，用于页面标题与导出文件名。

        导出文件名里带条件描述能省掉大量「这是哪个版本的报表」的沟通成本。
        """
        parts = []
        if self.period:
            parts.append(self.period)
        elif self.years:
            parts.append("/".join(str(year) for year in self.years))
        if self.date_from or self.date_to:
            parts.append(
                "{}~{}".format(
                    self.date_from.isoformat() if self.date_from else "",
                    self.date_to.isoformat() if self.date_to else "",
                )
            )
        for label, values in (
            ("渠道", self.channels),
            ("品类", self.categories),
            ("客户类型", self.customer_types),
            ("订单类型", self.order_types),
            ("门店", self.stores),
            ("客群", self.segments),
        ):
            if values:
                parts.append("{}:{}".format(label, "/".join(values)))
        return " ".join(parts) if parts else "全量"


def _store_identifiers(values: list) -> list:
    """门店筛选值归一：既接受 ``store_code`` 也接受 ``store_id`` 数字，统一成字符串。"""
    identifiers = []
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        identifiers.append(text)
    return identifiers


def _store_filter_q(values: list, prefix: str = ""):
    """构造「门店在给定集合内」的 Q 对象，兼容编码与名称两种写法。

    单独抽成函数而不是直接 ``filter(store__store_code__in=...)``：
    页面的门店下拉框提交的是编码，而手改 URL 的人往往写门店名（「桐乡旗舰店」），
    两种都得能用，否则筛选器会「看起来没反应」。
    """
    from django.db.models import Q

    identifiers = _store_identifiers(values)
    return Q(**{"{}store__store_code__in".format(prefix): identifiers}) | Q(
        **{"{}store__store_name__in".format(prefix): identifiers}
    )


__all__ = ["AnalyticsFilters", "MULTI_VALUE_FIELDS", "PERIOD_PATTERN"]
