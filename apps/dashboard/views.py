"""经营看板页面视图。

这些视图只做四件事：**解析筛选条件 → 调 selectors 取数 → 组装图表 option → 渲染模板**。
它们刻意不做第五件事：不写 SQL、不做 group by、不算指标口径，，
所有取数与口径都在 :mod:`apps.analytics.selectors` 里，一处口径、一处修改。

三个贯穿全文件的约定
--------------------
1. **异常兜底**：每个取数调用都包在 :func:`safe` 里，失败时把中文提示追加到
   ``context["warnings"]``，页面照常渲染。分析系统最糟糕的体验不是「数字少一个」，
   而是「点进去 500」。预聚合表（``dws_*``）是分阶段构建的，
   「表还没建好」是部署期的常态，必须能在页面上说清楚而不是崩掉。
2. **图表 option 在后端组装**：模板只负责 ``json_script`` 注入。
   图表类型、轴口径、颜色语义属于「分析表达」，跟筛选口径一样应该在服务端统一，
   散到模板里迟早会出现两个页面用两种口径画同一个指标。
3. **KPI 卡的数字在视图里格式化**：模板不写格式化逻辑，见 :func:`money` / :func:`pct`。
   金额统一按「万元 / 亿元」自适应，比率统一乘 100 显示，，这与全项目
   「比率存储 0~1、展示乘 100」的口径约定一致。
"""

from __future__ import annotations

import logging
from datetime import date

from django.contrib.auth.decorators import login_required
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.shortcuts import render

from apps.analytics.metrics import METRIC_DEFINITIONS, METRIC_MAP, METRIC_TREE_SPEC
from apps.analytics.selectors import (
    get_after_sales_analysis,
    get_cashflow_weekly,
    get_category_structure,
    get_channel_performance,
    get_churn_risk_list,
    get_cohort_matrix,
    get_customer_360,
    get_customer_segments,
    get_data_quality_summary,
    get_delivery_performance,
    get_discount_margin_relation,
    get_items,
    get_kpi_summary,
    get_model_overview,
    get_monthly_trend,
    get_profit_by_sku,
    get_profit_quadrant,
    get_receivables,
    get_region_distribution,
    get_rep_ranking,
    get_stage_bottleneck,
    get_store_comparison,
    get_valid_orders,
    get_yearly_summary,
)
from apps.analytics.services.filters import AnalyticsFilters
from apps.analytics.services.simulator import PARAM_SPEC, historical_backtest, run_simulation
from apps.core.constants import (
    AGING_BUCKET_CHOICES,
    CATEGORY_CHOICES,
    CHANNEL_CHOICES,
    CUSTOMER_TYPE_CHOICES,
    ORDER_TYPE_CHOICES,
    SEGMENT_CHOICES,
)

logger = logging.getLogger(__name__)

# 图表统一配色：与 static/css/design-system.css 的 --chart-* 变量保持同一组色值。
# 顺序即语义：主色（成交额/主序列）→ 次色 → 强调 → 四档状态色。
CHART_COLORS = ["#2F6FED", "#0F9D8C", "#D97706", "#C2410C", "#B42318", "#6B7280"]
CHART_COLORS_SOFT = ["#BBD0FA", "#A7E3DB", "#F7D9A8", "#F6C3A8", "#F2C1BC", "#D7DBE0"]


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def safe(label, func, *args, warnings=None, default=None, **kwargs):
    """安全取数：失败时记录并追加中文提示，返回 ``default``。

    为什么不用装饰器：需要把「哪一个环节失败」写进页面提示，
    装饰器拿不到这么细的粒度，而这条提示对使用者（业务同事）非常重要，，
    「渠道 ROI 取数失败」和「交付数据取数失败」要采取的措施完全不同。
    """
    try:
        return func(*args, **kwargs)
    except Exception as exc:  # pragma: no cover - 取决于预聚合表是否已构建
        logger.warning("取数失败 [%s]：%s: %s", label, type(exc).__name__, exc)
        if warnings is not None:
            warnings.append("「{}」数据暂时无法计算，已跳过该模块（原因：{}）。".format(label, type(exc).__name__))
        return default


def money(value, digits: int = 2) -> str:
    """金额格式化：自动在「元 / 万元 / 亿元」之间切换。

    门业单值从几千到几十万不等，看板上用「元」显示长串数字会让 KPI 卡失去可比性。
    """
    if value is None or value == "":
        return "-"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "-"
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    if amount >= 100000000:
        return "{}{:.2f} 亿元".format(sign, amount / 100000000)
    if amount >= 10000:
        return "{}{:,.2f} 万元".format(sign, amount / 10000)
    return "{}{:,.2f} 元".format(sign, amount)


def number(value, digits: int = 0) -> str:
    """整数/数量格式化（带千分位）。"""
    if value is None or value == "":
        return "-"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "-"
    if digits:
        return "{:,.{d}f}".format(amount, d=digits)
    return "{:,.0f}".format(amount)


def pct(value, digits: int = 1) -> str:
    """比率格式化：入参是 0~1 的小数，展示时乘 100（全项目统一约定）。"""
    if value is None or value == "":
        return "-"
    try:
        return "{:.{d}f}%".format(float(value) * 100, d=digits)
    except (TypeError, ValueError):
        return "-"


def trend_of(value, good_when_up: bool = True) -> dict:
    """把同比/环比数值转成 KPI 卡上的趋势对象。

    ``good_when_up=False`` 用于「交付周期」「售后率」这类越小越好的指标，，
    单纯按符号上色会把「周期缩短」标成红色，这是分析看板上最常见的低级错误。
    """
    if value is None or value == "":
        return {"text": "无同比", "direction": "flat", "good": None}
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return {"text": "无同比", "direction": "flat", "good": None}
    direction = "up" if ratio > 0 else ("down" if ratio < 0 else "flat")
    if ratio == 0:
        good = None
    else:
        good = (ratio > 0) if good_when_up else (ratio < 0)
    return {"text": "同比 {:+.1f}%".format(ratio * 100), "direction": direction, "good": good}


def find_segment(segments, *names):
    """在 RFM 分群结果里按「中文标签或英文编码」找一行。

    选择器返回的 ``segment`` 是中文标签（高价值客户 / 沉睡客户），
    页面曾经按英文编码 ``high_value`` / ``dormant`` 去找，永远找不到，
    结果三张 KPI 卡一直显示「-」，而页面本身不报错。
    这里同时接受两种写法，避免以后再因为改名而静默失效。
    """
    wanted = set(names)
    for row in segments or []:
        if (row.get("segment") or "") in wanted or (row.get("segment_label") or "") in wanted:
            return row
    return None


def kpi(label, value, sub="", trend=None, status="", metric_code="", note=""):
    """构造一张 KPI 卡的数据结构（模板 :file:`partials/_kpi_cards.html` 消费）。"""
    return {
        "label": label,
        "value": value,
        "sub": sub,
        "trend": trend or {"text": "", "direction": "flat", "good": None},
        "status": status,
        "metric_code": metric_code,
        "note": note,
    }


def chart(chart_id, title, option, height=340, conclusion="", drill_dimension="", drill_api="/api/v1/orders/"):
    """构造一张图表的数据结构（模板 :file:`partials/_chart.html` 消费）。

    ``drill_dimension`` 非空时，前端会把柱子/扇区变成可点击的：
    点击后用该维度的值请求 ``drill_api``，在右侧抽屉里渲染订单明细。
    下钻是「看板可信度」的关键，，业务看到一个异常柱子后的第一反应
    永远是「让我看看具体是哪几单」，给不出明细，他们就不会相信这张图。
    """
    return {
        "chart_id": chart_id,
        "chart_title": title,
        "chart_option": option,
        "chart_height": height,
        "chart_conclusion": conclusion,
        "drill_dimension": drill_dimension,
        "drill_api": drill_api,
    }


# 图表 id 片段 → 下钻维度。用命名约定推导而不是给每处调用都加参数：
# 图表 id 已经包含业务维度（chartGrowthChannel → 渠道），
# 再传一遍参数只会制造「两处不一致」的机会。
CHART_DIMENSION_HINTS = (
    ("Channel", "渠道"),
    ("Cac", "渠道"),
    ("GrowthTrend", "渠道"),
    ("Category", "品类"),
    ("Cost", "品类"),
    ("Sku", "品类"),
    ("Segment", "客群"),
    ("Value", "客群"),
    ("Retention", "客群"),
    ("Risk", "客群"),
    ("Delivery", "品类"),
    ("AfterSales", "品类"),
    ("Top", "门店"),
    ("Store", "门店"),
    ("Rep", "门店"),
    ("Region", "门店"),
    ("Discount", "品类"),
)


def _guessed_dimension(chart_id: str) -> str:
    """按图表 id 推导下钻维度（推不出来就返回空串，表示该图不可下钻）。"""
    for hint, dimension in CHART_DIMENSION_HINTS:
        if hint and hint in chart_id:
            return dimension
    return ""


def filters_from(request) -> AnalyticsFilters:
    """解析筛选条件；解析异常时退回「全量」而不是 500。"""
    try:
        return AnalyticsFilters.from_request(request)
    except Exception as exc:  # pragma: no cover
        logger.warning("筛选条件解析失败，已退回全量：%s", exc)
        return AnalyticsFilters()


# 通用表格的格式化类型：format 取值 → 格式化函数
# 格式化在视图完成（而不是在模板里选择 filter），原因见 templates/partials/_data_table.html
FORMATTERS = {
    "money": money,
    "number": lambda value: number(value, 0),
    "number1": lambda value: number(value, 1),
    "number2": lambda value: number(value, 2),
    "percent": lambda value: pct(value, 1),
    "percent2": lambda value: pct(value, 2),
    "date": lambda value: value.isoformat() if hasattr(value, "isoformat") else (value or "-"),
}


def build_table(table_id, title, columns, rows, caption="", sortable=False, description=""):
    """构造通用表格上下文（模板 :file:`partials/_data_table.html` 消费）。

    参数
    ----
    columns : list[dict]
        每列 ``{label, key, format, align}``；
        ``format`` 取 ``money/number/number1/number2/percent/percent2/date`` 之一，
        缺省按原值输出。**格式化在视图完成**，所以模板只做渲染，，
        这样金额单位与百分比口径只在一处定义（见 :data:`FORMATTERS`）。
    rows : list[dict]
        原始数据行；本函数会原地补上 ``{key}_display`` 字段供模板直接输出。
    sortable : bool
        是否渲染可点击的排序表头（点击后带 ``ordering`` 参数跳转）。
    """
    prepared = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        entry = dict(row)
        cells = []
        for column in columns or ():
            key = column.get("key")
            value = entry.get(key) if key else None
            formatter = FORMATTERS.get(column.get("format") or "")
            if formatter is not None:
                display = formatter(value)
            else:
                display = "-" if value in (None, "") else value
            if key:
                entry["{}_display".format(key)] = display
            cells.append(display)
        # 按列顺序预先排好的显示值：模板 for 一遍即可渲染整行，
        # 因此不需要自定义 template filter（少一个必须同步维护的扩展点）。
        entry["_cells"] = cells
        prepared.append(entry)

    normalized_columns = []
    for column in columns or ():
        item = dict(column)
        if not item.get("display_key"):
            item["display_key"] = "{}_display".format(item.get("key"))
        item["numeric"] = item.get("align") == "right" or item.get("format") in (
            "money", "number", "number1", "number2", "percent", "percent2",
        )
        sort_key = item.get("sort_key")
        if sort_key:
            item["desc_key"] = "-{}".format(sort_key)
        normalized_columns.append(item)

    # 模板里把「列定义」与「行取值」配对渲染：用一个 zipped 列表代替自定义 filter
    zipped_rows = [
        {"pairs": list(zip(normalized_columns, row.get("_cells") or []))}
        for row in prepared
    ]

    return {
        "table_id": table_id,
        "table_title": title,
        "table_caption": caption or title,
        "table_description": description,
        "columns": normalized_columns,
        "rows": prepared,
        "zipped_rows": zipped_rows,
        "sortable": sortable,
    }


def _dump_choices(choices) -> list:
    """把 ``[(key, 中文), ...]`` 或 ``[中文, ...]`` 统一成下拉选项结构。

    这里有一个关键约束：**页面提交的值必须能被筛选器认出来**。
    ``constants`` 有两种可能的形态，，``[(英文 key, 中文 label)]`` 与
    ``[中文值]``，，而 :class:`AnalyticsFilters` 只接受它内部枚举里存在的值。
    因此这里的归一规则是：

    - 形如 ``(key, label)`` 的二元组：``value=key``（英文，筛选器认它）、``label=label``；
    - 纯字符串：``value=label=字符串``（中文枚举的写法）。

    这样无论是哪一种常量形态，页面都是「显示中文、提交筛选器认识的值」，
    不会出现「下拉里是中文、筛选器全丢弃、用户以为筛选没生效」的静默失效。
    """
    options = []
    for item in choices or ():
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            options.append({"value": str(item[0]), "label": str(item[1])})
        else:
            options.append({"value": str(item), "label": str(item)})
    return options


def _filter_options() -> dict:
    """各筛选维度的可选值（来自 constants，门店来自数据库）。

    统一成 ``{"value": 提交值, "label": 中文}``：页面提交的必须是
    :class:`AnalyticsFilters` 认识的值，展示的必须是中文。
    """
    options = {
        "years": [],
        "channels": _dump_choices(CHANNEL_CHOICES),
        "categories": _dump_choices(CATEGORY_CHOICES),
        "customer_types": _dump_choices(CUSTOMER_TYPE_CHOICES),
        "order_types": _dump_choices(ORDER_TYPE_CHOICES),
        "segments": _dump_choices(SEGMENT_CHOICES),
        "stores": [],
        "periods": [],
    }
    current_year = date.today().year
    years = list(range(2023, max(current_year, 2026) + 1))
    options["years"] = [{"value": str(year), "label": "{} 年".format(year)} for year in years]
    options["periods"] = [
        {"value": "{}".format(year), "label": "{} 年".format(year)} for year in reversed(years)
    ]
    try:
        from apps.masterdata.models import Store

        options["stores"] = [
            {"value": row.store_code or str(row.id), "label": row.store_name}
            for row in Store.objects.all().order_by("store_code")[:50]
        ]
    except Exception as exc:  # pragma: no cover - 门店表可能还没建
        logger.warning("门店列表读取失败（不影响页面渲染）：%s", exc)
    return options


#: 多选筛选字段 → 表单字段名。用 Django Form 而不是手写 HTML 的复选框组，
#: 是因为「回填已选值」这件事手写极易出错（模板里做 ``in`` 判断拿不到干净的
#: 字符串列表），交给 ``forms.MultipleChoiceField`` 一次解决。
FILTER_FORM_FIELDS = {
    "years": "years",
    "channels": "channels",
    "categories": "categories",
    "customer_types": "customer_types",
    "order_types": "order_types",
    "stores": "stores",
    "segments": "segments",
}


def _build_filter_form(filters, options):
    """构造筛选条的回填表单（永远返回可用对象，失败时退化为空表单）。"""
    from django import forms

    class ChoiceListField(forms.MultipleChoiceField):
        """选项由视图动态下发（门店来自数据库），因此不做服务端取值校验。

        校验留在 :class:`AnalyticsFilters` 里（非法值静默丢弃），
        表单只负责「把已选值渲染成勾选状态」这一件事。
        """

        def validate(self, value):
            return None

    class FilterForm(forms.Form):
        years = ChoiceListField(required=False)
        channels = ChoiceListField(required=False)
        categories = ChoiceListField(required=False)
        customer_types = ChoiceListField(required=False)
        order_types = ChoiceListField(required=False)
        stores = ChoiceListField(required=False)
        segments = ChoiceListField(required=False)

    try:
        return FilterForm(
            initial={
                "years": [str(year) for year in (filters.years or [])],
                "channels": list(filters.channels or []),
                "categories": list(filters.categories or []),
                "customer_types": list(filters.customer_types or []),
                "order_types": list(filters.order_types or []),
                "stores": list(filters.stores or []),
                "segments": list(filters.segments or []),
            }
        )
    except Exception as exc:  # pragma: no cover - 绝不因筛选条构造失败而 500
        logger.warning("筛选表单构造失败，已退化为空筛选条：%s", exc)

        class EmptyForm(forms.Form):
            pass

        return EmptyForm()


def _filter_bar_context(filters) -> dict:
    """筛选条所需的全部上下文（选项 + 已选勾选态）。"""
    options = _filter_options()
    form = _build_filter_form(filters, options)
    selected = {key: set(str(item) for item in (value or [])) for key, value in form.initial.items()}

    def _mark(field_name):
        marked = []
        for option in options.get(field_name, []) or []:
            item = dict(option)
            item["selected"] = str(item["value"]) in selected.get(field_name, set())
            marked.append(item)
        return marked

    return {
        "options": options,
        "selected": selected,
        "years_options": _mark("years"),
        "channel_options": _mark("channels"),
        "category_options": _mark("categories"),
        "customer_type_options": _mark("customer_types"),
        "order_type_options": _mark("order_types"),
        "store_options": _mark("stores"),
        "segment_options": _mark("segments"),
    }


def _page(request, template, page_title, page_subtitle, context=None, filters=None, current_module=""):
    """所有页面视图的统一出口：补齐公共上下文并渲染。

    统一出口的价值在于：分页、筛选、权限、警告这四个横切关注点只写一遍，
    新增页面不可能忘记传 ``filters`` 或 ``filter_options``。
    """
    ctx = dict(context or {})
    resolved_filters = filters if filters is not None else filters_from(request)
    ctx.setdefault("warnings", [])

    # 图表补全：给没显式声明下钻维度的图表按 id 推导一个，
    # 保证「点击柱子看明细」在绝大多数图上开箱可用。
    charts = []
    for index, item in enumerate(ctx.get("charts") or [], start=1):
        entry = dict(item)
        if not entry.get("drill_dimension"):
            entry["drill_dimension"] = _guessed_dimension(str(entry.get("chart_id") or ""))
        # 图表序号：模板要做「用 json_script 注入 option」与「容器引用同一个 id」，
        # 而 Django 模板无法把两个变量拼接成新变量名，所以由视图给出序号，
        # 模板用 chart-<序号> / chart-<序号>-option 这一对固定命名约定。
        entry["chart_index"] = index
        charts.append(entry)

    ctx.update(
        {
            "page_title": page_title,
            "page_subtitle": page_subtitle,
            "filters": resolved_filters,
            "filter_bar": _filter_bar_context(resolved_filters),
            "filter_query": _query_string(request),
            "current_module_key": current_module,
            # 注意：这两个键必须用 setdefault 语义，不能无条件赋空值。
            # 原实现写死 "metric_definitions": []，会把视图在调用 _page 之前
            # 用 _attach_metric_defs 算好的口径字典直接覆盖成空 —— 页面拿不到
            # 口径数据，模板里再去取值就变成 500。横切默认值只能「缺则补」，
            # 不能「无条件覆盖」，否则每个页面的局部计算结果都会神秘消失。
            "metric_definitions": ctx.get("metric_definitions", []),
            "metric_map": ctx.get("metric_map", {}),
            "conclusions": ctx.get("conclusions", []),
            "charts": charts,
            "chart_colors": CHART_COLORS,
            "chart_colors_soft": CHART_COLORS_SOFT,
            "data_version": _data_version_token(),
        }
    )
    return render(request, template, ctx)


def _data_version_token() -> str:
    """页脚展示的数据版本 token（与接口 ``meta.data_version`` 同一实现）。

    页面与接口用同一个函数取版本号：这样「页面截图」与「接口返回」能被对齐到
    同一批数据，排查「两个数字不一致」时不用先怀疑数据是不是同一份。
    """
    try:
        from apps.api.utils import data_version_token

        return data_version_token()
    except Exception as exc:  # pragma: no cover - 兜底：版本信息不该阻断渲染
        logger.warning("数据版本读取失败：%s", exc)
        return "unknown"


def _query_string(request) -> str:
    """当前查询串（去掉 page / ordering），供分页与排序链接复用。"""
    get = getattr(request, "GET", None)
    if get is None:
        return ""
    try:
        params = get.copy()
    except Exception:  # pragma: no cover
        return ""
    for key in ("page", "ordering"):
        params.pop(key, None)
    encoded = params.urlencode()
    return encoded


def _attach_metric_defs(ctx, codes, warnings):
    """按指标编码补口径说明：既给出列表（表格用），也给出字典（KPI 卡直接取用）。

    ``METRIC_MAP`` 是内存常量，不会抛异常；这里仍然做一次防御，
    因为口径字典一旦有脏数据，全局模板都会受影响。
    """
    matched = _matched_codes(codes)
    defs = [METRIC_MAP[code] for code in matched]
    ctx["metric_definitions"] = defs
    ctx["metric_map"] = {code: METRIC_MAP[code] for code in matched}
    if codes and not defs:
        warnings.append("部分指标口径未在指标字典中登记，卡片提示可能不完整。")
    return ctx


def _matched_codes(codes) -> list:
    """过滤出确实存在的指标编码（模板里按 code 取定义用）。"""
    return [code for code in (codes or ()) if code in METRIC_MAP]


def _metric_map(codes) -> dict:
    """``{指标编码: 口径定义}`` ，， 供 KPI 卡与表格直接 ``{{ metric_map.M_OV_001.name }}`` 取用。

    为什么不在模板里循环匹配：模板里做 ``{% if metric.code == item.metric_code %}``
    属于「在视图层就该完成的字典查找」，循环匹配既慢又容易写成嵌套判断。
    这里在视图侧构建好字典，模板只做一次属性访问。
    """
    return {code: METRIC_MAP[code] for code in _matched_codes(codes)}


# ---------------------------------------------------------------------------
# 一、经营总览
# ---------------------------------------------------------------------------
@login_required
def overview(request):
    """经营总览：今年到底赚了多少，比去年好还是差？

    这是整个系统的第一屏，回答的是老板每天最关心的三个问题：
    成交额完成得怎么样、毛利有没有守住、钱收回来了多少。
    """
    filters = filters_from(request)
    warnings = []
    kpi_data = safe("经营总览指标", get_kpi_summary, filters, warnings=warnings, default={}) or {}

    revenue = kpi_data.get("revenue")
    gross_profit = kpi_data.get("gross_profit")
    gross_margin = kpi_data.get("gross_margin")
    collection_rate = kpi_data.get("collection_rate")
    target_achievement = kpi_data.get("target_achievement")
    on_time_rate = kpi_data.get("on_time_rate")

    kpis = [
        kpi("成交额", money(revenue), "有效订单终价合计（不含已取消）", trend_of(kpi_data.get("yoy")), "primary", "M_OV_001"),
        kpi("订单量", "{} 单".format(number(kpi_data.get("order_count"))), "剔除已取消订单", trend_of(kpi_data.get("qoq"), good_when_up=True), "", "M_OV_002"),
        kpi("客单价", money(kpi_data.get("avg_order_value")), "成交额 ÷ 订单量", None, "", "M_OV_003"),
        kpi("毛利额", money(gross_profit), "成交额 − 五项成本", trend_of(kpi_data.get("yoy")), "primary", "M_OV_004"),
        kpi("毛利率", pct(gross_margin), "毛利额 ÷ 成交额", None,
            "good" if (gross_margin or 0) >= 0.30 else "warn", "M_OV_005"),
        kpi("目标达成率", pct(target_achievement), "成交额 ÷ 年度目标值",
            None, "good" if (target_achievement or 0) >= 1 else "danger", "M_OV_006"),
        kpi("回款额", money(kpi_data.get("collection_amount")), "按付款日归集，非退款", None, "", "M_CW_001"),
        kpi("准时交付率", pct(on_time_rate), "分母仅含已交付订单", None,
            "good" if (on_time_rate or 0) >= 0.9 else "warn", "M_DS_001"),
    ]

    trend = safe("月度趋势", get_monthly_trend, filters, "revenue", "month", warnings=warnings, default=[]) or []
    labels = [row.get("period") for row in trend]
    option_trend = {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["成交额", "毛利率"]},
        "grid": {"left": 60, "right": 60, "top": 50, "bottom": 40},
        "xAxis": {"type": "category", "data": labels},
        "yAxis": [
            {"type": "value", "name": "成交额(元)"},
            {"type": "value", "name": "毛利率", "axisLabel": {"formatter": "{value}%"}, "max": 60},
        ],
        "series": [
            {
                "name": "成交额",
                "type": "bar",
                "data": [float(row.get("revenue") or 0) for row in trend],
                "itemStyle": {"color": CHART_COLORS[0]},
            },
            {
                "name": "毛利率",
                "type": "line",
                "yAxisIndex": 1,
                "smooth": True,
                "data": [round(float(row.get("gross_margin") or 0) * 100, 2) for row in trend],
                "itemStyle": {"color": CHART_COLORS[2]},
            },
        ],
    }

    yearly = safe("年度汇总", get_yearly_summary, filters, warnings=warnings, default=[]) or []
    option_yearly = {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["成交额", "毛利额"]},
        "grid": {"left": 60, "right": 30, "top": 50, "bottom": 40},
        "xAxis": {"type": "category", "data": [row.get("year") for row in yearly]},
        "yAxis": {"type": "value", "name": "元"},
        "series": [
            {"name": "成交额", "type": "bar", "data": [float(row.get("revenue") or 0) for row in yearly],
             "itemStyle": {"color": CHART_COLORS[0]}},
            {"name": "毛利额", "type": "bar", "data": [float(row.get("gross_profit") or 0) for row in yearly],
             "itemStyle": {"color": CHART_COLORS[1]}},
        ],
    }

    channels = safe("渠道绩效", get_channel_performance, filters, warnings=warnings, default=[]) or []
    option_channel = {
        "tooltip": {"trigger": "item", "formatter": "{b}: {c} 元 ({d}%)"},
        "legend": {"bottom": 0},
        "series": [
            {
                "name": "成交额结构",
                "type": "pie",
                "radius": ["45%", "70%"],
                "data": [
                    {"name": row.get("channel_label") or row.get("channel"), "value": float(row.get("revenue") or 0)}
                    for row in channels
                ],
            }
        ],
    }

    region = safe("区域分布", get_region_distribution, filters, warnings=warnings, default=[]) or []
    region_table = build_table(
        "tblOverviewRegion",
        "区域分布 Top 15（省 × 市）",
        [
            {"label": "省 / 市", "key": "region_label"},
            {"label": "成交额", "key": "revenue", "format": "money"},
            {"label": "收入占比", "key": "revenue_share", "format": "percent"},
            {"label": "订单数", "key": "order_count", "format": "number"},
            {"label": "客户数", "key": "customer_count", "format": "number"},
            {"label": "客单价", "key": "avg_order_value", "format": "money"},
        ],
        [
            {
                "region_label": row.get("region_label"),
                "revenue": row.get("revenue"),
                "revenue_share": row.get("revenue_share"),
                "order_count": row.get("order_count"),
                "customer_count": row.get("customer_count"),
                "avg_order_value": row.get("avg_order_value"),
            }
            for row in region[:15]
        ],
        caption="按成交额排序的区域分布，用于判断市场集中度与拓展空间",
        description="区域集中度高（单市占比超过一半）时，优先在邻近县市布点，而不是贸然跨省。",
    )
    yearly_table = build_table(
        "tblOverviewYear",
        "年度汇总",
        [
            {"label": "年份", "key": "year"},
            {"label": "成交额", "key": "revenue", "format": "money"},
            {"label": "毛利额", "key": "gross_profit", "format": "money"},
            {"label": "毛利率", "key": "gross_margin", "format": "percent"},
            {"label": "订单数", "key": "order_count", "format": "number"},
            {"label": "客户数", "key": "customer_count", "format": "number"},
            {"label": "客单价", "key": "avg_order_value", "format": "money"},
            {"label": "同比", "key": "yoy", "format": "percent"},
        ],
        yearly,
        caption="年度经营汇总，含成交额、毛利与同比",
        description="同比为「与去年同期同天数窗口」比较，不是简单的上一行相除。",
    )

    conclusions = _overview_conclusions(kpi_data, channels, trend, warnings)

    ctx = {
        "warnings": warnings,
        "kpis": kpis,
        "kpi_raw": kpi_data,
        "charts": [
            chart("chartOverviewTrend", "月度成交额与毛利率", option_trend, 360,
                  "看柱子高度是否在抬升、折线是否在同步抬升：柱高线平的组合通常意味着在靠降价换量。"),
            chart("chartOverviewChannel", "渠道成交结构", option_channel, 340,
                  "任一渠道占比超过 50% 就是集中度风险，需要提前布第二增长曲线。"),
            chart("chartOverviewYearly", "年度成交额与毛利额", option_yearly, 320,
                  "毛利额增速持续低于成交额增速，说明增长是「不赚钱的增长」。"),
        ],
        "year_table": yearly_table,
        "region_table": region_table,
        "conclusions": conclusions,
    }
    _attach_metric_defs(ctx, _matched_codes(
        ["M_OV_001", "M_OV_002", "M_OV_003", "M_OV_004", "M_OV_005", "M_OV_006", "M_CW_001", "M_DS_001"]
    ), warnings)
    return _page(request, "dashboard/overview.html", "经营总览",
                 "今年到底赚了多少？比去年好还是差？", ctx, filters, "overview")


def _overview_conclusions(kpi_data, channels, trend, warnings) -> list:
    """生成总览页结论：每条都带「所以建议……」的落地动作。"""
    conclusions = []
    yoy = kpi_data.get("yoy")
    margin = kpi_data.get("gross_margin")
    achievement = kpi_data.get("target_achievement")

    if yoy is not None:
        level = "good" if float(yoy) >= 0 else "danger"
        conclusions.append(
            {
                "title": "成交额同比{}".format("增长" if float(yoy) >= 0 else "下滑"),
                "body": "成交额同比{:+.1f}%。{}{}".format(
                    float(yoy) * 100,
                    "增长主要看是否由老客复购驱动，，若靠拉新堆量，获客成本会同步上升。" if float(yoy) >= 0
                    else "下滑需要先分清是「订单量掉了」还是「客单价掉了」，两者的处方完全不同。",
                    "所以建议：立刻按渠道与品类拆一次成交额，定位是哪个环节出的问题。",
                ),
                "level": level,
            }
        )

    if margin is not None:
        margin_value = float(margin)
        conclusions.append(
            {
                "title": "毛利率 {}，{}行业制造企业的合理区间".format(
                    pct(margin_value), "高于" if margin_value >= 0.30 else "低于"
                ),
                "body": "当前毛利率 {}，材料成本占比与折扣率是它的两个主要抓手。"
                "所以建议：{}".format(
                    pct(margin_value),
                    "把毛利率拆到品类与 SKU，找出拉低整体毛利的具体产品并谈材料价。"
                    if margin_value < 0.30
                    else "把高毛利品类（如铜门、铸铝门）的产能与推荐优先级保住宅。",
                ),
                "level": "good" if margin_value >= 0.30 else "warn",
            }
        )

    if achievement is not None:
        conclusions.append(
            {
                "title": "目标达成率 {}".format(pct(achievement)),
                "body": "目标值来自 SalesTarget（全公司 / 成交额口径），不含渠道与个人目标。"
                "所以建议：{}{}".format(
                    "把剩余目标按月份拆到渠道与业务员，避免年底集中冲量压价。"
                    if float(achievement) < 1
                    else "提前锁定下一年目标基数，把今年的高毛利结构固化进目标里。",
                    "注意 12 月冲量订单会拉低毛利率并推高次年一季度回款压力。" if float(achievement) < 1 else "",
                ),
                "level": "good" if float(achievement) >= 1 else "warn",
            }
        )

    if channels:
        top = max(channels, key=lambda row: float(row.get("revenue") or 0))
        share = top.get("revenue_share") or 0
        conclusions.append(
            {
                "title": "渠道集中度：{} 占成交额 {}".format(top.get("channel_label") or top.get("channel"), pct(share)),
                "body": "渠道占比是「增长来源」也是「依赖度」。单一渠道超过 50% 时，"
                "该渠道的规则变化（平台流量政策、展会停办）会直接冲击营收。"
                "所以建议：{}".format(
                    "把该渠道的获客成本按月监控起来，同时用老客户转介做低成本对冲。"
                    if float(share) >= 0.5
                    else "在保持主力渠道的同时，给低占比高 ROI 的渠道加预算试点。"
                ),
                "level": "warn" if float(share) >= 0.5 else "info",
            }
        )

    if not conclusions:
        conclusions.append(
            {
                "title": "暂无可分析数据",
                "body": "当前筛选条件下没有有效订单。所以建议：放宽时间范围或清空筛选条件后重试。",
                "level": "info",
            }
        )
    return conclusions[:4]


# ---------------------------------------------------------------------------
# 二、增长与渠道
# ---------------------------------------------------------------------------
@login_required
def growth(request):
    """增长与渠道：客户是从哪儿来的？哪个渠道真正值得加钱？"""
    filters = filters_from(request)
    warnings = []
    channels = safe("渠道绩效", get_channel_performance, filters, warnings=warnings, default=[]) or []
    kpi_data = safe("经营总览指标", get_kpi_summary, filters, warnings=warnings, default={}) or {}
    trend = safe("月度趋势", get_monthly_trend, filters, "revenue", "month", warnings=warnings, default=[]) or []

    total_revenue = sum(float(row.get("revenue") or 0) for row in channels)
    total_cost = sum(float(row.get("marketing_cost") or 0) for row in channels)
    total_profit = sum(float(row.get("gross_profit") or 0) for row in channels)
    total_new = sum(int(row.get("new_customer_count") or 0) for row in channels)
    best_roi = max(channels, key=lambda row: float(row.get("roi") or 0), default=None)
    worst_cac = max(channels, key=lambda row: float(row.get("cost_per_customer") or 0), default=None)

    kpis = [
        kpi("渠道成交额", money(total_revenue), "全部渠道合计", trend_of(kpi_data.get("yoy")), "primary", "M_GR_005"),
        kpi("新增客户数", "{} 人".format(number(total_new)), "按首单日期判定为新增", None, "", "M_GR_001"),
        kpi("营销投入估算", money(total_cost), "新增客户 × 3 线索 × 渠道单价", None, "", "M_GR_002"),
        kpi("整体渠道 ROI", "{:.2f}".format(total_profit / total_cost) if total_cost else "-",
            "渠道毛利 ÷ 营销费用", None, "good" if total_cost and total_profit / total_cost >= 3 else "warn", "M_GR_004"),
        kpi("最优渠道", (best_roi or {}).get("channel_label") or "-",
            "ROI {:.2f}".format(float((best_roi or {}).get("roi") or 0)) if best_roi else "无数据", None, "good", "M_GR_004"),
        kpi("最贵获客渠道", (worst_cac or {}).get("channel_label") or "-",
            "单客 {} 元".format(number((worst_cac or {}).get("cost_per_customer"), 0)) if worst_cac else "无数据",
            None, "danger", "M_GR_003"),
    ]

    option_channel = {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "legend": {"data": ["成交额", "毛利额", "渠道ROI"]},
        "grid": {"left": 60, "right": 60, "top": 50, "bottom": 60},
        "xAxis": {"type": "category", "data": [row.get("channel_label") or row.get("channel") for row in channels]},
        "yAxis": [
            {"type": "value", "name": "元"},
            {"type": "value", "name": "ROI"},
        ],
        "series": [
            {"name": "成交额", "type": "bar", "data": [float(row.get("revenue") or 0) for row in channels],
             "itemStyle": {"color": CHART_COLORS[0]}},
            {"name": "毛利额", "type": "bar", "data": [float(row.get("gross_profit") or 0) for row in channels],
             "itemStyle": {"color": CHART_COLORS[1]}},
            {"name": "渠道ROI", "type": "line", "yAxisIndex": 1, "smooth": True,
             "data": [round(float(row.get("roi") or 0), 2) for row in channels],
             "itemStyle": {"color": CHART_COLORS[2]}},
        ],
    }

    option_cac = {
        "tooltip": {"trigger": "axis"},
        "grid": {"left": 70, "right": 30, "top": 40, "bottom": 60},
        "xAxis": {"type": "category", "data": [row.get("channel_label") or row.get("channel") for row in channels]},
        "yAxis": {"type": "value", "name": "元/人"},
        "series": [
            {"name": "单客获客成本", "type": "bar",
             "data": [float(row.get("cost_per_customer") or 0) for row in channels],
             "itemStyle": {"color": CHART_COLORS[3]},
             "label": {"show": True, "position": "top", "formatter": "{c}"}},
        ],
    }

    option_trend = {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["成交额", "去年同期"]},
        "grid": {"left": 60, "right": 30, "top": 50, "bottom": 40},
        "xAxis": {"type": "category", "data": [row.get("period") for row in trend]},
        "yAxis": {"type": "value", "name": "元"},
        "series": [
            {"name": "成交额", "type": "line", "smooth": True,
             "data": [float(row.get("revenue") or 0) for row in trend], "itemStyle": {"color": CHART_COLORS[0]}},
            {"name": "去年同期", "type": "line", "smooth": True, "lineStyle": {"type": "dashed"},
             "data": [float(row.get("previous_revenue") or 0) for row in trend],
             "itemStyle": {"color": CHART_COLORS[5]}},
        ],
    }

    channel_table = build_table(
        "tblGrowthChannel",
        "渠道绩效明细",
        [
            {"label": "渠道", "key": "channel"},
            {"label": "成交额", "key": "revenue", "format": "money"},
            {"label": "收入占比", "key": "revenue_share", "format": "percent"},
            {"label": "订单数", "key": "order_count", "format": "number"},
            {"label": "客单价", "key": "avg_order_value", "format": "money"},
            {"label": "毛利率", "key": "gross_margin", "format": "percent"},
            {"label": "新增客户", "key": "new_customer_count", "format": "number"},
            {"label": "单客获客成本", "key": "cost_per_customer", "format": "money"},
            {"label": "渠道 ROI", "key": "roi", "format": "number2"},
            {"label": "平均折扣率", "key": "discount_rate", "format": "percent"},
        ],
        [
            {
                "channel": row.get("channel_label") or row.get("channel"),
                "revenue": row.get("revenue"),
                "revenue_share": row.get("revenue_share"),
                "order_count": row.get("order_count"),
                "avg_order_value": row.get("avg_order_value"),
                "gross_margin": row.get("gross_margin"),
                "new_customer_count": row.get("new_customer_count"),
                "cost_per_customer": row.get("cost_per_customer"),
                "roi": row.get("roi"),
                "discount_rate": row.get("discount_rate"),
            }
            for row in channels
        ],
        caption="各渠道的成交额、毛利、获客成本与 ROI 对比",
        description="营销费用为估算值（新增客户 × 3 条线索 × 渠道线索单价，见 constants）；真实系统应接渠道费用台账。",
    )

    conclusions = _growth_conclusions(channels, total_cost, total_profit)
    ctx = {
        "warnings": warnings,
        "kpis": kpis,
        "channel_table": channel_table,
        "charts": [
            chart("chartGrowthChannel", "渠道成交额 / 毛利额 / ROI", option_channel, 380,
                  "ROI 折线低于柱高的渠道，是在用毛利换规模。"),
            chart("chartGrowthCac", "各渠道单客获客成本", option_cac, 320,
                  "这张图决定预算往哪儿挪：成本高而 ROI 低的渠道应优先被削减。"),
            chart("chartGrowthTrend", "成交额与去年同期对比", option_trend, 320,
                  "两条线的开口方向就是增长质量：开口扩大说明增长在加速。"),
        ],
        "conclusions": conclusions,
    }
    _attach_metric_defs(ctx, _matched_codes(["M_GR_001", "M_GR_002", "M_GR_003", "M_GR_004", "M_GR_005"]), warnings)
    return _page(request, "dashboard/growth.html", "增长与渠道",
                 "客户是从哪儿来的？哪个渠道真正值得加钱？", ctx, filters, "growth")


def _growth_conclusions(channels, total_cost, total_profit) -> list:
    conclusions = []
    if not channels:
        return [{"title": "暂无渠道数据", "body": "当前条件下没有渠道成交记录。所以建议：确认订单数据是否已导入。", "level": "info"}]

    high_cost_low_roi = [
        row for row in channels
        if float(row.get("cost_per_customer") or 0) > 0 and float(row.get("roi") or 0) < 3
    ]
    if high_cost_low_roi:
        names = "、".join((row.get("channel_label") or row.get("channel")) for row in high_cost_low_roi[:3])
        conclusions.append(
            {
                "title": "高成本低产出的渠道：{}".format(names),
                "body": "这些渠道的单客获客成本偏高而 ROI 低于 3（即每投入 1 元带来不到 3 元毛利）。"
                "所以建议：把它们的预算按 30% 幅度削减，转移给 ROI 最高的渠道做 A/B 测试，两周后对比单客成本。",
                "level": "warn",
            }
        )

    best = max(channels, key=lambda row: float(row.get("roi") or 0))
    conclusions.append(
        {
            "title": "最优渠道：{}（ROI {:.2f}）".format(best.get("channel_label") or best.get("channel"),
                                                float(best.get("roi") or 0)),
            "body": "该渠道每 1 元营销费用带来 {:.2f} 元毛利。所以建议：在产能允许的前提下把它的占比从 {} 提升到 {} 以上，"
            "但要同时监控交付准时率，，渠道放量最常见的事故是交付跟不上导致口碑反噬。".format(
                float(best.get("roi") or 0), pct(best.get("revenue_share")), pct(min(float(best.get("revenue_share") or 0) + 0.1, 0.9))
            ),
            "level": "good",
        }
    )

    referral = [row for row in channels if (row.get("channel") or "") in ("老客户转介", "referral")]
    if referral and float(referral[0].get("revenue_share") or 0) < 0.15:
        conclusions.append(
            {
                "title": "老客户转介占比仅 {}".format(pct(referral[0].get("revenue_share"))),
                "body": "转介是定制门行业成本最低的获客方式（本渠道每元获客产出最高）。"
                "所以建议：给已交付满 6 个月且准时交付的客户发一次转介激励，把这部分占比提上来。",
                "level": "info",
            }
        )

    if total_cost > 0:
        conclusions.append(
            {
                "title": "营销费用与毛利的关系",
                "body": "整体 ROI 为 {:.2f}（营销费用估算 {}）。营销费用是**估算值**：按「新增客户 × 3 条线索 × 渠道单价」推算，"
                "真实系统应接入渠道费用表。所以建议：把月度渠道费用真实台账补进系统，再据此做渠道预算分配。".format(
                    total_profit / total_cost, money(total_cost)
                ),
                "level": "info",
            }
        )
    return conclusions[:4]


# ---------------------------------------------------------------------------
# 三、品类与利润
# ---------------------------------------------------------------------------
@login_required
def profit(request):
    """品类与利润：哪类门真正在赚钱？折扣到底让掉了多少毛利？"""
    filters = filters_from(request)
    warnings = []
    categories = safe("品类结构", get_category_structure, filters, warnings=warnings, default=[]) or []
    sku_payload = safe("SKU 毛利排名", get_profit_by_sku, filters, 20, warnings=warnings, default={}) or {}
    quadrant = safe("利润四象限", get_profit_quadrant, filters, warnings=warnings, default={}) or {}
    discount_rows = safe("折扣与毛利关系", get_discount_margin_relation, filters, warnings=warnings, default=[]) or []
    kpi_data = safe("经营总览指标", get_kpi_summary, filters, warnings=warnings, default={}) or {}

    total_revenue = sum(float(row.get("revenue") or 0) for row in categories)
    total_profit = sum(float(row.get("gross_profit") or 0) for row in categories)
    total_material = sum(float(row.get("material_cost") or 0) for row in categories)
    total_cost = sum(float(row.get("total_cost") or 0) for row in categories)
    best_margin = max(categories, key=lambda row: float(row.get("gross_margin") or 0), default=None)
    worst_margin = min(
        [row for row in categories if row.get("gross_margin") is not None],
        key=lambda row: float(row.get("gross_margin") or 0), default=None,
    )
    cr3 = max((float(row.get("cr3_basis") or 0) for row in categories), default=0.0)

    kpis = [
        kpi("品类成交额", money(total_revenue), "明细行金额合计，折前（与首页成交额口径不同）", trend_of(kpi_data.get("yoy")), "primary", "M_CP_001"),
        kpi("品类毛利额", money(total_profit), "成交额 − 五项成本", None, "primary", "M_OV_004"),
        kpi("整体毛利率", pct(kpi_data.get("gross_margin")), "毛利额 ÷ 成交额", None,
            "good" if (kpi_data.get("gross_margin") or 0) >= 0.30 else "warn", "M_OV_005"),
        kpi("材料成本占成本", pct(total_material / total_cost if total_cost else None),
            "材料成本 ÷ 五项成本合计，是毛利率最大的敏感项", None, "warn", "M_CP_009"),
        kpi("毛利率最高品类", (best_margin or {}).get("category_label") or "-",
            pct((best_margin or {}).get("gross_margin")) if best_margin else "无数据", None, "good", ""),
        kpi("毛利率最低品类", (worst_margin or {}).get("category_label") or "-",
            pct((worst_margin or {}).get("gross_margin")) if worst_margin else "无数据", None, "danger", ""),
    ]

    labels = [row.get("category_label") or row.get("category") for row in categories]
    option_structure = {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "legend": {"data": ["成交额", "毛利额", "累计占比"]},
        "grid": {"left": 70, "right": 60, "top": 50, "bottom": 60},
        "xAxis": {"type": "category", "data": labels, "axisLabel": {"interval": 0, "rotate": 20}},
        "yAxis": [
            {"type": "value", "name": "元"},
            {"type": "value", "name": "累计占比", "max": 100, "axisLabel": {"formatter": "{value}%"}},
        ],
        "series": [
            {"name": "成交额", "type": "bar", "data": [float(row.get("revenue") or 0) for row in categories],
             "itemStyle": {"color": CHART_COLORS[0]}},
            {"name": "毛利额", "type": "bar", "data": [float(row.get("gross_profit") or 0) for row in categories],
             "itemStyle": {"color": CHART_COLORS[1]}},
            {"name": "累计占比", "type": "line", "yAxisIndex": 1, "smooth": False,
             "data": _cumulative_share(categories),
             "itemStyle": {"color": CHART_COLORS[2]}},
        ],
    }

    option_cost = {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "legend": {"data": ["材料", "人工", "制造费用", "物流", "安装"]},
        "grid": {"left": 70, "right": 30, "top": 50, "bottom": 60},
        "xAxis": {"type": "category", "data": labels, "axisLabel": {"interval": 0, "rotate": 20}},
        "yAxis": {"type": "value", "name": "成本(元)"},
        "series": [
            {"name": "材料", "type": "bar", "stack": "cost",
             "data": [float(row.get("material_cost") or 0) for row in categories], "itemStyle": {"color": CHART_COLORS[0]}},
            {"name": "人工", "type": "bar", "stack": "cost",
             "data": [float(row.get("labor_cost") or 0) for row in categories], "itemStyle": {"color": CHART_COLORS[1]}},
            {"name": "制造费用", "type": "bar", "stack": "cost",
             "data": [float(row.get("manufacturing_cost") or 0) for row in categories], "itemStyle": {"color": CHART_COLORS[2]}},
            {"name": "物流", "type": "bar", "stack": "cost",
             "data": [float(row.get("logistics_cost") or 0) for row in categories], "itemStyle": {"color": CHART_COLORS[3]}},
            {"name": "安装", "type": "bar", "stack": "cost",
             "data": [float(row.get("installation_cost") or 0) for row in categories], "itemStyle": {"color": CHART_COLORS[4]}},
        ],
    }

    quadrants = (quadrant or {}).get("quadrants") or {}
    scatter_series = []
    quadrant_names = {"star": "明星产品", "volume": "跑量产品", "niche": "小众精品", "drag": "待优化"}
    for key, name in quadrant_names.items():
        items = (quadrants.get(key) or {}).get("items") or []
        scatter_series.append(
            {
                "name": name,
                "type": "scatter",
                "symbolSize": 14,
                "data": [
                    [round(float(item.get("revenue_share") or 0) * 100, 2),
                     round(float(item.get("profit_share") or 0) * 100, 2),
                     item.get("product_name") or item.get("product_code")]
                    for item in items
                ],
                "itemStyle": {"color": CHART_COLORS[["star", "volume", "niche", "drag"].index(key)]},
            }
        )
    thresholds = (quadrant or {}).get("thresholds") or {}
    option_quadrant = {
        "tooltip": {"trigger": "item", "formatter": "{a}<br/>{c}"},
        "legend": {"bottom": 0},
        "grid": {"left": 60, "right": 40, "top": 40, "bottom": 60},
        "xAxis": {"type": "value", "name": "营收占比 %"},
        "yAxis": {"type": "value", "name": "毛利占比 %"},
        "series": scatter_series,
        "markLine": {
            "silent": True,
            "data": [
                {"xAxis": round(float(thresholds.get("revenue_share") or 0) * 100, 2)},
                {"yAxis": round(float(thresholds.get("profit_share") or 0) * 100, 2)},
            ],
        },
    }

    option_discount = {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["订单数", "毛利率"]},
        "grid": {"left": 60, "right": 60, "top": 50, "bottom": 50},
        "xAxis": {"type": "category", "data": [row.get("bucket") for row in discount_rows]},
        "yAxis": [
            {"type": "value", "name": "订单数"},
            {"type": "value", "name": "毛利率", "axisLabel": {"formatter": "{value}%"}},
        ],
        "series": [
            {"name": "订单数", "type": "bar", "data": [row.get("order_count") for row in discount_rows],
             "itemStyle": {"color": CHART_COLORS_SOFT[0]}},
            {"name": "毛利率", "type": "line", "yAxisIndex": 1, "smooth": True,
             "data": [round(float(row.get("gross_margin") or 0) * 100, 2) for row in discount_rows],
             "itemStyle": {"color": CHART_COLORS[4]}},
        ],
    }

    category_table = build_table(
        "tblProfitCategory",
        "品类收入与成本结构",
        [
            {"label": "品类", "key": "category"},
            {"label": "数量", "key": "qty", "format": "number"},
            {"label": "成交额", "key": "revenue", "format": "money"},
            {"label": "收入占比", "key": "revenue_share", "format": "percent"},
            {"label": "毛利额", "key": "gross_profit", "format": "money"},
            {"label": "毛利率", "key": "gross_margin", "format": "percent"},
            {"label": "材料成本占收入", "key": "material_ratio", "format": "percent"},
            {"label": "元/㎡", "key": "unit_price", "format": "money"},
        ],
        [
            {
                "category": row.get("category_label") or row.get("category"),
                "qty": row.get("qty"),
                "revenue": row.get("revenue"),
                "revenue_share": row.get("revenue_share"),
                "gross_profit": row.get("gross_profit"),
                "gross_margin": row.get("gross_margin"),
                "material_ratio": row.get("material_ratio"),
                "unit_price": row.get("unit_price_per_m2"),
            }
            for row in categories
        ],
        caption="六大品类的收入、毛利与成本结构对比",
        description="元/㎡ 只在明细口径（fact）下可算，走预聚合表时为空，，空表示「算不出」，不是 0。",
    )
    sku_table = build_table(
        "tblProfitSku",
        "SKU 毛利排名 Top 15（与收入排名常有差异）",
        [
            {"label": "产品编码", "key": "product_code"},
            {"label": "产品名称", "key": "product_name"},
            {"label": "品类", "key": "category"},
            {"label": "销量", "key": "qty", "format": "number"},
            {"label": "成交额", "key": "revenue", "format": "money"},
            {"label": "毛利额", "key": "gross_profit", "format": "money"},
            {"label": "毛利率", "key": "gross_margin", "format": "percent"},
            {"label": "均价", "key": "unit_price", "format": "money"},
        ],
        [
            {
                "product_code": item.get("product_code"),
                "product_name": item.get("product_name"),
                "category": item.get("category"),
                "qty": item.get("qty"),
                "revenue": item.get("revenue"),
                "gross_profit": item.get("gross_profit"),
                "gross_margin": item.get("gross_margin"),
                "unit_price": item.get("unit_price"),
            }
            for item in ((sku_payload or {}).get("by_profit") or [])[:15]
        ],
        caption="按毛利额排序的 SKU 榜单，用于与收入榜交叉比对",
        description="毛利榜冠军如果不在收入榜前十，说明「卖得多」与「赚得多」在这里已经分叉。",
    )
    discount_table = build_table(
        "tblProfitDiscount",
        "折扣区间与毛利表现",
        [
            {"label": "折扣区间", "key": "bucket"},
            {"label": "订单数", "key": "order_count", "format": "number"},
            {"label": "成交额", "key": "revenue", "format": "money"},
            {"label": "让利金额", "key": "discount_amount", "format": "money"},
            {"label": "毛利额", "key": "gross_profit", "format": "money"},
            {"label": "毛利率", "key": "gross_margin", "format": "percent"},
            {"label": "客单价", "key": "avg_order_value", "format": "money"},
        ],
        discount_rows,
        caption="按折扣率分组的订单数与毛利率，用于量化折扣对毛利的侵蚀",
        description="折扣不会让成本下降：让掉的每一分都是纯毛利，这条曲线的斜率就是「降价换量」的单位代价。",
    )

    conclusions = _profit_conclusions(categories, discount_rows, quadrants, cr3)
    ctx = {
        "warnings": warnings,
        "kpis": kpis,
        "category_table": category_table,
        "sku_table": sku_table,
        "discount_table": discount_table,
        "quadrant": quadrant,
        "discount_rows": discount_rows,
        "charts": [
            chart("chartProfitStructure", "品类成交额、毛利额与累计占比", option_structure, 380,
                  "累计占比线爬升越快，说明收入越依赖头部品类，，集中是效率，也是风险。"),
            chart("chartProfitCost", "品类成本结构（五项成本堆叠）", option_cost, 360,
                  "材料成本是所有品类的第一大宗，看它的堆叠高度就找到了降本入口。"),
            chart("chartProfitQuadrant", "SKU 利润四象限（营收占比 vs 毛利占比）", option_quadrant, 380,
                  "分界线是「平均占比」：右上保产能、左上提价、右下优化成本、左下考虑下架。"),
            chart("chartProfitDiscount", "折扣区间与毛利率关系", option_discount, 340,
                  "折线向下的斜率就是「降价换量」的单位代价。"),
        ],
        "conclusions": conclusions,
    }
    _attach_metric_defs(ctx, _matched_codes(
        ["M_CP_002", "M_CP_003", "M_CP_004", "M_CP_005", "M_CP_009", "M_OV_005"]
    ), warnings)
    return _page(request, "dashboard/profit.html", "品类与利润",
                 "哪类门真正在赚钱？折扣到底让掉了多少毛利？", ctx, filters, "profit")


def _cumulative_share(categories) -> list:
    """品类累计收入占比（帕累托曲线）；使用 selectors 已算好的单品类占比累加。"""
    total = 0.0
    result = []
    for row in categories:
        total += float(row.get("revenue_share") or 0)
        result.append(round(total * 100, 2))
    return result


def _profit_conclusions(categories, discount_rows, quadrants, cr3) -> list:
    conclusions = []
    if not categories:
        return [{"title": "暂无品类数据", "body": "当前筛选条件下没有明细数据。所以建议：清空品类筛选或放宽时间范围。", "level": "info"}]

    if cr3:
        conclusions.append(
            {
                "title": "收入集中度 CR3 = {}".format(pct(cr3)),
                "body": "前三品类贡献了 {} 的成交额。所以建议：{}".format(
                    pct(cr3),
                    "对头部品类做「双供应商」准备，避免单一材料断供直接砸掉主要收入。"
                    if cr3 > 0.75 else "集中度健康，可把资源优先投给累计占比 80% 以内的核心品类。",
                ),
                "level": "warn" if cr3 > 0.75 else "good",
            }
        )

    if discount_rows:
        first = discount_rows[0]
        last = discount_rows[-1]
        gap = float(first.get("gross_margin") or 0) - float(last.get("gross_margin") or 0)
        if gap > 0:
            conclusions.append(
                {
                    "title": "折扣侵蚀毛利率 {:.1f} 个百分点".format(gap * 100),
                    "body": "从「{}」（毛利率 {}）到「{}」（毛利率 {}），毛利率共下降 {:.1f} 个百分点。"
                    "折扣不会让成本下降，让掉的每一分都是纯毛利。"
                    "所以建议：把 8% 以上的折扣从「销售自主决定」改为「店长审批」，并把审批依据写成毛利下限。".format(
                        first.get("bucket"), pct(first.get("gross_margin")),
                        last.get("bucket"), pct(last.get("gross_margin")), gap * 100,
                    ),
                    "level": "danger",
                }
            )

    volume = quadrants.get("volume") or {}
    if float(volume.get("count") or 0) > 0:
        names = "、".join((item.get("product_name") or item.get("product_code") or "") for item in (volume.get("items") or [])[:3])
        conclusions.append(
            {
                "title": "跑量产品有 {} 个（高收入 · 低毛利）".format(volume.get("count")),
                "body": "代表产品：{}。它们撑住了规模，但正在吃掉利润。所以建议：优先与供应商重谈这几款的材料价，"
                "或在报价单里作为「引流款」搭配高毛利款成组报价。".format(names or "-"),
                "level": "warn",
            }
        )

    drag = quadrants.get("drag") or {}
    if float(drag.get("count") or 0) > 0:
        conclusions.append(
            {
                "title": "待优化 SKU 有 {} 个（低营收 · 低毛利）".format(drag.get("count")),
                "body": "这类产品既不走量也不赚钱，但占用排产、图纸与库存资源。"
                "所以建议：列一份清单做「保留 / 改款 / 下架」三选一评审，评审前先算清它占用的工时。",
                "level": "info",
            }
        )
    return conclusions[:4]


# ---------------------------------------------------------------------------
# 四、客户价值
# ---------------------------------------------------------------------------
@login_required
def customers(request):
    """客户价值：客户结构健康吗？谁是真正的高价值客户？"""
    filters = filters_from(request)
    warnings = []
    segments = safe("客户分群", get_customer_segments, filters, warnings=warnings, default=[]) or []
    cohort = safe("留存矩阵", get_cohort_matrix, filters, warnings=warnings, default={}) or {}
    churn = safe("流失预警", get_churn_risk_list, filters, 60, warnings=warnings, default=[]) or []
    kpi_data = safe("经营总览指标", get_kpi_summary, filters, warnings=warnings, default={}) or {}

    total_customers = sum(int(row.get("customer_count") or 0) for row in segments)
    total_spend = sum(float(row.get("total_spend") or 0) for row in segments)
    high_value = find_segment(segments, "高价值客户", "high_value")
    dormant = find_segment(segments, "沉睡客户", "dormant")

    kpis = [
        kpi("成交客户数", "{} 人".format(number(kpi_data.get("customer_count"))), "统计期内有成交的客户（去重）", None, "primary", ""),
        kpi("新增客户数", "{} 人".format(number(kpi_data.get("new_customer_count"))), "首单日期落在统计期内", None, "", "M_GR_001"),
        kpi("高价值客户占比", pct((high_value or {}).get("customer_share")) if high_value else "-",
            "高价值客群人数 ÷ 全部客户", None, "good", ""),
        kpi("高价值客户收入贡献", pct((high_value or {}).get("monetary_share")) if high_value else "-",
            "高价值客群累计消费 ÷ 全部客户消费", None, "good", ""),
        kpi("沉睡客户数", "{} 人".format(number((dormant or {}).get("customer_count"))), "RFM 分群为「沉睡客户」", None, "danger", ""),
        kpi("流失预警名单", "{} 人".format(number(len(churn))), "Top30% 消费客户中超过 2 倍复购间隔未下单", None, "warn", "M_CV_006"),
    ]

    option_segment = {
        "tooltip": {"trigger": "item", "formatter": "{b}<br/>人数 {c} 人（{d}%）"},
        "legend": {"bottom": 0},
        "series": [
            {
                "name": "客群分布",
                "type": "pie",
                "radius": ["45%", "70%"],
                "data": [
                    {"name": row.get("segment_label") or row.get("segment"),
                     "value": int(row.get("customer_count") or 0)}
                    for row in segments
                ],
            }
        ],
    }

    option_segment_value = {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["累计消费额", "人均消费额"]},
        "grid": {"left": 70, "right": 70, "top": 50, "bottom": 60},
        "xAxis": {"type": "category",
                  "data": [row.get("segment_label") or row.get("segment") for row in segments],
                  "axisLabel": {"interval": 0, "rotate": 15}},
        "yAxis": [
            {"type": "value", "name": "累计消费(元)"},
            {"type": "value", "name": "人均(元)"},
        ],
        "series": [
            {"name": "累计消费额", "type": "bar", "data": [float(row.get("total_spend") or 0) for row in segments],
             "itemStyle": {"color": CHART_COLORS[0]}},
            {"name": "人均消费额", "type": "line", "yAxisIndex": 1, "smooth": True,
             "data": [float(row.get("avg_spend") or 0) for row in segments],
             "itemStyle": {"color": CHART_COLORS[2]}},
        ],
    }

    option_cohort = _cohort_heatmap(cohort)

    segment_table = build_table(
        "tblCustomerSegment",
        "客群结构与价值贡献",
        [
            {"label": "客群", "key": "segment"},
            {"label": "客户数", "key": "customer_count", "format": "number"},
            {"label": "人数占比", "key": "customer_share", "format": "percent"},
            {"label": "累计消费", "key": "total_spend", "format": "money"},
            {"label": "消费占比", "key": "monetary_share", "format": "percent"},
            {"label": "人均消费", "key": "avg_spend", "format": "money"},
            {"label": "平均最近间隔(天)", "key": "avg_recency_days", "format": "number1"},
            {"label": "平均频次", "key": "avg_frequency", "format": "number2"},
        ],
        [
            {
                "segment": row.get("segment_label") or row.get("segment"),
                "customer_count": row.get("customer_count"),
                "customer_share": row.get("customer_share"),
                "total_spend": row.get("total_spend"),
                "monetary_share": row.get("monetary_share"),
                "avg_spend": row.get("avg_spend"),
                "avg_recency_days": row.get("avg_recency_days"),
                "avg_frequency": row.get("avg_frequency"),
            }
            for row in segments
        ],
        caption="RFM 客群的人数、消费贡献与活跃度对比",
        description="客群来自 Customer.segment（由模型任务回写），本页只统计不重新判定，保证与营销名单一致。",
    )
    churn_table = build_table(
        "tblCustomerChurn",
        "流失预警名单（Top 30）",
        [
            {"label": "客户编码", "key": "customer_code"},
            {"label": "客户名称", "key": "customer_name"},
            {"label": "客群", "key": "segment"},
            {"label": "渠道", "key": "channel"},
            {"label": "累计消费", "key": "monetary", "format": "money"},
            {"label": "频次", "key": "frequency", "format": "number"},
            {"label": "最近间隔(天)", "key": "recency_days", "format": "number"},
            {"label": "最近下单日", "key": "last_order_date"},
            {"label": "风险等级", "key": "risk_level"},
        ],
        churn[:30],
        caption="按累计消费额排序的流失预警名单",
        description="判定规则：消费额在全体客户 Top 30% 之内，且未下单天数超过平均复购间隔的 2 倍。",
    )

    conclusions = _customer_conclusions(segments, churn)
    ctx = {
        "warnings": warnings,
        "kpis": kpis,
        "segment_table": segment_table,
        "churn_table": churn_table,
        "cohort": cohort,
        "charts": [
            chart("chartCustomerSegment", "RFM 客群人数分布", option_segment, 340,
                  "人数占比与消费占比的落差，就是「少数人贡献多数收入」的量化形式。"),
            chart("chartCustomerValue", "各客群的累计与人均消费", option_segment_value, 360,
                  "人均消费高的客群是营销预算该优先投向的地方。"),
            chart("chartCustomerCohort", "客户留存热力图（队列 × 第 N 月）", option_cohort, 380,
                  "空白格表示该队列还没到那个月，不是留存为 0，，不要把空白读成流失。"),
        ],
        "conclusions": conclusions,
    }
    _attach_metric_defs(ctx, _matched_codes(
        ["M_CV_001", "M_CV_002", "M_CV_003", "M_CV_005", "M_CV_006", "M_GR_001"]
    ), warnings)
    return _page(request, "dashboard/customers.html", "客户价值",
                 "客户结构健康吗？谁是真正的高价值客户？", ctx, filters, "customers")


def _cohort_heatmap(cohort: dict) -> dict:
    """把队列留存矩阵转成 ECharts heatmap option。

    ``retention`` 里的 ``None`` 表示「还没到这个月」，这里**保留为不上色**，，
    用 0 填充会把「未来」画成「留存归零」，是热力图最常见的误读来源。
    """
    cohorts = cohort.get("cohorts") or []
    max_index = int(cohort.get("max_index") or 12)
    x_labels = ["第 {} 月".format(index) for index in range(max_index + 1)]
    y_labels = [row.get("cohort_month") for row in cohorts]
    data = []
    for y_index, row in enumerate(cohorts):
        for x_index, value in enumerate(row.get("retention") or []):
            if value is None:
                continue
            data.append([x_index, y_index, round(float(value) * 100, 2)])
    return {
        "tooltip": {"position": "top", "formatter": "{b}: {c}%"},
        "grid": {"left": 80, "right": 40, "top": 40, "bottom": 60},
        "xAxis": {"type": "category", "data": x_labels, "splitArea": {"show": True}},
        "yAxis": {"type": "category", "data": y_labels, "splitArea": {"show": True}},
        "visualMap": {"min": 0, "max": 100, "calculable": True, "orient": "horizontal", "left": "center", "bottom": 0},
        "series": [{"name": "留存率", "type": "heatmap", "data": data, "label": {"show": False}}],
    }


def _customer_conclusions(segments, churn) -> list:
    conclusions = []
    if not segments:
        return [{"title": "暂无客户分群数据", "body": "客户表可能尚未回写 RFM 分群结果。所以建议：先运行分群任务，再回看本页。", "level": "info"}]

    high_value = find_segment(segments, "高价值客户", "high_value")
    if high_value:
        conclusions.append(
            {
                "title": "高价值客户：{} 人，贡献 {} 的消费".format(
                    number(high_value.get("customer_count")), pct(high_value.get("monetary_share"))
                ),
                "body": "人数占比仅 {}，却贡献了 {} 的累计消费，人均 {}。"
                "所以建议：为这批客户建立「一人一策」的服务清单（专属图纸复核、优先排产、交付前后各一次回访），"
                "流失一个高价值客户的代价远高于获取三个新客户。".format(
                    pct(high_value.get("customer_share")), pct(high_value.get("monetary_share")),
                    money(high_value.get("avg_spend")),
                ),
                "level": "good",
            }
        )

    dormant = find_segment(segments, "沉睡客户", "dormant")
    if dormant and int(dormant.get("customer_count") or 0) > 0:
        conclusions.append(
            {
                "title": "沉睡客户 {} 人，人均历史消费 {}".format(
                    number(dormant.get("customer_count")), money(dormant.get("avg_spend"))
                ),
                "body": "沉睡客户的累计消费占全部客户的 {}，这不是小数。"
                "所以建议：先按「最近一次消费时间」分三档做唤醒（电话 → 微信 → 短信），"
                "并给一线一条明确的优惠底线，避免唤醒动作本身把毛利吃掉。".format(
                    pct(dormant.get("monetary_share"))
                ),
                "level": "warn",
            }
        )

    if churn:
        high_risk = [row for row in churn if row.get("risk_level") == "高"]
        conclusions.append(
            {
                "title": "高流失风险客户 {} 人".format(len(high_risk) or len(churn)),
                "body": "判定规则：消费额在全体客户 Top30% 之内，且距今未下单天数超过平均复购间隔的 2 倍。"
                "规则可解释、可复现，业务拿到名单就能直接打电话。"
                "所以建议：本周内由客户对应的原销售完成一次回访，回访结果回写系统，形成闭环。",
                "level": "danger" if high_risk else "warn",
            }
        )

    growth = next((row for row in segments if (row.get("segment") or "") == "growth_potential"), None)
    if growth:
        conclusions.append(
            {
                "title": "成长潜力客户 {} 人".format(number(growth.get("customer_count"))),
                "body": "这类客户频次与金额都在上升通道，是性价比最高的追加销售对象。"
                "所以建议：对他们在二次装修、邻居拼单场景做定向推荐，别把预算全花在拉新上。",
                "level": "info",
            }
        )
    return conclusions[:4]


# ---------------------------------------------------------------------------
# 五、客户 360
# ---------------------------------------------------------------------------
@login_required
def customer_detail(request, customer_code):
    """客户 360：这个客户到底买了什么、付了多少钱、有没有风险？"""
    warnings = []
    data = safe("客户 360", get_customer_360, customer_code, warnings=warnings, default={"found": False}) or {}

    if not data.get("found"):
        return _page(
            request, "dashboard/customer_detail.html", "客户 360 · 未找到",
            "客户编码 {} 不存在".format(customer_code),
            {"warnings": warnings, "found": False, "customer_code": customer_code,
             "conclusions": [{"title": "没有找到该客户", "body": "客户编码可能书写有误，或该客户尚未建档。所以建议：回到客户价值页从名单中直接点进。", "level": "warn"}]},
            None, "customers",
        )

    profile = dict(data.get("profile") or {})
    profile["phone_masked"] = _mask_phone(request.user, profile.get("phone"))
    summary = data.get("value_summary") or {}
    orders = data.get("orders") or []
    payments = data.get("payments") or []
    after_sales = data.get("after_sales") or []
    timeline = data.get("timeline") or []
    risk_flags = data.get("risk_flags") or []

    kpis = [
        kpi("累计成交额", money(summary.get("revenue")), "{} 笔有效订单".format(number(summary.get("order_count"))), None, "primary", "M_CV_001"),
        kpi("累计毛利额", money(summary.get("gross_profit")), "毛利率 {}".format(pct(summary.get("gross_margin"))), None, "", "M_OV_004"),
        kpi("客单价", money(summary.get("avg_order_value")), "成交额 ÷ 有效订单数", None, "", "M_OV_003"),
        kpi("累计回款", money(summary.get("paid_amount")), "含退款 {}（已扣除）".format(money(summary.get("refund_amount"))), None, "good", "M_CW_001"),
        kpi("应收未收", money(summary.get("outstanding_amount")),
            "最长逾期 {} 天".format(number(summary.get("max_overdue_days"))), None,
            "danger" if float(summary.get("outstanding_amount") or 0) > 0 else "good", "M_CW_004"),
        kpi("距上次下单", "{} 天".format(number(summary.get("recency_days"))),
            "频次 {} 次".format(number(summary.get("frequency"))), None, "warn", ""),
    ]

    # 双轴时间线：柱＝订单金额，线＝回款金额
    order_labels = [order.get("order_date") for order in reversed(orders)]
    option_timeline = {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["订单金额", "回款金额"]},
        "grid": {"left": 70, "right": 40, "top": 50, "bottom": 60},
        "xAxis": {"type": "category", "data": order_labels, "axisLabel": {"rotate": 30}},
        "yAxis": [{"type": "value", "name": "订单(元)"}, {"type": "value", "name": "回款(元)"}],
        "series": [
            {"name": "订单金额", "type": "bar",
             "data": [float(order.get("final_amount") or 0) for order in reversed(orders)],
             "itemStyle": {"color": CHART_COLORS[0]}},
            {"name": "回款金额", "type": "line", "yAxisIndex": 1, "smooth": True,
             "data": [float(payment.get("amount") or 0) for payment in reversed(payments)][:len(order_labels)],
             "itemStyle": {"color": CHART_COLORS[1]}},
        ],
    }

    option_payment = {
        "tooltip": {"trigger": "item", "formatter": "{b}: {c} 元 ({d}%)"},
        "legend": {"bottom": 0},
        "series": [
            {
                "name": "付款阶段",
                "type": "pie",
                "radius": ["40%", "68%"],
                "data": _payment_stage_data(payments),
            }
        ],
    }

    option_after = {
        "tooltip": {"trigger": "axis"},
        "grid": {"left": 70, "right": 30, "top": 40, "bottom": 50},
        "xAxis": {"type": "category", "data": [row.get("report_date") for row in reversed(after_sales)]},
        "yAxis": {"type": "value", "name": "售后成本(元)"},
        "series": [
            {"name": "售后成本", "type": "bar",
             "data": [float(row.get("cost") or 0) for row in reversed(after_sales)],
             "itemStyle": {"color": CHART_COLORS[4]}},
        ],
    }

    item_rows = []
    for order in orders:
        for item in order.get("items") or []:
            item_rows.append(
                {
                    "order_no": order.get("order_no"),
                    "order_date": order.get("order_date"),
                    "product_name": item.get("product_name"),
                    "category": item.get("category"),
                    "quantity": item.get("quantity"),
                    "line_amount": item.get("line_amount"),
                    "gross_profit": item.get("gross_profit"),
                    "area_m2": item.get("area_m2"),
                }
            )

    order_table = build_table(
        "tblCustomerOrders",
        "订单记录（按时间倒序）",
        [
            {"label": "订单号", "key": "order_no"},
            {"label": "下单日期", "key": "order_date"},
            {"label": "状态", "key": "order_status"},
            {"label": "渠道", "key": "channel"},
            {"label": "主品类", "key": "dominant_category"},
            {"label": "成交额", "key": "final_amount", "format": "money"},
            {"label": "让利率", "key": "discount", "format": "percent2"},
            {"label": "交付周期(天)", "key": "actual_lead_days", "format": "number"},
            {"label": "延期天数", "key": "delay_days", "format": "number"},
            {"label": "门店", "key": "store_name"},
            {"label": "业务员", "key": "sales_rep_name"},
        ],
        orders,
        caption="该客户的全部订单（含已取消），用于核对成交与交付表现",
        description="is_on_time 为 -1 表示尚未交付（不参与准时率分母），不要在明细里把它读成「延期」。",
    )
    item_table = build_table(
        "tblCustomerItems",
        "订单明细（产品级）",
        [
            {"label": "订单号", "key": "order_no"},
            {"label": "下单日期", "key": "order_date"},
            {"label": "产品名称", "key": "product_name"},
            {"label": "品类", "key": "category"},
            {"label": "数量", "key": "quantity", "format": "number"},
            {"label": "行金额", "key": "line_amount", "format": "money"},
            {"label": "行毛利", "key": "gross_profit", "format": "money"},
            {"label": "面积(㎡)", "key": "area_m2", "format": "number2"},
        ],
        item_rows,
        caption="该客户订单下的产品明细与单行毛利",
        description="行毛利能看出「这一单是哪个产品赚的钱」，，客户级毛利异常时先看这里。",
    )
    payment_table = build_table(
        "tblCustomerPayments",
        "付款流水",
        [
            {"label": "付款单号", "key": "payment_no"},
            {"label": "付款日期", "key": "payment_date"},
            {"label": "阶段", "key": "stage"},
            {"label": "金额", "key": "amount", "format": "money"},
            {"label": "方式", "key": "payment_method"},
            {"label": "是否退款", "key": "is_refund_text"},
        ],
        [
            {
                "payment_no": row.get("payment_no"),
                "payment_date": row.get("payment_date"),
                "stage": row.get("stage"),
                "amount": row.get("amount"),
                "payment_method": row.get("payment_method"),
                "is_refund_text": "是" if row.get("is_refund") else "否",
            }
            for row in payments
        ],
        caption="该客户的付款流水，含退款记录",
        description="回款额统计一律排除退款行；退款单独看，否则会虚高客户的付款贡献。",
    )
    after_table = build_table(
        "tblCustomerAfterSales",
        "售后工单",
        [
            {"label": "工单号", "key": "after_no"},
            {"label": "报修日期", "key": "report_date"},
            {"label": "类型", "key": "after_type"},
            {"label": "涉及品类", "key": "category"},
            {"label": "成本", "key": "cost", "format": "money"},
            {"label": "状态", "key": "status"},
            {"label": "处理天数", "key": "handling_days", "format": "number"},
        ],
        after_sales,
        caption="该客户的售后工单与处理成本",
        description="售后成本会回冲这张客户的价值贡献，评估客户价值时必须把它一起看。",
    )

    conclusions = _customer360_conclusions(profile, summary, risk_flags, orders, after_sales)
    ctx = {
        "warnings": warnings,
        "found": True,
        "customer_code": data.get("customer_code") or customer_code,
        "profile": profile,
        "summary": summary,
        "orders": orders,
        "payments": payments,
        "after_sales": after_sales,
        "timeline": timeline,
        "risk_flags": risk_flags,
        "kpis": kpis,
        "order_table": order_table,
        "item_table": item_table,
        "payment_table": payment_table,
        "after_table": after_table,
        "charts": [
            chart("chartCustomerTimeline", "订单金额（柱）与回款金额（折线）", option_timeline, 360,
                  "柱高而线低的时间段，就是这家客户压款最重的阶段。"),
            chart("chartCustomerPayment", "回款阶段构成", option_payment, 320,
                  "定金/中期款/尾款的比例能反映合同条款是否偏松。"),
            chart("chartCustomerAfterSales", "售后成本时间线", option_after, 300,
                  "售后成本集中在某个交付批次时，问题多半出在那批次的工艺或承运商。"),
        ],
        "conclusions": conclusions,
    }
    _attach_metric_defs(ctx, _matched_codes(
        ["M_CV_001", "M_CV_006", "M_CW_001", "M_CW_003", "M_DS_006", "M_OV_003"]
    ), warnings)
    return _page(request, "dashboard/customer_detail.html",
                 "客户 360 · {}".format(profile.get("customer_name") or customer_code),
                 "这个客户买了什么、付了多少钱、有没有风险？", ctx, None, "customers")


def _aging_label(bucket) -> str:
    """账龄桶编码 → 中文标签（``over_90`` → ``90 天以上``）。

    用 AGING_BUCKET_CHOICES 反查，未登记的编码原样返回：
    账龄桶的枚举定义只应有一处（constants），这里不硬编码第二份。
    """
    mapping = dict(AGING_BUCKET_CHOICES)
    return mapping.get(str(bucket), str(bucket))


def _mask_phone(user, phone):
    """客户电话脱敏。

    优先用 ``User.mask_phone()``（权限模型在 accounts 应用里，一处实现；
    它有 ``can_see_customer_contact`` 判断）；用户对象没有这个能力时
    （例如登录中间件被替换、匿名对象）退回本地脱敏，，
    脱敏失败绝不能变成「明文泄漏」，所以这里的兜底方向是**更严格**。
    """
    if not phone:
        return "-"
    masker = getattr(user, "mask_phone", None)
    if callable(masker):
        try:
            masked = masker(phone)
            if masked:
                return masked
        except Exception as exc:  # pragma: no cover
            logger.warning("mask_phone 调用失败，改用本地脱敏：%s", exc)
    text = str(phone)
    if len(text) >= 7:
        return "{}****{}".format(text[:3], text[-2:])
    return "***"


def _payment_stage_data(payments) -> list:
    buckets = {}
    for payment in payments:
        if payment.get("is_refund"):
            continue
        key = payment.get("stage") or "未标注"
        buckets[key] = buckets.get(key, 0.0) + float(payment.get("amount") or 0)
    return [{"name": key, "value": round(value, 2)} for key, value in buckets.items()]


def _customer360_conclusions(profile, summary, risk_flags, orders, after_sales) -> list:
    conclusions = []
    if risk_flags:
        top = risk_flags[0]
        conclusions.append(
            {
                "title": "风险提示：{}（{}）".format(top.get("flag"), top.get("level")),
                "body": "{}。所以建议：{}".format(
                    top.get("detail"),
                    "由客户经理在 3 个工作日内完成一次回访并登记结果；若涉及应收账款，同步启动对账。"
                    if top.get("level") in ("高", "中") else "保持常规跟进即可，留意其开工节奏变化。",
                ),
                "level": "danger" if top.get("level") == "高" else ("warn" if top.get("level") == "中" else "info"),
            }
        )

    if float(summary.get("outstanding_amount") or 0) > 0:
        conclusions.append(
            {
                "title": "应收未收 {}，最长逾期 {} 天".format(
                    money(summary.get("outstanding_amount")), number(summary.get("max_overdue_days"))
                ),
                "body": "逾期 90 天以上的应收需要按坏账计提口径管理。"
                "所以建议：先核对是否存在「已付款未登记」的情况，再决定是否进入正式催收流程。",
                "level": "warn",
            }
        )

    if orders:
        conclusions.append(
            {
                "title": "复购画像：{} 笔订单，客单价 {}".format(
                    number(summary.get("order_count")), money(summary.get("avg_order_value"))
                ),
                "body": "该客户最近一次下单为 {}，累计毛利 {}。"
                "所以建议：{}".format(
                    summary.get("last_order_date") or "-", money(summary.get("gross_profit")),
                    "在装修旺季前 1 个月做一次主动触达（新款式样册 / 老客户专属价）。"
                    if int(summary.get("frequency") or 0) >= 2
                    else "把首单交付体验做扎实，首单之后的第二次触达是复购的关键窗口。",
                ),
                "level": "info",
            }
        )

    if after_sales:
        conclusions.append(
            {
                "title": "售后工单 {} 单".format(len(after_sales)),
                "body": "售后成本合计 {}。所以建议：把该客户已发生的售后类型（如尺寸误差、门体变形）登记为"
                "「下次报价时的风险提示」，在下单前先复核尺寸与安装条件，避免重复发生。".format(
                    money(sum(float(row.get("cost") or 0) for row in after_sales))
                ),
                "level": "warn" if len(after_sales) > 2 else "info",
            }
        )
    return conclusions[:4]


# ---------------------------------------------------------------------------
# 六、留存与预警
# ---------------------------------------------------------------------------
@login_required
def retention(request):
    """留存与预警：客户还回来吗？该在什么时候给谁打电话？"""
    filters = filters_from(request)
    warnings = []
    cohort = safe("留存矩阵", get_cohort_matrix, filters, warnings=warnings, default={}) or {}
    churn = safe("流失预警", get_churn_risk_list, filters, 100, warnings=warnings, default=[]) or []
    segments = safe("客户分群", get_customer_segments, filters, warnings=warnings, default=[]) or []

    average_curve = cohort.get("average_retention") or []
    month_indexes = ["第 {} 月".format(index) for index in range(int(cohort.get("max_index") or 12) + 1)]
    option_retention = {
        "tooltip": {"trigger": "axis", "formatter": "{b}: {c}%"},
        "grid": {"left": 60, "right": 40, "top": 50, "bottom": 40},
        "xAxis": {"type": "category", "data": month_indexes},
        "yAxis": {"type": "value", "name": "留存率", "max": 100, "axisLabel": {"formatter": "{value}%"}},
        "series": [
            {"name": "平均留存率", "type": "line", "smooth": True, "symbolSize": 8,
             "data": [None if value is None else round(float(value) * 100, 2) for value in average_curve],
             "itemStyle": {"color": CHART_COLORS[0]},
             "areaStyle": {"opacity": 0.12}}
        ],
    }

    option_cohort = _cohort_heatmap(cohort)

    risk_labels = ["高", "中", "低"]
    risk_counts = [sum(1 for row in churn if row.get("risk_level") == level) for level in risk_labels]
    option_risk = {
        "tooltip": {"trigger": "axis"},
        "grid": {"left": 60, "right": 30, "top": 40, "bottom": 40},
        "xAxis": {"type": "category", "data": risk_labels},
        "yAxis": {"type": "value", "name": "客户数"},
        "series": [
            {"name": "预警客户数", "type": "bar", "data": risk_counts,
             "itemStyle": {"color": CHART_COLORS[4]},
             "label": {"show": True, "position": "top"}}
        ],
    }

    dormant = find_segment(segments, "沉睡客户", "dormant")
    kpis = [
        kpi("次月留存率", pct(average_curve[1] if len(average_curve) > 1 else None), "队列平均，第 1 月仍下单的比例", None, "primary", "M_CV_005"),
        kpi("3 月留存率", pct(average_curve[3] if len(average_curve) > 3 else None), "队列平均，第 3 月仍下单的比例", None, "", "M_CV_005"),
        kpi("队列数量", "{} 个".format(number(len(cohort.get("cohorts") or []))), "按首单月份划分的客户队列", None, "", ""),
        kpi("流失预警名单", "{} 人".format(number(len(churn))), "高价值 + 超 2 倍复购间隔未下单", None, "danger", "M_CV_006"),
        kpi("高危预警", "{} 人".format(number(risk_counts[0])), "距上次下单超过 3 倍复购间隔", None, "danger", "M_CV_006"),
        kpi("沉睡客户", "{} 人".format(number((dormant or {}).get("customer_count"))),
            "人均历史消费 {}".format(money((dormant or {}).get("avg_spend"))), None, "warn", ""),
    ]

    churn_table = build_table(
        "tblRetentionChurn",
        "流失预警名单（Top 50，按累计消费降序）",
        [
            {"label": "客户编码", "key": "customer_code"},
            {"label": "客户名称", "key": "customer_name"},
            {"label": "客户类型", "key": "customer_type"},
            {"label": "客群", "key": "segment"},
            {"label": "会员等级", "key": "member_level"},
            {"label": "累计消费", "key": "monetary", "format": "money"},
            {"label": "频次", "key": "frequency", "format": "number"},
            {"label": "最近间隔(天)", "key": "recency_days", "format": "number"},
            {"label": "最近下单日", "key": "last_order_date"},
            {"label": "风险等级", "key": "risk_level"},
            {"label": "判定原因", "key": "reason"},
        ],
        churn[:50],
        caption="流失预警名单，含判定原因与风险等级",
        description="名单按累计消费额排序分配：前 20% 由资深销售一对一跟进，其余走标准化话术，避免平均用力。",
    )

    conclusions = _retention_conclusions(average_curve, churn, dormant)
    ctx = {
        "warnings": warnings,
        "kpis": kpis,
        "cohort": cohort,
        "churn_table": churn_table,
        "charts": [
            chart("chartRetentionCurve", "平均留存曲线", option_retention, 340,
                  "曲线在第 2~3 月趋平说明客户关系已稳定；持续下滑说明交付或产品出了问题。"),
            chart("chartRetentionCohort", "队列留存热力图", option_cohort, 400,
                  "横向看同一队列的衰减速度，纵向看不同队列的表现在变好还是变差。"),
            chart("chartRetentionRisk", "流失预警风险分布", option_risk, 300,
                  "高危名单应该当天分派到人，否则预警就只是一张报表。"),
        ],
        "conclusions": conclusions,
    }
    _attach_metric_defs(ctx, _matched_codes(["M_CV_003", "M_CV_004", "M_CV_005", "M_CV_006"]), warnings)
    return _page(request, "dashboard/retention.html", "留存与预警",
                 "客户还回来吗？该在什么时候给谁打电话？", ctx, filters, "retention")


def _retention_conclusions(average_curve, churn, dormant) -> list:
    conclusions = []
    if average_curve and len(average_curve) > 1 and average_curve[1] is not None:
        conclusions.append(
            {
                "title": "次月留存率 {}".format(pct(average_curve[1])),
                "body": "定制门是低频耐用消费品，次月留存天然偏低属于正常；关键看**衰减斜率**是否在变缓。"
                "所以建议：把「交付完成后 30 天回访」固化成动作，这是唯一能在首个留存窗口内产生影响的时点。",
                "level": "info",
            }
        )
    if churn:
        first = churn[0]
        conclusions.append(
            {
                "title": "优先触达：{}（{}）".format(first.get("customer_name"), first.get("risk_level") or "中"),
                "body": "{}，累计消费 {}。所以建议：按「累计消费额 × 风险等级」排序分配，"
                "前 20% 的名单由资深销售一对一跟进，其余走标准化话术，避免平均用力。".format(
                    first.get("reason"), money(first.get("monetary"))
                ),
                "level": "danger",
            }
        )
    if dormant and int(dormant.get("customer_count") or 0) > 0:
        conclusions.append(
            {
                "title": "沉睡客户人均历史消费 {}".format(money(dormant.get("avg_spend"))),
                "body": "沉睡客群里仍有大量高历史价值的客户。所以建议：区分「自然沉睡」（已装修完成）与"
                "「受挫沉睡」（有售后台账的客户），后者才是真正可挽回的，优先做这部分的补救式回访。",
                "level": "warn",
            }
        )
    conclusions.append(
        {
            "title": "预警口径说明",
            "body": "预警采用规则法（Top30% 消费 + 超 2 倍复购间隔），而非模型。"
            "因为规则可解释、可复现，业务拿到名单能立刻打电话并说清原因；"
            "模型（见「模型在线」页）用于更精细的排序，两者冲突时以规则为准做触达。",
            "level": "info",
        }
    )
    return conclusions[:4]


# ---------------------------------------------------------------------------
# 七、交付与售后
# ---------------------------------------------------------------------------
@login_required
def delivery(request):
    """交付与售后：订单能不能按时交？延期到底卡在哪一段？"""
    filters = filters_from(request)
    warnings = []
    performance = safe("交付绩效", get_delivery_performance, filters, warnings=warnings, default={}) or {}
    bottlenecks = safe("阶段瓶颈", get_stage_bottleneck, filters, warnings=warnings, default=[]) or []
    after_sales = safe("售后分析", get_after_sales_analysis, filters, warnings=warnings, default={}) or {}

    delay_distribution = performance.get("delay_reason_distribution") or []
    quality_cost = float(after_sales.get("total_cost") or 0)

    kpis = [
        kpi("准时交付率", pct(performance.get("on_time_rate")),
            "分母仅含已交付订单（{} 单）".format(number(performance.get("sample_orders"))),
            None, "good" if float(performance.get("on_time_rate") or 0) >= 0.9 else "danger", "M_DS_001"),
        kpi("平均交付周期", "{} 天".format(number(performance.get("avg_lead_days"), 1)),
            "中位数 {} 天".format(number(performance.get("median_lead_days"), 1)), None, "", "M_DS_002"),
        kpi("延期订单", "{} 单".format(number(performance.get("delayed_orders"))),
            "平均延期 {} 天".format(number(performance.get("avg_delay_days"), 1)), None, "warn", "M_DS_003"),
        kpi("最长交付周期", "{} 天".format(number(performance.get("max_lead_days"))),
            "用于识别极端案例（含特殊尺寸定制）", None, "", "M_DS_002"),
        kpi("售后发生率", pct(after_sales.get("after_sales_rate")),
            "售后工单 {} 单".format(number(after_sales.get("after_sales_count"))),
            None, "warn" if float(after_sales.get("after_sales_rate") or 0) > 0.1 else "good", "M_DS_006"),
        kpi("售后成本率", pct(after_sales.get("cost_rate")),
            "售后成本合计 {}".format(money(quality_cost)),
            None, "danger" if float(after_sales.get("cost_rate") or 0) > 0.02 else "warn", "M_DS_007"),
    ]

    stage_labels = [row.get("stage") for row in bottlenecks]
    option_stage = {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["平均耗时", "瓶颈指数"]},
        "grid": {"left": 90, "right": 70, "top": 50, "bottom": 40},
        "xAxis": {"type": "value", "name": "天"},
        "yAxis": {"type": "category", "data": stage_labels, "inverse": True},
        "series": [
            {"name": "平均耗时", "type": "bar",
             "data": [float(row.get("avg_duration_days") or 0) for row in bottlenecks],
             "itemStyle": {"color": CHART_COLORS[0]},
             "label": {"show": True, "position": "right", "formatter": "{c} 天"}},
            {"name": "瓶颈指数", "type": "scatter",
             "data": [round(float(row.get("bottleneck_score") or 0), 4) for row in bottlenecks],
             "itemStyle": {"color": CHART_COLORS[4]}, "symbolSize": 12},
        ],
    }

    option_delay = {
        "tooltip": {"trigger": "item", "formatter": "{b}: {c} 单（{d}%）"},
        "legend": {"bottom": 0},
        "series": [
            {"name": "延期原因", "type": "pie", "radius": ["42%", "68%"],
             "data": [{"name": row.get("reason"), "value": row.get("order_count")} for row in delay_distribution]}
        ],
    }

    option_after_type = {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["工单数", "成本"]},
        "grid": {"left": 70, "right": 60, "top": 50, "bottom": 60},
        "xAxis": {"type": "category",
                  "data": [row.get("key") for row in (after_sales.get("by_type") or [])],
                  "axisLabel": {"interval": 0, "rotate": 20}},
        "yAxis": [{"type": "value", "name": "工单数"}, {"type": "value", "name": "成本(元)"}],
        "series": [
            {"name": "工单数", "type": "bar",
             "data": [row.get("order_count") for row in (after_sales.get("by_type") or [])],
             "itemStyle": {"color": CHART_COLORS_SOFT[0]}},
            {"name": "成本", "type": "line", "yAxisIndex": 1, "smooth": True,
             "data": [float(row.get("cost") or 0) for row in (after_sales.get("by_type") or [])],
             "itemStyle": {"color": CHART_COLORS[4]}},
        ],
    }

    stage_table = build_table(
        "tblDeliveryStage",
        "交付阶段瓶颈排名（按瓶颈指数降序）",
        [
            {"label": "交付阶段", "key": "stage"},
            {"label": "样本订单数", "key": "order_count", "format": "number"},
            {"label": "平均耗时(天)", "key": "avg_duration_days", "format": "number2"},
            {"label": "超时订单数", "key": "delay_count", "format": "number"},
            {"label": "超时率", "key": "delay_rate", "format": "percent"},
            {"label": "平均超时(天)", "key": "avg_delay_days", "format": "number2"},
            {"label": "瓶颈指数", "key": "bottleneck_score", "format": "number2"},
        ],
        [
            {
                "stage": row.get("stage"),
                "order_count": row.get("order_count"),
                "avg_duration_days": row.get("avg_duration_days"),
                "delay_count": row.get("delay_count"),
                "delay_rate": row.get("delay_rate"),
                "avg_delay_days": row.get("avg_delay_days"),
                "bottleneck_score": row.get("bottleneck_score"),
            }
            for row in bottlenecks
        ],
        caption="交付各阶段的耗时、超时率与瓶颈指数",
        description="瓶颈指数 = 阶段耗时占比 × 超时率：又慢又常超时的阶段才值得投资源去改进。",
    )
    after_table = build_table(
        "tblDeliveryAfterSales",
        "售后类型分布",
        [
            {"label": "售后类型", "key": "key"},
            {"label": "工单数", "key": "order_count", "format": "number"},
            {"label": "占比", "key": "share", "format": "percent"},
            {"label": "成本", "key": "cost", "format": "money"},
            {"label": "平均处理(天)", "key": "avg_handling_days", "format": "number1"},
        ],
        [
            {
                "key": row.get("key"),
                "order_count": row.get("order_count"),
                "share": row.get("share"),
                "cost": row.get("cost"),
                "avg_handling_days": row.get("avg_handling_days"),
            }
            for row in (after_sales.get("by_type") or [])
        ],
        caption="按售后类型统计的工单数、成本与平均处理时长",
        description="工单多的类型管口碑，成本高的类型管利润，两者往往不是同一类。",
    )
    delay_table = build_table(
        "tblDeliveryDelayReason",
        "延期原因分布",
        [
            {"label": "延期原因", "key": "reason"},
            {"label": "订单数", "key": "order_count", "format": "number"},
        ],
        delay_distribution,
        caption="延期订单的原因分布",
        description="单一原因占比超过三成时，它就不是偶发问题而是流程缺陷，应写成排产前置检查项。",
    )

    conclusions = _delivery_conclusions(performance, bottlenecks, after_sales)
    ctx = {
        "warnings": warnings,
        "kpis": kpis,
        "performance": performance,
        "after_sales": after_sales,
        "stage_table": stage_table,
        "after_table": after_table,
        "delay_table": delay_table,
        "charts": [
            chart("chartDeliveryStage", "交付阶段耗时与瓶颈指数", option_stage, 400,
                  "横向条形按耗时排序，最后的阶段就是排产的瓶颈所在。"),
            chart("chartDeliveryDelay", "延期原因分布", option_delay, 320,
                  "原因分布决定改进方向：图纸确认集中 → 前端要加人；物流集中 → 要换承运商。"),
            chart("chartDeliveryAfterSales", "售后类型与成本", option_after_type, 340,
                  "工单多的类型管口碑，成本高的类型管利润，两者未必是同一类。"),
        ],
        "conclusions": conclusions,
    }
    _attach_metric_defs(ctx, _matched_codes(
        ["M_DS_001", "M_DS_002", "M_DS_003", "M_DS_004", "M_DS_005", "M_DS_006", "M_DS_007"]
    ), warnings)
    return _page(request, "dashboard/delivery.html", "交付与售后",
                 "订单能不能按时交？延期到底卡在哪一段？", ctx, filters, "delivery")


def _delivery_conclusions(performance, bottlenecks, after_sales) -> list:
    conclusions = []
    rate = performance.get("on_time_rate")
    if rate is not None:
        conclusions.append(
            {
                "title": "准时交付率 {}".format(pct(rate)),
                "body": "口径说明：分母只含已交付订单（is_on_time 为 0/1），在途订单不计入，，"
                "把在途算进分母会让准时率随订单积压虚假下降。"
                "所以建议：{}".format(
                    "把「延期原因」在每周生产例会上逐条过一遍，连续两周同一原因出现的必须形成改进措施。"
                    if float(rate) < 0.9 else "保持当前排产节奏，同时监控旺季前的产能裕度（准时率是滞后指标）。"
                ),
                "level": "good" if float(rate) >= 0.9 else "danger",
            }
        )

    if bottlenecks:
        top = bottlenecks[0]
        conclusions.append(
            {
                "title": "当前瓶颈阶段：{}".format(top.get("stage")),
                "body": "该阶段平均耗时 {} 天、超时率 {}、瓶颈指数 {}（= 阶段耗时占比 × 超时率）。"
                "所以建议：对这个阶段设一个「在制品上限」，超过上限就不再接新单排产，，"
                "瓶颈工序加量只会让整体周期更长。".format(
                    number(top.get("avg_duration_days"), 1), pct(top.get("delay_rate")),
                    round(float(top.get("bottleneck_score") or 0), 4),
                ),
                "level": "danger",
            }
        )

    delay_distribution = performance.get("delay_reason_distribution") or []
    if delay_distribution:
        top_reason = delay_distribution[0]
        share = (top_reason.get("order_count") or 0) / max(performance.get("delayed_orders") or 1, 1)
        conclusions.append(
            {
                "title": "头号延期原因：{}（{} 单，占延期单的 {}）".format(
                    top_reason.get("reason"), number(top_reason.get("order_count")), pct(share)
                ),
                "body": "单一原因占到三成以上时，它就不再是「偶发问题」而是流程缺陷。"
                "所以建议：把这个原因写成一条排产前置检查项（Checklist），在接单环节就不放行进生产。",
                "level": "warn",
            }
        )

    cost_rate = after_sales.get("cost_rate")
    if cost_rate is not None:
        conclusions.append(
            {
                "title": "售后成本率 {}（成本 {}）".format(pct(cost_rate), money(after_sales.get("total_cost"))),
                "body": "售后成本直接冲减毛利，且常被算作「偶发费用」而被忽视。"
                "所以建议：把售后成本按责任方（生产 / 物流 / 安装 / 客户原因）分摊到对应环节的成本里，"
                "让每个环节看得见自己的质量问题成本。",
                "level": "danger" if float(cost_rate) > 0.02 else "warn",
            }
        )
    return conclusions[:4]


# ---------------------------------------------------------------------------
# 八、回款与现金流
# ---------------------------------------------------------------------------
@login_required
def cashflow(request):
    """回款与现金流：还有多少钱没收回来？未来 13 周能回多少？"""
    filters = filters_from(request)
    warnings = []
    receivables = safe("应收账款", get_receivables, filters, warnings=warnings, default={}) or {}
    weekly = safe("回款预测", get_cashflow_weekly, filters, 13, warnings=warnings, default=[]) or []
    kpi_data = safe("经营总览指标", get_kpi_summary, filters, warnings=warnings, default={}) or {}

    buckets = receivables.get("buckets") or []
    total_outstanding = float(receivables.get("total_outstanding") or 0)
    dso = _compute_dso(receivables, kpi_data)
    forecast_total = sum(float(row.get("expected_amount") or 0) for row in weekly)

    kpis = [
        kpi("应收余额", money(receivables.get("total_outstanding")),
            "以数据基准日 {} 为切点".format(receivables.get("as_of_date") or "-"), None, "primary", "M_CW_004"),
        kpi("逾期金额", money(receivables.get("overdue_amount")),
            "逾期占比 {}".format(pct(receivables.get("overdue_ratio"))), None, "danger", ""),
        kpi("90 天以上应收", money(receivables.get("over_90_amount")),
            "超过 90 天通常需要按坏账计提口径管理", None, "danger", "M_CW_006"),
        kpi("账龄客户数", "{} 人".format(number(receivables.get("customer_count"))),
            "存在未收款的客户数", None, "", ""),
        kpi("DSO（应收周转天数）", "{} 天".format(number(dso, 0)), "应收余额 ÷ 日均成交额",
            None, "warn" if dso and dso > 60 else "good", "M_CW_003"),
        kpi("未来 13 周预测回款", money(forecast_total), "按历史周回收率推演，非财务预算数",
            None, "", "M_CW_008"),
    ]

    aging_labels = [_aging_label(row.get("aging_bucket")) for row in buckets]
    option_aging = {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "legend": {"data": ["应收金额", "客户数"]},
        "grid": {"left": 70, "right": 60, "top": 50, "bottom": 50},
        "xAxis": {"type": "category", "data": aging_labels},
        "yAxis": [{"type": "value", "name": "应收金额(元)"}, {"type": "value", "name": "客户数"}],
        "series": [
            {"name": "应收金额", "type": "bar", "stack": "aging",
             "data": [float(row.get("amount") or 0) for row in buckets],
             "itemStyle": {"color": CHART_COLORS[4]}},
            {"name": "客户数", "type": "line", "yAxisIndex": 1, "smooth": True,
             "data": [row.get("customer_count") for row in buckets],
             "itemStyle": {"color": CHART_COLORS[0]}},
        ],
    }
    option_forecast = {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["预测回款", "剩余应收"]},
        "grid": {"left": 70, "right": 60, "top": 50, "bottom": 50},
        "xAxis": {"type": "category", "data": [row.get("week_label") for row in weekly]},
        "yAxis": [{"type": "value", "name": "回款(元)"}, {"type": "value", "name": "余额(元)"}],
        "series": [
            {"name": "预测回款", "type": "bar",
             "data": [float(row.get("expected_amount") or 0) for row in weekly],
             "itemStyle": {"color": CHART_COLORS[1]}},
            {"name": "剩余应收", "type": "line", "yAxisIndex": 1, "smooth": True,
             "data": [float(row.get("remaining_outstanding") or 0) for row in weekly],
             "itemStyle": {"color": CHART_COLORS[5]}},
        ],
    }

    top_customers = receivables.get("details") or []
    option_top = {
        "tooltip": {"trigger": "axis"},
        "grid": {"left": 100, "right": 70, "top": 40, "bottom": 40},
        "xAxis": {"type": "value", "name": "未收金额(元)"},
        "yAxis": {"type": "category", "inverse": True,
                  "data": [row.get("customer_name") for row in top_customers[:15]]},
        "series": [
            {"name": "未收金额", "type": "bar",
             "data": [float(row.get("outstanding_amount") or 0) for row in top_customers[:15]],
             "itemStyle": {"color": CHART_COLORS[3]},
             "label": {"show": True, "position": "right",
                       "formatter": "{c}"}}
        ],
    }

    aging_table = build_table(
        "tblCashflowAging",
        "应收账龄分布",
        [
            {"label": "账龄桶", "key": "aging_label"},
            {"label": "应收金额", "key": "amount", "format": "money"},
            {"label": "金额占比", "key": "share", "format": "percent"},
            {"label": "订单数", "key": "order_count", "format": "number"},
            {"label": "客户数", "key": "customer_count", "format": "number"},
        ],
        [
            {
                "aging_label": _aging_label(row.get("aging_bucket")),
                "amount": row.get("amount"),
                "share": row.get("share"),
                "order_count": row.get("order_count"),
                "customer_count": row.get("customer_count"),
            }
            for row in buckets
        ],
        caption="按账龄分桶的应收金额、订单数与客户数",
        description="四个账龄桶始终齐全（无数据时为 0），便于跨期对比与固定四段堆叠柱渲染。",
    )
    forecast_table = build_table(
        "tblCashflowForecast",
        "未来 13 周回款预测",
        [
            {"label": "周次", "key": "week_label"},
            {"label": "起始日", "key": "week_start"},
            {"label": "截止日", "key": "week_end"},
            {"label": "预计回款", "key": "expected_amount", "format": "money"},
            {"label": "剩余应收", "key": "remaining_outstanding", "format": "money"},
        ],
        weekly,
        caption="未来 13 周的回款预测与应收余额变化",
        description="假设：恒定周回收率（过去 13 周实际回款 ÷ 期末应收余额，收敛到 1.5%~8%）；只含已发生的应收，不含未来新签合同。",
    )
    top_table = build_table(
        "tblCashflowTop",
        "未收款金额 Top 30 客户",
        [
            {"label": "客户编码", "key": "customer_code"},
            {"label": "客户名称", "key": "customer_name"},
            {"label": "客户类型", "key": "customer_type"},
            {"label": "会员等级", "key": "member_level"},
            {"label": "未收金额", "key": "outstanding_amount", "format": "money"},
            {"label": "涉及订单", "key": "order_count", "format": "number"},
            {"label": "最长逾期(天)", "key": "max_overdue_days", "format": "number"},
        ],
        top_customers[:30],
        caption="按未收款金额排序的客户清单",
        description="排产与备料前先看这张表：应收越集中的客户，越需要先把账对清楚。",
    )

    conclusions = _cashflow_conclusions(receivables, weekly, dso, kpi_data)
    ctx = {
        "warnings": warnings,
        "kpis": kpis,
        "receivables": receivables,
        "aging_table": aging_table,
        "forecast_table": forecast_table,
        "top_table": top_table,
        "charts": [
            chart("chartCashflowAging", "应收账龄分布", option_aging, 340,
                  "90 天以上那根柱子的高度，就是需要计提坏账的部分。"),
            chart("chartCashflowForecast", "未来 13 周回款预测", option_forecast, 360,
                  "折线（剩余应收）下降的斜率就是回收速度；斜率变平说明后面还有硬骨头。"),
            chart("chartCashflowTop", "未收款金额 Top 15 客户", option_top, 420,
                  "排产与备料前先看这张图：应收越集中的客户，越需要先对账。"),
        ],
        "conclusions": conclusions,
    }
    _attach_metric_defs(ctx, _matched_codes(
        ["M_CW_001", "M_CW_002", "M_CW_003", "M_CW_004", "M_CW_005", "M_CW_006", "M_CW_007"]
    ), warnings)
    return _page(request, "dashboard/cashflow.html", "回款与现金流",
                 "还有多少钱没收回来？未来 13 周能回多少？", ctx, filters, "cashflow")


def _compute_dso(receivables, kpi_data) -> float | None:
    """DSO = 应收余额 ÷ 日均成交额（按统计期天数换算）。

    用「日均成交额」而不是「期末余额 ÷ 应收账款周转率」：
    本项目数据跨度只有 3 年且规模在增长，标准周转率公式会因口径不稳定而漂移。
    """
    outstanding = float(receivables.get("total_outstanding") or 0)
    revenue = float(kpi_data.get("revenue") or 0)
    start = kpi_data.get("date_from")
    end = kpi_data.get("date_to")
    days = 365
    if start and end:
        try:
            days = max((date.fromisoformat(end) - date.fromisoformat(start)).days + 1, 1)
        except (TypeError, ValueError):
            days = 365
    if revenue <= 0:
        return None
    return outstanding / (revenue / days)


def _cashflow_conclusions(receivables, weekly, dso, kpi_data) -> list:
    conclusions = []
    total = float(receivables.get("total_outstanding") or 0)
    if total <= 0:
        conclusions.append(
            {"title": "当前无未收款", "body": "应收余额为 0（或账龄表尚未构建）。所以建议：确认数据版本是否包含付款条款数据。", "level": "info"}
        )
        return conclusions

    over_90 = float(receivables.get("over_90_amount") or 0)
    if over_90 > 0:
        conclusions.append(
            {
                "title": "90 天以上应收 {}".format(money(over_90)),
                "body": "占应收总额的 {}。这部分回款概率显著下降，通常要按坏账准备口径管理。"
                "所以建议：对这部分逐户出「对账函 + 还款计划」，并暂停其后续订单的赊销额度。".format(
                    pct(over_90 / total if total else 0)
                ),
                "level": "danger",
            }
        )

    if dso:
        conclusions.append(
            {
                "title": "DSO {} 天".format(number(dso, 0)),
                "body": "DSO 衡量「签单到收钱」的平均占款周期。定制门行业因尾款常绑定安装验收，"
                "DSO 天然高于快消品。所以建议：{}".format(
                    "把尾款收款节点从「验收后 30 天」提前到「安装完成当场」，或设置提前付款折扣。"
                    if dso > 60 else "维持当前收款节奏，把它作为月度资金计划的基准参数。"
                ),
                "level": "warn" if dso > 60 else "good",
            }
        )

    if weekly:
        first4 = sum(float(row.get("expected_amount") or 0) for row in weekly[:4])
        conclusions.append(
            {
                "title": "未来 4 周预计回款 {}".format(money(first4)),
                "body": "该预测基于「恒定周回收率」推演，且**只含已发生的应收**，不含未来新签合同。"
                "所以建议：把它当作排产与备料的现金参考，不要当作财务预算数，，"
                "12 月至次年 1 月门业回款会明显偏慢，预测需要人工下调。",
                "level": "info",
            }
        )

    if kpi_data.get("collection_rate") is not None:
        conclusions.append(
            {
                "title": "回款率 {}".format(pct(kpi_data.get("collection_rate"))),
                "body": "回款率 = 区间回款额 ÷ 区间成交额。它低于 1 并不代表异常，，"
                "因为回款按付款日归集、成交按订单日归集，两个时间轴本就错位。"
                "所以建议：判断回款健康度要看「账龄结构」而不是「回款率是否达到 100%」。",
                "level": "info",
            }
        )
    return conclusions[:4]


# ---------------------------------------------------------------------------
# 九、人效与门店
# ---------------------------------------------------------------------------
@login_required
def workforce(request):
    """人效与门店：谁在创造产值？哪个门店的坪效最高？"""
    filters = filters_from(request)
    warnings = []
    reps = safe("业务员排名", get_rep_ranking, filters, warnings=warnings, default=[]) or []
    stores = safe("门店对比", get_store_comparison, filters, warnings=warnings, default=[]) or []
    kpi_data = safe("经营总览指标", get_kpi_summary, filters, warnings=warnings, default={}) or {}

    total_revenue = sum(float(row.get("revenue") or 0) for row in reps)
    total_profit = sum(float(row.get("gross_profit") or 0) for row in reps)
    active_reps = len([row for row in reps if float(row.get("revenue") or 0) > 0])
    per_rep = total_revenue / active_reps if active_reps else 0.0
    best_store = max(stores, key=lambda row: float(row.get("revenue_per_rep") or 0), default=None)

    kpis = [
        kpi("成交额", money(kpi_data.get("revenue")), "全公司口径", trend_of(kpi_data.get("yoy")), "primary", "M_WF_001"),
        kpi("人均成交额", money(per_rep), "成交额 ÷ 有业绩的业务员数（{} 人）".format(active_reps), None, "", "M_WF_001"),
        kpi("在岗业务员", "{} 人".format(number(active_reps)), "本期内有成交记录的业务员", None, "", "M_WF_002"),
        kpi("人均毛利", money(total_profit / active_reps if active_reps else 0), "毛利额 ÷ 业务员数", None, "", "M_WF_002"),
        kpi("门店数", "{} 家".format(number(len(stores))), "本期有成交的门店", None, "", "M_WF_005"),
        kpi("门店人均产出最优", (best_store or {}).get("store_name") or "-",
            "人均 {}".format(money((best_store or {}).get("revenue_per_rep"))) if best_store else "无数据",
            None, "good", "M_WF_005"),
    ]

    option_rep = {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["成交额", "毛利额", "准时交付率"]},
        "grid": {"left": 70, "right": 60, "top": 50, "bottom": 70},
        "xAxis": {"type": "category", "data": [row.get("rep_name") for row in reps[:15]],
                  "axisLabel": {"interval": 0, "rotate": 30}},
        "yAxis": [{"type": "value", "name": "元"},
                  {"type": "value", "name": "准时率", "max": 100, "axisLabel": {"formatter": "{value}%"}}],
        "series": [
            {"name": "成交额", "type": "bar", "data": [float(row.get("revenue") or 0) for row in reps[:15]],
             "itemStyle": {"color": CHART_COLORS[0]}},
            {"name": "毛利额", "type": "bar", "data": [float(row.get("gross_profit") or 0) for row in reps[:15]],
             "itemStyle": {"color": CHART_COLORS[1]}},
            {"name": "准时交付率", "type": "line", "yAxisIndex": 1, "smooth": True,
             "data": [round(float(row.get("on_time_rate") or 0) * 100, 2) for row in reps[:15]],
             "itemStyle": {"color": CHART_COLORS[2]}},
        ],
    }

    option_store = {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["成交额", "人均成交额", "毛利率"]},
        "grid": {"left": 70, "right": 70, "top": 50, "bottom": 60},
        "xAxis": {"type": "category", "data": [row.get("store_name") for row in stores]},
        "yAxis": [{"type": "value", "name": "元"}, {"type": "value", "name": "毛利率", "axisLabel": {"formatter": "{value}%"}}],
        "series": [
            {"name": "成交额", "type": "bar", "data": [float(row.get("revenue") or 0) for row in stores],
             "itemStyle": {"color": CHART_COLORS[0]}},
            {"name": "人均成交额", "type": "bar",
             "data": [float(row.get("revenue_per_rep") or 0) for row in stores],
             "itemStyle": {"color": CHART_COLORS[1]}},
            {"name": "毛利率", "type": "line", "yAxisIndex": 1, "smooth": True,
             "data": [round(float(row.get("gross_margin") or 0) * 100, 2) for row in stores],
             "itemStyle": {"color": CHART_COLORS[2]}},
        ],
    }

    option_scatter = {
        "tooltip": {"trigger": "item", "formatter": "{c}"},
        "grid": {"left": 70, "right": 40, "top": 40, "bottom": 50},
        "xAxis": {"type": "value", "name": "人均成交额(元)"},
        "yAxis": {"type": "value", "name": "毛利率 %"},
        "series": [
            {
                "name": "门店",
                "type": "scatter",
                "symbolSize": 16,
                "data": [
                    [float(row.get("revenue_per_rep") or 0), round(float(row.get("gross_margin") or 0) * 100, 2),
                     row.get("store_name")]
                    for row in stores
                ],
                "itemStyle": {"color": CHART_COLORS[1]},
            }
        ],
    }

    rep_table = build_table(
        "tblWorkforceRep",
        "业务员业绩排名（按成交额降序，Top 20）",
        [
            {"label": "业务员", "key": "rep_name"},
            {"label": "成交额", "key": "revenue", "format": "money"},
            {"label": "毛利额", "key": "gross_profit", "format": "money"},
            {"label": "毛利率", "key": "gross_margin", "format": "percent"},
            {"label": "订单数", "key": "order_count", "format": "number"},
            {"label": "客户数", "key": "customer_count", "format": "number"},
            {"label": "新增客户", "key": "new_customer_count", "format": "number"},
            {"label": "客单价", "key": "avg_order_value", "format": "money"},
            {"label": "平均折扣率", "key": "avg_discount", "format": "percent2"},
            {"label": "准时交付率", "key": "on_time_rate", "format": "percent"},
        ],
        [
            {
                "rep_name": row.get("rep_name"),
                "revenue": row.get("revenue"),
                "gross_profit": row.get("gross_profit"),
                "gross_margin": row.get("gross_margin"),
                "order_count": row.get("order_count"),
                "customer_count": row.get("customer_count"),
                "new_customer_count": row.get("new_customer_count"),
                "avg_order_value": row.get("avg_order_value"),
                "avg_discount": row.get("avg_discount"),
                "on_time_rate": row.get("on_time_rate"),
            }
            for row in reps[:20]
        ],
        caption="业务员业绩排名，含毛利质量与交付表现",
        description="排名只是入口：把毛利率与准时率并列看，才能分辨「高质量增长」与「靠让价冲量」。",
    )
    store_table = build_table(
        "tblWorkforceStore",
        "门店对比",
        [
            {"label": "门店", "key": "store_name"},
            {"label": "城市", "key": "city"},
            {"label": "成交额", "key": "revenue", "format": "money"},
            {"label": "收入占比", "key": "revenue_share", "format": "percent"},
            {"label": "毛利率", "key": "gross_margin", "format": "percent"},
            {"label": "订单数", "key": "order_count", "format": "number"},
            {"label": "客户数", "key": "customer_count", "format": "number"},
            {"label": "业务员数", "key": "sales_rep_count", "format": "number"},
            {"label": "人均成交额", "key": "revenue_per_rep", "format": "money"},
            {"label": "准时交付率", "key": "on_time_rate", "format": "percent"},
        ],
        [
            {
                "store_name": row.get("store_name"),
                "city": row.get("city"),
                "revenue": row.get("revenue"),
                "revenue_share": row.get("revenue_share"),
                "gross_margin": row.get("gross_margin"),
                "order_count": row.get("order_count"),
                "customer_count": row.get("customer_count"),
                "sales_rep_count": row.get("sales_rep_count"),
                "revenue_per_rep": row.get("revenue_per_rep"),
                "on_time_rate": row.get("on_time_rate"),
            }
            for row in stores
        ],
        caption="门店之间的成交、毛利与人效对比",
        description="人均成交额用于剔除门店规模差异：绝对成交额高，不等于人效高。",
    )

    conclusions = _workforce_conclusions(reps, stores)
    ctx = {
        "warnings": warnings,
        "kpis": kpis,
        "rep_table": rep_table,
        "store_table": store_table,
        "charts": [
            chart("chartWorkforceRep", "业务员成交额、毛利额与准时率", option_rep, 400,
                  "把准时率叠在业绩上看，能发现「冲量但交付差」的同事，，这类业绩迟早要用售后成本买单。"),
            chart("chartWorkforceStore", "门店成交额与人效对比", option_store, 360,
                  "绝对成交额高不代表人效高，关键是看人均那一根柱子。"),
            chart("chartWorkforceScatter", "门店人效 vs 毛利率", option_scatter, 340,
                  "右上象限是「又忙又赚钱」的门店，左下象限需要单独复盘。"),
        ],
        "conclusions": conclusions,
    }
    _attach_metric_defs(ctx, _matched_codes(["M_WF_001", "M_WF_002", "M_WF_003", "M_WF_004", "M_WF_005", "M_WF_006"]), warnings)
    return _page(request, "dashboard/workforce.html", "人效与门店",
                 "谁在创造产值？哪个门店的人效最高？", ctx, filters, "workforce")


def _workforce_conclusions(reps, stores) -> list:
    conclusions = []
    if reps:
        top = reps[0]
        conclusions.append(
            {
                "title": "业绩榜首：{}（{}）".format(top.get("rep_name"), money(top.get("revenue"))),
                "body": "其毛利率 {}、平均折扣 {}、准时交付率 {}。"
                "所以建议：把这位同事的成交过程拆成可复制的动作（跟进节奏、报价话术、老客转介方法），"
                "在周会上做一次案例复盘，而不是只公布排名。".format(
                    pct(top.get("gross_margin")), pct(top.get("avg_discount")), pct(top.get("on_time_rate"))
                ),
                "level": "good",
            }
        )
        low_margin = [
            row for row in reps
            if row.get("gross_margin") is not None and float(row.get("gross_margin")) < 0.25
        ]
        if low_margin:
            names = "、".join(row.get("rep_name") or "" for row in low_margin[:3])
            conclusions.append(
                {
                    "title": "低毛利接单：{}".format(names),
                    "body": "这些同事的成交毛利率低于 25%，而平均折扣并不高，，说明问题可能在品类结构"
                    "（接了太多低毛利品类）而不是让价。所以建议：按品类看这几位的成交构成，"
                    "必要时调整报价权限或客户分配。",
                    "level": "warn",
                }
            )
    if stores:
        best = max(stores, key=lambda row: float(row.get("revenue_per_rep") or 0))
        worst = min(stores, key=lambda row: float(row.get("revenue_per_rep") or 0))
        gap = float(best.get("revenue_per_rep") or 0) - float(worst.get("revenue_per_rep") or 0)
        conclusions.append(
            {
                "title": "门店人效差距 {}".format(money(gap) if gap > 0 else "-"),
                "body": "{} 人均 {}，{} 人均 {}。差距在 30% 以内属于正常波动，"
                "超过 1 倍就必须找结构性原因（客流位置、人员配置、品类结构）。"
                "所以建议：让两家门店各出一份「一周客户来源」统计，用同一张表对比。".format(
                    best.get("store_name"), money(best.get("revenue_per_rep")),
                    worst.get("store_name"), money(worst.get("revenue_per_rep")),
                ),
                "level": "info",
            }
        )
    conclusions.append(
        {
            "title": "人效口径说明",
            "body": "人均产值按「成交额 ÷ 有成交记录的业务员数」计算。"
            "它不包含设计、生产与安装人员，，把全员算进来会摊薄指标，失去对销售团队的诊断力。"
            "所以建议：对外汇报时同时给出「全员人均产值」，两个口径一起看。",
            "level": "info",
        }
    )
    return conclusions[:4]


# ---------------------------------------------------------------------------
# 十、决策模拟
# ---------------------------------------------------------------------------
@login_required
def simulator(request):
    """决策模拟：如果调价、涨价、延期，毛利会怎么变？

    本页是系统的记忆点功能：把「拍脑袋」换成「有假设、有区间、有回测」的推演。
    参数全部来自 :data:`apps.analytics.services.simulator.PARAM_SPEC`，
    表单与后端共用同一份定义，不会出现「前端滑块范围与后端校验不一致」。
    """
    filters = filters_from(request)
    warnings = []
    result = None

    if request.method == "POST":
        params = {spec["key"]: request.POST.get(spec["key"], spec["default"]) for spec in PARAM_SPEC}
        result = safe("决策模拟", run_simulation, params, filters, warnings=warnings, default=None)
        if result is None:
            warnings.append("模拟推演失败，已保留参数表单，请稍后重试。")

    backtest = safe("历史回测", historical_backtest, 2024, 2025, warnings=warnings, default=None)

    # 基准卡：无论有没有提交，都先把当前实际盘摆在眼前
    baseline_card = (result or {}).get("baseline") or {}
    scenario_card = (result or {}).get("scenario") or {}
    delta_card = (result or {}).get("delta") or {}

    kpis = [
        kpi("基准毛利额", money(baseline_card.get("gross_profit")),
            "期间：{}".format(baseline_card.get("period") or filters.describe()), None, "primary", "M_OV_004"),
        kpi("基准毛利率", pct(baseline_card.get("gross_margin")), "毛利额 ÷ 成交额", None, "", "M_OV_005"),
        kpi("情景毛利额", money(scenario_card.get("gross_profit")) if result else "-",
            "{} 个参数调整后的推演结果".format(len(PARAM_SPEC)) if result else "未提交推演",
            None, "", "M_OV_004"),
        kpi("毛利额变化", money(delta_card.get("gross_profit")) if result else "-",
            "{:+.2f} 个百分点".format(float(delta_card.get("margin_points") or 0)) if result else "未提交推演",
            None, "good" if float(delta_card.get("gross_profit") or 0) >= 0 else "danger", "M_OV_005"),
        kpi("准时率变化", ("{:+.2f} 个百分点".format(float(delta_card.get("on_time_rate") or 0) * 100)) if result else "-",
            "交付延期对履约的影响", None, "warn" if result and float(delta_card.get("on_time_rate") or 0) < 0 else "", "M_DS_001"),
        kpi("回测误差率", _backtest_error_text(backtest), "2024 参数预测 2025 的成交额误差",
            None, "good", "M_OV_001"),
    ]

    option_waterfall = _waterfall_option(result)
    option_sensitivity = _sensitivity_option(result)
    option_compare = _compare_option(result)

    ctx = {
        "warnings": warnings,
        "kpis": kpis,
        "param_spec": PARAM_SPEC,
        "params": (result or {}).get("params") or {spec["key"]: spec["default"] for spec in PARAM_SPEC},
        "result": result,
        "baseline": baseline_card,
        "scenario": scenario_card,
        "delta": delta_card,
        "assumptions": (result or {}).get("assumptions") or [],
        "waterfall_rows": (result or {}).get("waterfall") or [],
        "sensitivity_rows": (result or {}).get("sensitivity") or [],
        "backtest": backtest,
        "charts": [
            chart("chartSimulatorWaterfall", "毛利瀑布图：从基准到情景", option_waterfall, 400,
                  "每条台阶就是一项决策的代价或收益，台阶之和严格等于毛利变化。"),
            chart("chartSimulatorTornado", "敏感性分析（单参数扰动）", option_sensitivity, 380,
                  "条带越长，说明该参数对毛利的杠杆越大，，优先管理排在最上面的那个。"),
            chart("chartSimulatorCompare", "基准 vs 情景关键指标对比", option_compare, 340,
                  "对比图只放可比的指标，避免把「元」和「%」画在同一根轴上。"),
        ],
        "conclusions": [(result or {}).get("conclusion")] if result else [],
        "conclusion_text": (result or {}).get("conclusion") or "",
    }
    if result:
        ctx["conclusions"] = [
            {
                "title": "推演结论",
                "body": "{}{}".format(
                    result.get("conclusion"),
                    " 该结论的每一条假设都列在下方「假设清单」里，可逐条核对或替换。" if result.get("assumptions") else "",
                ),
                "level": "good" if float(delta_card.get("gross_profit") or 0) >= 0 else "danger",
            }
        ]
    else:
        ctx["conclusions"] = [
            {
                "title": "怎么用这个模拟器",
                "body": "先在左侧调整参数（不调就是基准），然后点「运行推演」。"
                "建议先单独动一个参数看清它的杠杆，再组合使用，，同时动四个参数会说不清是谁的功劳。",
                "level": "info",
            }
        ]
    if backtest:
        ctx["conclusions"].append(
            {
                "title": "回测：公式在真实数据上误差 {} ".format(
                    "{:.1%}".format(float(backtest.get("revenue", {}).get("error_rate") or 0))
                ),
                "body": "{}{}".format(
                    backtest.get("conclusion") or "",
                    " 所以建议：把模拟结果作为「方向与量级」的参考，用月度实际毛利率做滚动校验。",
                ),
                "level": "info",
            }
        )

    _attach_metric_defs(ctx, _matched_codes(
        ["M_OV_001", "M_OV_004", "M_OV_005", "M_CP_009", "M_DS_001", "M_DS_006"]
    ), warnings)
    return _page(request, "dashboard/simulator.html", "决策模拟",
                 "如果提价 3%、材料再涨 5%，毛利会变成多少？", ctx, filters, "simulator")


def _backtest_error_text(backtest) -> str:
    if not backtest:
        return "-"
    error = (backtest.get("revenue") or {}).get("error_rate")
    if error is None:
        return "-"
    return "{:+.1%}".format(float(error))


def _waterfall_option(result) -> dict:
    """瀑布图：基准 → 各归因台阶 → 情景。

    用「透明底座 + 可见条」的经典瀑布写法：底座用 ``stack`` 的隐形系列实现，
    这样每根柱子的**高度**是增量、**位置**是累计值，符合财务归因的阅读习惯。
    """
    steps = (result or {}).get("waterfall") or []
    if not steps:
        return {"xAxis": {"type": "category", "data": []}, "yAxis": {"type": "value"}, "series": []}

    names = [step.get("name") for step in steps]
    bases = []
    heights = []
    colors = []
    running = 0.0
    for index, step in enumerate(steps):
        value = float(step.get("value") or 0)
        kind = step.get("type")
        if index == 0 or kind == "total":
            bases.append(0.0)
            heights.append(value)
            colors.append(CHART_COLORS[0])
            running = value
        else:
            if value >= 0:
                bases.append(running)
                heights.append(value)
                colors.append(CHART_COLORS[1])
            else:
                bases.append(running + value)
                heights.append(abs(value))
                colors.append(CHART_COLORS[4])
            running += value

    return {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "grid": {"left": 80, "right": 40, "top": 40, "bottom": 80},
        "xAxis": {"type": "category", "data": names, "axisLabel": {"interval": 0, "rotate": 25}},
        "yAxis": {"type": "value", "name": "毛利额(元)"},
        "series": [
            {"name": "底座", "type": "bar", "stack": "waterfall", "itemStyle": {"color": "transparent"},
             "emphasis": {"itemStyle": {"color": "transparent"}}, "data": [round(base, 2) for base in bases]},
            {
                "name": "毛利贡献",
                "type": "bar",
                "stack": "waterfall",
                "data": [
                    {"value": round(height, 2), "itemStyle": {"color": colors[index]}}
                    for index, height in enumerate(heights)
                ],
                "label": {"show": True, "position": "top", "formatter": "{c}"},
            },
        ],
    }


def _sensitivity_option(result) -> dict:
    """敏感性 tornado 图：每个参数在低/高取值下的毛利区间，按摆动幅度排序。"""
    items = (result or {}).get("sensitivity") or []
    if not items:
        return {"xAxis": {"type": "value"}, "yAxis": {"type": "category", "data": []}, "series": []}

    labels = [item.get("param_label") for item in reversed(items)]
    base = float((result or {}).get("scenario", {}).get("gross_profit") or 0)
    low_values = [float(item.get("low") or 0) - base for item in reversed(items)]
    high_values = [float(item.get("high") or 0) - base for item in reversed(items)]

    return {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "grid": {"left": 110, "right": 40, "top": 40, "bottom": 50},
        "xAxis": {"type": "value", "name": "相对情景毛利的变动(元)"},
        "yAxis": {"type": "category", "data": labels},
        "series": [
            {"name": "低取值", "type": "bar", "stack": "range", "itemStyle": {"color": CHART_COLORS[4]},
             "data": low_values},
            {"name": "高取值", "type": "bar", "stack": "range", "itemStyle": {"color": CHART_COLORS[1]},
             "data": high_values},
        ],
    }


def _compare_option(result) -> dict:
    """基准 vs 情景对比：用「相对基准的变化率」而不是绝对值。"""
    if not result:
        return {"xAxis": {"type": "category", "data": []}, "yAxis": {"type": "value"}, "series": []}
    baseline = result.get("baseline") or {}
    scenario = result.get("scenario") or {}
    metrics = [
        ("成交额", "revenue"),
        ("毛利额", "gross_profit"),
        ("材料成本", "material_cost"),
        ("售后成本", "after_sales_cost"),
        ("回款额", "collection_amount"),
    ]
    labels = []
    ratios = []
    for label, key in metrics:
        base_value = float(baseline.get(key) or 0)
        scenario_value = float(scenario.get(key) or 0)
        labels.append(label)
        ratios.append(round((scenario_value - base_value) / base_value * 100, 2) if base_value else 0.0)
    return {
        "tooltip": {"trigger": "axis", "formatter": "{b}: {c}%"},
        "grid": {"left": 70, "right": 40, "top": 40, "bottom": 50},
        "xAxis": {"type": "category", "data": labels},
        "yAxis": {"type": "value", "name": "相对基准变化 %"},
        "series": [
            {
                "name": "变化率",
                "type": "bar",
                "data": [
                    {"value": value, "itemStyle": {"color": CHART_COLORS[1] if value >= 0 else CHART_COLORS[4]}}
                    for value in ratios
                ],
                "label": {"show": True, "position": "top", "formatter": "{c}%"},
            }
        ],
    }


# ---------------------------------------------------------------------------
# 十一、数据质量
# ---------------------------------------------------------------------------
@login_required
def data_quality(request):
    """数据质量：这批数据能信吗？哪些字段有问题？"""
    warnings = []
    summary = safe("数据质量摘要", get_data_quality_summary, warnings=warnings, default={"has_run": False}) or {}

    trend_runs = []
    try:
        from apps.dq.models import DQRun

        queryset = DQRun.objects.all().order_by("-id")[:30]
        for run in reversed(list(queryset)):
            trend_runs.append(
                {
                    "id": run.id,
                    "trigger": getattr(run, "trigger", ""),
                    "total_checks": getattr(run, "total_checks", 0) or 0,
                    "passed": getattr(run, "passed", 0) or 0,
                    "warned": getattr(run, "warned", 0) or 0,
                    "failed": getattr(run, "failed", 0) or 0,
                    "pass_rate": round(
                        float(getattr(run, "passed", 0) or 0) / max(int(getattr(run, "total_checks", 0) or 0), 1) * 100, 2
                    ),
                    "duration_ms": getattr(run, "duration_ms", 0) or 0,
                }
            )
    except Exception as exc:  # pragma: no cover - DQ 表可能尚未建立
        logger.warning("数据质量历史读取失败：%s", exc)
        warnings.append("历史校验记录暂时无法读取，仅展示最近一次结果。")

    results = summary.get("results") or []
    kpis = [
        kpi("校验是否健康", "健康" if summary.get("is_healthy") else "存在失败项",
            "无 error 级失败即视为健康", None,
            "good" if summary.get("is_healthy") else "danger", ""),
        kpi("规则总数", "{} 条".format(number(summary.get("total_checks"))), "本次执行的校验规则数", None, "", ""),
        kpi("通过", "{} 条".format(number(summary.get("passed"))), "通过率 {}".format(pct(_pass_rate(summary))), None, "good", ""),
        kpi("警告", "{} 条".format(number(summary.get("warned"))), "warning 级差异（如手工抹零）在门厂属常态", None, "warn", ""),
        kpi("失败", "{} 条".format(number(summary.get("failed"))), "error 级差异，需要人工核查", None, "danger", ""),
        kpi("执行耗时", "{} ms".format(number(summary.get("duration_ms"))),
            "触发方式：{}".format(summary.get("trigger") or "-"), None, "", ""),
    ]

    status_labels = ["pass", "warn", "fail"]
    status_counts = [sum(1 for row in results if row.get("status") == status) for status in status_labels]
    option_status = {
        "tooltip": {"trigger": "item", "formatter": "{b}: {c} 条"},
        "legend": {"bottom": 0},
        "series": [
            {"name": "规则状态", "type": "pie", "radius": ["45%", "70%"],
             "data": [
                 {"name": "通过", "value": status_counts[0], "itemStyle": {"color": CHART_COLORS[1]}},
                 {"name": "警告", "value": status_counts[1], "itemStyle": {"color": CHART_COLORS[2]}},
                 {"name": "失败", "value": status_counts[2], "itemStyle": {"color": CHART_COLORS[4]}},
             ]}
        ],
    }

    option_trend = {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["通过率", "失败规则数"]},
        "grid": {"left": 60, "right": 60, "top": 50, "bottom": 40},
        "xAxis": {"type": "category", "data": ["#{}".format(row["id"]) for row in trend_runs]},
        "yAxis": [{"type": "value", "name": "通过率", "max": 100, "axisLabel": {"formatter": "{value}%"}},
                  {"type": "value", "name": "失败数"}],
        "series": [
            {"name": "通过率", "type": "line", "smooth": True,
             "data": [row["pass_rate"] for row in trend_runs], "itemStyle": {"color": CHART_COLORS[1]}},
            {"name": "失败规则数", "type": "bar",
             "data": [row["failed"] for row in trend_runs], "itemStyle": {"color": CHART_COLORS[4]}},
        ],
    }

    status_text = {"pass": "通过", "warn": "警告", "fail": "失败"}
    rule_table = build_table(
        "tblDqRules",
        "校验规则结果（按规则顺序）",
        [
            {"label": "规则编码", "key": "rule_code"},
            {"label": "规则名称", "key": "rule_name"},
            {"label": "分类", "key": "category"},
            {"label": "级别", "key": "severity"},
            {"label": "状态", "key": "status_text"},
            {"label": "检查行数", "key": "checked_rows", "format": "number"},
            {"label": "问题行数", "key": "issue_rows", "format": "number"},
            {"label": "问题占比", "key": "issue_ratio", "format": "percent2"},
            {"label": "说明", "key": "message"},
            {"label": "问题样本", "key": "sample_keys"},
        ],
        [
            {
                "rule_code": row.get("rule_code"),
                "rule_name": row.get("rule_name"),
                "category": row.get("category"),
                "severity": "error" if row.get("severity") == "error" else "warning",
                "status_text": status_text.get(row.get("status"), row.get("status")),
                "checked_rows": row.get("checked_rows"),
                "issue_rows": row.get("issue_rows"),
                "issue_ratio": row.get("issue_ratio"),
                "message": row.get("message"),
                "sample_keys": "、".join(str(key) for key in (row.get("sample_keys") or [])[:5]) or "-",
            }
            for row in results
        ],
        caption="本次数据质量校验的逐条规则结果",
        description="「问题样本」列给出问题记录的业务主键，可直接在明细页检索定位；失败样本的处理原则是「先分清真错还是口径过严」。",
    )
    failed_table = build_table(
        "tblDqFailed",
        "失败规则明细（error 级）",
        [
            {"label": "规则编码", "key": "rule_code"},
            {"label": "规则名称", "key": "rule_name"},
            {"label": "问题行数", "key": "issue_rows", "format": "number"},
            {"label": "问题占比", "key": "issue_ratio", "format": "percent2"},
            {"label": "说明", "key": "message"},
            {"label": "问题样本", "key": "sample_keys_text"},
        ],
        [
            {
                "rule_code": row.get("rule_code"),
                "rule_name": row.get("rule_name"),
                "issue_rows": row.get("issue_rows"),
                "issue_ratio": row.get("issue_ratio"),
                "message": row.get("message"),
                "sample_keys_text": "、".join(str(key) for key in (row.get("sample_keys") or [])[:5]) or "-",
            }
            for row in (summary.get("failed_rules") or [])
        ],
        caption="error 级失败规则与样本主键",
        description="失败项会直接影响指标可信度：先定位样本，再决定是修数据还是改规则，两种情况都要留痕。",
    )

    conclusions = _dq_conclusions(summary, results)
    ctx = {
        "warnings": warnings,
        "kpis": kpis,
        "summary": summary,
        "rule_table": rule_table,
        "failed_table": failed_table,
        "charts": [
            chart("chartDqStatus", "本次校验规则状态分布", option_status, 320,
                  "失败项必须先确认真实数据问题还是规则本身过严，再决定改数据还是改规则。"),
            chart("chartDqTrend", "近 30 次校验通过率与失败数", option_trend, 340,
                  "通过率突然下滑的那一天，往往对应着一次不规范的导入。"),
        ],
        "conclusions": conclusions,
    }
    _attach_metric_defs(ctx, [], warnings)
    return _page(request, "dashboard/data_quality.html", "数据质量",
                 "这批数据能信吗？哪些字段有问题？", ctx, None, "data_quality")


def _pass_rate(summary) -> float | None:
    total = int(summary.get("total_checks") or 0)
    if not total:
        return None
    return float(summary.get("passed") or 0) / total


def _dq_conclusions(summary, results) -> list:
    conclusions = []
    if not summary.get("has_run"):
        return [
            {
                "title": "尚未执行数据质量校验",
                "body": "没有校验记录时不要显示「全部通过」，，0 会被误读成「完美」。"
                "所以建议：先在「数据质量」入口执行一次全量校验，再回来看结果。",
                "level": "warn",
            }
        ]

    failed = summary.get("failed_rules") or []
    if failed:
        names = "、".join("{} {}".format(row.get("rule_code"), row.get("rule_name")) for row in failed[:3])
        conclusions.append(
            {
                "title": "存在 {} 条 error 级失败".format(len(failed)),
                "body": "代表规则：{}。失败规则会直接影响指标可信度，必须优先处理。"
                "所以建议：逐条打开失败样本，先判断是「数据错」还是「规则口径过严」，"
                "前者修数据、后者改规则，两种情况都要在系统里留痕。".format(names or "-"),
                "level": "danger",
            }
        )

    warned = summary.get("warned_rules") or []
    if warned:
        conclusions.append(
            {
                "title": "{} 条 warning 级差异".format(len(warned)),
                "body": "本项目的判定策略是：只有 error 级失败才算不健康。"
                "因为门厂的数据里存在合理例外（手工抹零、个别亏损接单），把它们一律判 fail 会让告警失去意义。"
                "所以建议：为每条 warning 规则维护一份「可接受例外清单」，按季度复核一次。",
                "level": "warn",
            }
        )
    else:
        conclusions.append(
            {
                "title": "本次校验无失败项",
                "body": "当前批次数据通过全部 error 级规则。所以建议：把本次结果作为数据版本基线记录下来，"
                "后续指标异常时先与这条基线对比，排除数据本身的问题。",
                "level": "good",
            }
        )

    conclusions.append(
        {
            "title": "口径说明",
            "body": "通过率 = 通过规则数 ÷ 执行规则数。规则按 error / warning 两级处理，"
            "单条规则自身执行异常按 warning 处理并保留异常信息，，直接判 fail 会淹没真实数据问题。",
            "level": "info",
        }
    )
    return conclusions[:3]


# ---------------------------------------------------------------------------
# 十二、模型在线
# ---------------------------------------------------------------------------
@login_required
def model_online(request):
    """模型在线：模型到底靠不靠谱？这个订单为什么被打了高分？"""
    warnings = []
    overview = safe("模型概览", get_model_overview, warnings=warnings, default={}) or {}
    scoring_results = overview.get("scoring_results") or []
    active_versions = overview.get("active_versions") or []

    kpis = [
        kpi("模型版本总数", "{} 个".format(number(overview.get("version_count"))), "含历史版本", None, "", ""),
        kpi("在线版本", "{} 个".format(number(overview.get("active_count"))), "is_active=True 的版本", None, "good", ""),
        kpi("任务类型", "{} 类".format(number(len(overview.get("tasks") or []))),
            "、".join(overview.get("tasks") or []) or "尚无模型", None, "", ""),
        kpi("最新评分记录", "{} 条".format(number(len(scoring_results))), "最近 200 条评分结果", None, "", ""),
        kpi("最高 AUC", "{:.4f}".format(max([float(row.get("auc") or 0) for row in active_versions] or [0])),
            "分类任务（流失预警）的区分能力", None, "good", ""),
        kpi("最低 MAPE", _best_mape(active_versions), "回归任务（销量/金额预测）的误差", None, "good", ""),
    ]

    feature_source = next(
        (row for row in active_versions if row.get("feature_importance")), None
    ) or next((row for row in (overview.get("all_versions") or []) if row.get("feature_importance")), None)
    feature_rows = _feature_rows(feature_source)
    option_feature = {
        "tooltip": {"trigger": "axis"},
        "grid": {"left": 140, "right": 60, "top": 40, "bottom": 40},
        "xAxis": {"type": "value", "name": "重要性"},
        "yAxis": {"type": "category", "inverse": True, "data": [row["feature"] for row in feature_rows]},
        "series": [
            {"name": "特征重要性", "type": "bar", "data": [row["importance"] for row in feature_rows],
             "itemStyle": {"color": CHART_COLORS[0]},
             "label": {"show": True, "position": "right", "formatter": "{c}"}}
        ],
    }

    feature_table = build_table(
        "tblModelFeature",
        "特征重要性 Top 12",
        [
            {"label": "特征", "key": "feature"},
            {"label": "重要性", "key": "importance", "format": "number2"},
        ],
        feature_rows,
        caption="模型的特征重要性排序",
        description="特征重要性说明「模型在看什么」，是向业务解释预测结果的最短路径；前三位通常就能讲清模型的判断逻辑。",
    )

    version_rows = [
        {
            "task": row.get("task"),
            "version": row.get("version"),
            "algo": row.get("algo"),
            "is_active_text": "在线" if row.get("is_active") else "历史",
            "train_period": "{} ~ {}".format(row.get("train_period_start") or "-", row.get("train_period_end") or "-"),
            "sample_count": row.get("sample_count"),
            "feature_count": row.get("feature_count"),
            "auc": row.get("auc"),
            "ks": row.get("ks"),
            "mae": row.get("mae"),
            "mape": row.get("mape"),
            "r2": row.get("r2"),
            "baseline_name": row.get("baseline_name"),
            "baseline_auc": row.get("baseline_auc"),
            "baseline_mae": row.get("baseline_mae"),
            "improvement": _improvement_text(row),
            "note": row.get("note"),
        }
        for row in (overview.get("all_versions") or [])
    ]
    version_table = build_table(
        "tblModelVersions",
        "模型版本与评估指标",
        [
            {"label": "任务", "key": "task"},
            {"label": "版本", "key": "version"},
            {"label": "算法", "key": "algo"},
            {"label": "状态", "key": "is_active_text"},
            {"label": "训练窗口", "key": "train_period"},
            {"label": "样本量", "key": "sample_count", "format": "number"},
            {"label": "特征数", "key": "feature_count", "format": "number"},
            {"label": "AUC", "key": "auc", "format": "number2"},
            {"label": "KS", "key": "ks", "format": "number2"},
            {"label": "MAE", "key": "mae", "format": "number2"},
            {"label": "MAPE", "key": "mape", "format": "percent"},
            {"label": "R²", "key": "r2", "format": "number2"},
            {"label": "基线模型", "key": "baseline_name"},
            {"label": "相对基线改善", "key": "improvement"},
            {"label": "备注", "key": "note"},
        ],
        version_rows,
        caption="全部模型版本及其评估指标与基线对比",
        description="评估指标必须连同基线一起看：高 AUC 但相对基线无改善的模型，说明复杂度没有换来效果。",
    )

    # 预测 vs 实际回看：用评分结果的 score 与 predicted_value 交叉对照
    option_backtest = _model_backtest_option(scoring_results)

    scored_table = build_table(
        "tblModelScored",
        "最近评分结果（Top 50）",
        [
            {"label": "任务", "key": "task"},
            {"label": "评分对象", "key": "subject_key"},
            {"label": "对象类型", "key": "subject_type"},
            {"label": "模型评分", "key": "score", "format": "number2"},
            {"label": "预测标签", "key": "predicted_label"},
            {"label": "预测值", "key": "predicted_value", "format": "number2"},
            {"label": "Top 因子", "key": "top_factors_text"},
            {"label": "人话解释", "key": "explanation"},
        ],
        [
            {
                "task": row.get("task"),
                "subject_key": row.get("subject_key"),
                "subject_type": row.get("subject_type"),
                "score": row.get("score"),
                "predicted_label": row.get("predicted_label"),
                "predicted_value": row.get("predicted_value"),
                # Top 因子可能是 [{"feature": ..., "value": ...}] 或 ["材质等级", ...] 两种形态，
                # 统一成一行中文文本，避免把原始结构直接丢给模板
                "top_factors_text": "、".join(_factor_text(row.get("top_factors"))) or "-",
                "explanation": row.get("explanation") or "-",
            }
            for row in scoring_results[:50]
        ],
        caption="模型输出与业务可读解释的对照表",
        description="模型给出分值，人工给出解释与动作，，这是业务方接受模型的唯一方式；解释列不允许留空。",
    )

    query_result = None
    query_key = ""
    if request.method == "POST":
        query_key = (request.POST.get("subject_key") or "").strip()
        if query_key:
            query_result = next(
                (
                    row for row in scoring_results
                    if str(row.get("subject_key")) == query_key
                    or str(_extract_code(row.get("subject_key"))) == query_key
                ),
                None,
            )
            if query_result is None:
                warnings.append("未找到「{}」的评分记录，请确认客户编码或订单号是否正确。".format(query_key))

    conclusions = _model_conclusions(active_versions, scoring_results)
    ctx = {
        "warnings": warnings,
        "kpis": kpis,
        "overview": overview,
        "version_table": version_table,
        "feature_table": feature_table,
        "scored_table": scored_table,
        "query_result": query_result,
        "query_key": query_key,
        "charts": [
            chart("chartModelFeature", "特征重要性 Top 12", option_feature, 400,
                  "特征重要性说明「模型在看什么」，也是向业务解释预测结果的最短路径。"),
            chart("chartModelBacktest", "预测分与实际结果的回看对照", option_backtest, 340,
                  "回看图用于判断模型是否在近期的数据分布上漂移（线上衰减）。"),
        ],
        "conclusions": conclusions,
    }
    _attach_metric_defs(ctx, [], warnings)
    return _page(request, "dashboard/model_online.html", "模型在线",
                 "模型到底靠不靠谱？这个客户为什么被打了高分？", ctx, None, "models")


def _factor_text(top_factors) -> list:
    """把 ``top_factors`` 统一成中文文本列表。

    ``ScoringResult.top_factors`` 在本项目里可能是
    ``[{"feature": "交付准时率", "value": 0.32}, ...]`` 或 ``["交付准时率", ...]``，
    两种形态都由这里收敛，，模板不应该关心模型输出了哪种结构。
    """
    items = []
    for factor in top_factors or ():
        if isinstance(factor, dict):
            name = factor.get("feature") or factor.get("name") or factor.get("key")
            weight = factor.get("value") if factor.get("value") is not None else factor.get("importance")
            items.append("{}（{}）".format(name, weight) if name and weight is not None else str(name or weight or "-"))
        else:
            items.append(str(factor))
    return items


def _best_mape(active_versions) -> str:
    values = [float(row.get("mape") or 0) for row in active_versions if row.get("mape") is not None]
    if not values:
        return "-"
    return "{:.1%}".format(min(values))


def _feature_rows(version) -> list:
    """特征重要性 → 排序后的列表。

    ``feature_importance`` 可能是 dict（``{"材质等级": 0.21}``）也可能已是列表，
    这里统一成 ``[{feature, importance}]``，兼容两种存储形态。
    """
    if not version:
        return []
    raw = version.get("feature_importance")
    items = []
    if isinstance(raw, dict):
        items = [{"feature": str(key), "importance": float(value or 0)} for key, value in raw.items()]
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, dict):
                items.append(
                    {
                        "feature": str(item.get("feature") or item.get("name") or ""),
                        "importance": float(item.get("importance") or item.get("value") or 0),
                    }
                )
    items.sort(key=lambda row: -row["importance"])
    return items[:12]


def _improvement_text(row) -> str:
    """相对基线模型的改善幅度。"""
    parts = []
    auc, baseline_auc = row.get("auc"), row.get("baseline_auc")
    if auc is not None and baseline_auc:
        parts.append("AUC {:+.1%}".format((float(auc) - float(baseline_auc)) / float(baseline_auc)))
    mae, baseline_mae = row.get("mae"), row.get("baseline_mae")
    if mae is not None and baseline_mae:
        parts.append("MAE {:+.1%}".format((float(mae) - float(baseline_mae)) / float(baseline_mae)))
    return " / ".join(parts) if parts else "-"


def _extract_code(subject_key) -> str:
    """评分主体的业务编码：``customer:HB0001`` / ``order:SO20250001`` 取冒号后半段。"""
    text = str(subject_key or "")
    return text.split(":")[-1] if ":" in text else text


def _model_backtest_option(scoring_results) -> dict:
    """预测分与实际值的回看散点/折线。

    评分结果里同时存在 ``score``（如流失概率）与 ``predicted_value``（预测金额），
    这里画成「评分序列 + 预测值」的双轴回看，用于观察模型输出是否稳定。
    """
    rows = scoring_results[:60]
    labels = [_extract_code(row.get("subject_key")) for row in rows]
    return {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["模型评分", "预测值"]},
        "grid": {"left": 60, "right": 60, "top": 50, "bottom": 80},
        "xAxis": {"type": "category", "data": labels, "axisLabel": {"rotate": 40, "interval": 0}},
        "yAxis": [{"type": "value", "name": "评分", "max": 1},
                  {"type": "value", "name": "预测值"}],
        "series": [
            {"name": "模型评分", "type": "line", "smooth": True,
             "data": [row.get("score") for row in rows], "itemStyle": {"color": CHART_COLORS[0]}},
            {"name": "预测值", "type": "scatter", "yAxisIndex": 1,
             "data": [row.get("predicted_value") for row in rows], "itemStyle": {"color": CHART_COLORS[2]}},
        ],
    }


def _model_conclusions(active_versions, scoring_results) -> list:
    conclusions = []
    if not active_versions:
        return [
            {
                "title": "暂无在线模型",
                "body": "模型版本表为空，或尚无 is_active=True 的版本。"
                "所以建议：先确认模型训练任务是否已执行，再回看本页。",
                "level": "warn",
            }
        ]

    for version in active_versions[:1]:
        task = version.get("task") or "模型"
        auc = version.get("auc")
        mae = version.get("mae")
        detail = []
        if auc is not None:
            detail.append("AUC {:.4f}".format(float(auc)))
        if mae is not None:
            detail.append("MAE {:.2f}".format(float(mae)))
        if version.get("mape") is not None:
            detail.append("MAPE {:.1%}".format(float(version["mape"] or 0)))
        conclusions.append(
            {
                "title": "在线模型：{} v{}".format(task, version.get("version")),
                "body": "算法 {}，训练窗口 {} ~ {}，样本 {} 条。评估指标：{}。"
                "所以建议：把该模型的输出用在「排序」而不是「判决」上，，"
                "模型给出优先级，最终是否触达由业务规则与客户经理判断。".format(
                    version.get("algo") or "-", version.get("train_period_start") or "-",
                    version.get("train_period_end") or "-", number(version.get("sample_count")),
                    "、".join(detail) or "暂无评估指标",
                ),
                "level": "good",
            }
        )

    if scoring_results:
        first = scoring_results[0]
        conclusions.append(
            {
                "title": "单条评分可解释：{}".format(_extract_code(first.get("subject_key"))),
                "body": "评分 {:.4f}，主要因子：{}。".format(
                    float(first.get("score") or 0),
                    "、".join(
                        str(item.get("feature") or item.get("name") or item)
                        for item in (first.get("top_factors") or [])[:3]
                    ) or "-",
                ) + "模型给出分值，人工给出解释与动作，，这是业务方接受模型的唯一方式。"
                "所以建议：在触达记录里回写「预测是否命中」，形成模型的线上监控闭环。",
                "level": "info",
            }
        )

    conclusions.append(
        {
            "title": "评估指标的读法",
            "body": "AUC 衡量排序能力（0.5 等于随机，0.7 以上可用）；MAE / MAPE 衡量预测误差；"
            "R² 衡量解释度。只看一个指标会被误导，，"
            "高 AUC 低稳定性的模型在生产环境里比中等 AUC 稳定模型更危险。"
            "所以建议：每次上线新版本都保留与基线模型的对比列，不达标不上线。",
            "level": "info",
        }
    )
    return conclusions[:3]


# ---------------------------------------------------------------------------
# 十三、指标字典
# ---------------------------------------------------------------------------
@login_required
def metric_dictionary(request):
    """指标字典：这些数字到底是怎么算出来的？

    本页是整个系统的「说明书」：业务同事看到任何不理解的口径，
    都应该能在这一页按编码找到定义、公式、分子分母与易误读提醒。
    """
    warnings = []
    keyword = (request.GET.get("q") or "").strip()
    domain = (request.GET.get("domain") or "").strip()

    definitions = list(METRIC_DEFINITIONS)
    if domain:
        definitions = [item for item in definitions if item.get("domain") == domain]
    if keyword:
        lowered = keyword.lower()
        definitions = [
            item for item in definitions
            if lowered in str(item.get("code", "")).lower()
            or lowered in str(item.get("name", "")).lower()
            or lowered in str(item.get("definition", "")).lower()
        ]

    groups = []
    for item in definitions:
        domain_name = item.get("domain") or "未分组"
        if not groups or groups[-1]["domain"] != domain_name:
            groups.append({"domain": domain_name, "metrics": []})
        groups[-1]["metrics"].append(item)

    domain_options = []
    for item in METRIC_DEFINITIONS:
        name = item.get("domain")
        if name and name not in [row["value"] for row in domain_options]:
            domain_options.append({"value": name, "label": name})

    tree_option = _metric_tree_option()
    type_counts = {}
    for item in METRIC_DEFINITIONS:
        key = item.get("metric_type") or "unknown"
        type_counts[key] = type_counts.get(key, 0) + 1
    type_labels = {"amount": "金额", "count": "数量", "ratio": "比率", "index": "指数", "growth": "增长率"}

    option_type = {
        "tooltip": {"trigger": "item", "formatter": "{b}: {c} 条 ({d}%)"},
        "legend": {"bottom": 0},
        "series": [
            {"name": "指标类型", "type": "pie", "radius": ["45%", "70%"],
             "data": [{"name": type_labels.get(key, key), "value": value} for key, value in type_counts.items()]}
        ],
    }

    kpis = [
        kpi("指标总数", "{} 条".format(number(len(METRIC_DEFINITIONS))), "全部为代码内定义，随版本发布", None, "primary", ""),
        kpi("覆盖域", "{} 个".format(number(len(domain_options))), "与导航模块一一对应", None, "", ""),
        kpi("拆解关系", "{} 条".format(number(len(METRIC_TREE_SPEC))), "父子指标之间的拆解与驱动关系", None, "", ""),
        kpi("比率类指标", "{} 条".format(number(type_counts.get("ratio", 0))), "存储 0~1，展示乘 100（全场统一约定）", None, "warn", ""),
        kpi("当前筛选结果", "{} 条".format(number(len(definitions))), "按关键词 / 域筛选后", None, "", ""),
    ]

    conclusions = [
        {
            "title": "为什么把口径写成代码而不是文档",
            "body": "经营分析系统最大的风险不是算错，而是同一个词在两处含义不同。"
            "所以建议：所有口径变更都走 METRIC_DEFINITIONS 这一处修改，"
            "并在版本号（version 字段）上留痕，便于回溯历史报表用的是哪一版口径。",
            "level": "info",
        },
        {
            "title": "比率类指标的约定",
            "body": "全部比率类指标（准时率、毛利率、占比等）在系统内**存储为 0~1 的小数**，"
            "仅在展示层乘 100。所以建议：跨系统对接时先确认对方拿到的到底是 0.35 还是 35，"
            "这是最容易出错、后果最隐蔽的一类口径事故。",
            "level": "warn",
        },
        {
            "title": "留意每条指标的 caveat",
            "body": "每条指标都带「易误读提醒」，例如准时率的分母排除在途订单、"
            "成交额与回款额分属两个时间轴。所以建议：把 caveat 直接放进报表导出的表头注释，"
            "让数据离开系统后仍然带着口径说明。",
            "level": "info",
        },
    ]

    ctx = {
        "warnings": warnings,
        "kpis": kpis,
        "groups": groups,
        "domain_options": domain_options,
        "keyword": keyword,
        "selected_domain": domain,
        "tree_spec": METRIC_TREE_SPEC,
        "charts": [
            chart("chartMetricTree", "指标拆解树（点击节点可下钻该指标）", tree_option, 460,
                  "树的层级就是分析的钻取路径：成交额 → 量与价 → 品类与渠道。"),
            chart("chartMetricType", "指标类型分布", option_type, 320,
                  "比率类指标最容易出问题，因为它涉及「0~1 还是 0~100」的约定。"),
        ],
        "conclusions": conclusions,
    }
    ctx["metric_definitions"] = definitions
    return _page(request, "dashboard/metric_dictionary.html", "指标字典",
                 "这些数字到底是怎么算出来的？", ctx, None, "metrics")


def _metric_tree_option() -> dict:
    """用 METRIC_TREE_SPEC 构建 ECharts tree。

    父指标可能出现多次（例如毛利额既有「成交额」子节点又有「毛利率」子节点），
    因此先按 parent_code 聚合再组装，避免重复建节点。
    """
    children = {}
    for relation in METRIC_TREE_SPEC:
        children.setdefault(relation.get("parent_code"), []).append(relation)

    def build_node(code, depth=0):
        definition = METRIC_MAP.get(code) or {}
        node = {
            "name": "{} {}".format(code, definition.get("name") or "").strip(),
            "metric_code": code,
            "value": 1,
        }
        if depth >= 3:
            return node
        nested = []
        for relation in children.get(code, []):
            child = build_node(relation.get("child_code"), depth + 1)
            child["relation_type"] = relation.get("relation_type")
            child["note"] = relation.get("note")
            nested.append(child)
        if nested:
            node["children"] = nested
        return node

    roots = [build_node("M_OV_001"), build_node("M_OV_004"), build_node("M_WF_001")]
    # 去掉空的重复根（M_OV_004 可能已在 M_OV_001 的子树里出现过）
    seen = set()
    unique_roots = []
    for node in roots:
        if node["metric_code"] in seen:
            continue
        seen.add(node["metric_code"])
        unique_roots.append(node)

    return {
        "tooltip": {"trigger": "item", "triggerOn": "mousemove"},
        "series": [
            {
                "type": "tree",
                "data": unique_roots,
                "top": "4%",
                "left": "6%",
                "bottom": "4%",
                "right": "16%",
                "symbolSize": 10,
                "orient": "LR",
                "label": {"position": "left", "verticalAlign": "middle", "align": "right", "fontSize": 12},
                "leaves": {"label": {"position": "right", "align": "left"}},
                "emphasis": {"focus": "descendant"},
                "expandAndCollapse": True,
                "animationDuration": 400,
            }
        ],
    }


# ---------------------------------------------------------------------------
# 十四、明细数据
# ---------------------------------------------------------------------------
ORDER_ORDERING_FIELDS = {
    "order_no": "order_no",
    "-order_no": "-order_no",
    "order_date": "order_date",
    "-order_date": "-order_date",
    "final_amount": "final_amount",
    "-final_amount": "-final_amount",
    "lead_days": "lead_days",
    "-lead_days": "-lead_days",
    "customer": "customer__customer_name",
    "-customer": "-customer__customer_name",
}


@login_required
def order_list(request):
    """明细数据：这一笔到底是谁下的单？能不能下钻到原始记录？

    列表页是「看得见的底稿」：任何看板上的聚合数字，都必须能被追到明细，
    否则业务方不会相信它。分页与排序参数走 URL，链接可分享、可收藏。
    """
    filters = filters_from(request)
    warnings = []
    ordering = (request.GET.get("ordering") or "-order_date").strip()
    if ordering not in ORDER_ORDERING_FIELDS:
        ordering = "-order_date"
    page_size = _safe_int(request.GET.get("page_size"), 50, low=10, high=200)

    queryset = safe(
        "订单明细", get_valid_orders, filters, warnings=warnings, default=None,
    )
    if queryset is None:
        # 取数失败时用一个空列表继续走分页流程：Paginator 接受列表，
        # 页面会渲染出「没有符合条件的记录」的空态而不是 500。
        # 这里刻意不 import Order 再兜底——那会把「数据库表不存在」这类
        # 底层错误再次抛出来，而用户只需要知道「这次没取到数据」。
        queryset = []

    try:
        if hasattr(queryset, "order_by"):
            queryset = queryset.order_by(ORDER_ORDERING_FIELDS[ordering])
        paginator = Paginator(queryset, page_size)
        page_number = request.GET.get("page") or 1
        try:
            page_obj = paginator.page(page_number)
        except PageNotAnInteger:
            page_obj = paginator.page(1)
        except EmptyPage:
            page_obj = paginator.page(paginator.num_pages or 1)
    except Exception as exc:  # pragma: no cover - 数据库不可用时也要能渲染
        logger.warning("订单分页失败：%s", exc)
        warnings.append("订单列表分页失败，已展示空列表（原因：{}）。".format(type(exc).__name__))
        page_obj = None

    rows = []
    if page_obj is not None:
        for order in page_obj.object_list:
            rows.append(
                {
                    "order_no": order.order_no,
                    "order_date": order.order_date,
                    "customer_code": getattr(order.customer, "customer_code", "") if order.customer_id else "",
                    "customer_name": getattr(order.customer, "customer_name", "") if order.customer_id else "散客",
                    "customer_type": order.customer_type,
                    "channel": order.channel,
                    "order_type": order.order_type,
                    "store_name": getattr(order.store, "store_name", "") if order.store_id else "",
                    "sales_rep_name": getattr(order.sales_rep, "rep_name", "") if order.sales_rep_id else "",
                    "dominant_category": order.dominant_category,
                    "final_amount": order.final_amount,
                    "discount": order.discount,
                    "item_count": order.item_count,
                    "order_status": order.order_status,
                    "lead_days": order.lead_days,
                    "actual_lead_days": order.actual_lead_days,
                    "is_on_time": order.is_on_time,
                    "on_time_text": {1: "准时", 0: "延期", -1: "未交付"}.get(order.is_on_time, "未交付"),
                    "delay_days": order.delay_days,
                    "delay_reason": order.delay_reason,
                    "phone_masked": _mask_phone(request.user, getattr(order.customer, "phone", "") if order.customer_id else ""),
                }
            )

    total_amount = 0.0
    try:
        if page_obj is not None:
            from django.db.models import Sum

            total_amount = float(page_obj.object_list.aggregate(total=Sum("final_amount"))["total"] or 0)
    except Exception as exc:  # pragma: no cover
        logger.warning("订单金额合计失败：%s", exc)

    kpis = [
        kpi("命中订单数", "{} 单".format(number(getattr(getattr(page_obj, "paginator", None), "count", 0))),
            "当前筛选条件下的有效订单", None, "primary", "M_OV_002"),
        kpi("当前页金额", money(total_amount), "本页 {} 单合计".format(len(rows)), None, "", "M_OV_001"),
        kpi("页码", "第 {} / {} 页".format(
            getattr(page_obj, "number", 1), getattr(getattr(page_obj, "paginator", None), "num_pages", 1) or 1),
            "每页 {} 条".format(page_size), None, "", ""),
        kpi("排序字段", ordering, "点击表头可切换升/降序", None, "", ""),
    ]

    on_time_text = {1: "准时", 0: "延期", -1: "未交付"}
    order_table = build_table(
        "tblOrderList",
        "订单明细（分页）",
        [
            {"label": "订单号", "key": "order_no", "sort_key": "order_no"},
            {"label": "下单日期", "key": "order_date", "sort_key": "order_date"},
            {"label": "客户编码", "key": "customer_code"},
            {"label": "客户名称", "key": "customer_name", "sort_key": "customer"},
            {"label": "客户类型", "key": "customer_type"},
            {"label": "渠道", "key": "channel"},
            {"label": "订单类型", "key": "order_type"},
            {"label": "门店", "key": "store_name"},
            {"label": "业务员", "key": "sales_rep_name"},
            {"label": "主品类", "key": "dominant_category"},
            {"label": "成交额", "key": "final_amount", "format": "money", "sort_key": "final_amount"},
            {"label": "让利率", "key": "discount", "format": "percent2"},
            {"label": "明细行", "key": "item_count", "format": "number"},
            {"label": "状态", "key": "order_status"},
            {"label": "计划周期(天)", "key": "lead_days", "format": "number", "sort_key": "lead_days"},
            {"label": "实际周期(天)", "key": "actual_lead_days", "format": "number"},
            {"label": "交付", "key": "on_time_text"},
            {"label": "延期天数", "key": "delay_days", "format": "number"},
            {"label": "联系电话", "key": "phone_masked"},
        ],
        rows,
        caption="按当前筛选条件分页的订单明细，含交付与联系方式（已脱敏）",
        description="交付列取值：准时 / 延期 / 未交付。「未交付」不参与准时率分母，不要把它读成延期。",
        sortable=True,
    )

    conclusions = [
        {
            "title": "明细页的用途",
            "body": "任何看板上的数字都应该能被追到这一层。所以建议：发现指标异常时，"
            "先用同样的筛选条件导出明细，用 Excel 独立算一遍，，对不上就是口径问题，"
            "而不是「系统不准」。",
            "level": "info",
        },
        {
            "title": "电话字段已脱敏",
            "body": "列表中的客户电话根据当前账号权限脱敏（无「查看客户联系方式」权限时只显示首尾）。"
            "所以建议：需要完整联系方式时走客户 360 页面，那里有明确的操作留痕。",
            "level": "warn",
        },
    ]

    ctx = {
        "warnings": warnings,
        "kpis": kpis,
        "page_obj": page_obj,
        "order_table": order_table,
        "ordering": ordering,
        "page_size": page_size,
        "conclusions": conclusions,
        "charts": [],
    }
    _attach_metric_defs(ctx, _matched_codes(["M_OV_001", "M_OV_002", "M_OV_003", "M_DS_001"]), warnings)
    return _page(request, "dashboard/order_list.html", "明细数据",
                 "这一笔到底是谁下的单？能不能追到原始记录？", ctx, filters, "detail")


def _safe_int(value, default: int, low: int, high: int) -> int:
    """把 URL 参数安全地转成区间内的整数（非法值静默回退默认值）。"""
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def help_page(request):
    """使用说明页：告诉使用者每个栏目是干什么的、先看什么。

    为什么需要这一页：系统做得再完整，第一次打开的人不知道从哪下手就等于没用。
    这一页刻意不接任何取数逻辑（不查库、不算指标），它只讲用法与口径约定，
    因此永远不会因为数据问题而失败，可以放心当作「上手入口」。
    """
    return _page(
        request,
        "dashboard/help.html",
        "使用说明：每个栏目怎么用",
        "第一次打开这个系统，按这份说明从上到下走一遍，十分钟就能看懂全部功能。",
        {},
        None,
        "help",
    )
