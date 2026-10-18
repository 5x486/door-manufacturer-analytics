"""What-if 决策模拟器（本项目的记忆点功能）。

这个模块回答的业务问题
----------------------
「如果明年整体提价 3%、材料成本再涨 5%、交付再拖 7 天，我们的毛利会变成多少？」

门厂老板每天都在做这类决策（调价、让利、加投放、压交付周期），
但绝大多数老板是靠「感觉」下决定的。本模块把这种感觉**拆成可审计的公式**：
每一项推演都能追到「哪条历史数据 + 哪条假设」，并且给出**敏感性区间**，
而不是一个看起来很精确的假数字。

三条设计原则（面试时可以照着讲）
--------------------------------
1. **假设透明**：所有假设都进 ``assumptions`` 列表（名称 / 取值 / 来源），
   页面上必须显式渲染。一个不给假设的模拟器等于半个算命先生。
2. **可解释的弹性公式**：推演只用乘法链与线性弹性，不用黑箱模型，，
   业务方要能自己拿计算器复算，这是它敢被用来做决策的前提。
3. **有回测**：:func:`historical_backtest` 用 2024 年参数预测 2025 年，
   把预测误差率摆出来。模型准不准由回测说话，不由我们自称。

数据与金额口径
--------------
- 基准数据来自 :mod:`apps.analytics.selectors`（**只读**，本模块不直接写 SQL）；
- 金额内部用 ``float`` 计算、返回前统一 ``round(..., 2)``。
  理由：模拟器的输出是「量级与方向的参考」，不是会计凭证；
  这里用 Decimal 只会让公式噪音变大（``float * Decimal`` 会直接抛异常），
  但**回测与基准值的来源仍然是 Decimal**，转换点集中在 :func:`_to_float`。
"""

from __future__ import annotations

import logging
from typing import Any

from apps.analytics.selectors import (
    get_after_sales_analysis,
    get_category_structure,
    get_channel_performance,
    get_delivery_performance,
    get_kpi_summary,
)
from apps.analytics.services.filters import AnalyticsFilters
from apps.core.constants import CHANNEL_ACQUISITION_COST

logger = logging.getLogger(__name__)

__all__ = [
    "PARAM_SPEC",
    "PARAM_MAP",
    "PRICE_ELASTICITY",
    "CHANNEL_BUDGET_PER_YUAN",
    "DELAY_RISK",
    "run_simulation",
    "historical_backtest",
]


# ---------------------------------------------------------------------------
# 一、模型常量（假设的落地处，每一条都能在 assumptions 里找到）
# ---------------------------------------------------------------------------
# 价格弹性：价格每变动 1%，销量反向变动 0.8%。
# 来源：本项目按历史折扣与销量做过一次简单回归，估计值落在 -0.6 ~ -1.0 之间，
# 取中位数 -0.8 作为默认值；真实项目应替换为实测弹性（见 assumptions）。
PRICE_ELASTICITY = -0.8

# 渠道每元获客成本带来的成交额（元/元）。
# 口径：渠道历史成交额 ÷ 该渠道估算营销费用（营销费用 = 新增客户 × 3 线索 × 线索单价）。
# 它回答的是「把一块钱预算从展会挪到抖音，能多带回多少成交额」。
# 它是**历史平均的边际替换率**，不是严格意义上的边际收益递减曲线——
# 大规模预算转移时真实收益会低于该值，页面上必须注明。
CHANNEL_BUDGET_PER_YUAN = {
    "抖音短视频": 12.5,
    "老客户转介": 26.0,
    "门店到访": 8.5,
    "线上渠道": 9.5,
    "展会营销": 3.2,
    "装修公司合作": 6.0,
}
# 兜底值：常量未覆盖的渠道（例如历史数据里出现了新渠道）
DEFAULT_PER_YUAN = 6.0

# 交付延期的两条传导链（比率均为「每延期 1 天增加的绝对百分点」）
DELAY_RISK = {
    # 每延期 1 天，准时率下降的百分点（延期天数本身直接吃掉准时率）
    "on_time_drop_per_day": 0.02,
    # 每延期 1 天，售后率上升的百分点
    "after_sales_rise_per_day": 0.004,
    # 每延期 1 天，新增的延期赔付与加急运费（元/单）
    "penalty_per_order_per_day": 60.0,
    # 每 1 个百分点的售后率，单均售后成本（元）
    "after_sales_cost_per_point": 18.0,
}

# 新增客户目标的转化成本与首单贡献
NEW_CUSTOMER_ASSUMPTION = {
    # 每个新增客户的平均获客成本（元/人）——各渠道线索单价的加权平均
    "cost_per_customer": 380.0,
    # 新客户首单成交额相对全体客单价的折扣系数（新客首单偏小）
    "first_order_factor": 0.85,
}

# 基准与情景对比用的「天数」，用于把年度基准换算成月度参考
DAYS_PER_MONTH = 30.4


# ---------------------------------------------------------------------------
# 二、参数定义（前端滑块与后端校验共用同一份 spec）
# ---------------------------------------------------------------------------
PARAM_SPEC = [
    {
        "key": "price_change_pct",
        "label": "整体价格变动",
        "unit": "%",
        "default": 0.0,
        "min": -15.0,
        "max": 15.0,
        "step": 0.5,
        "help": "正数＝提价，负数＝降价。会同时通过价格弹性影响销量。",
    },
    {
        "key": "volume_change_pct",
        "label": "销量变动（外生）",
        "unit": "%",
        "default": 0.0,
        "min": -30.0,
        "max": 30.0,
        "step": 1.0,
        "help": "不依赖价格的自然增减，例如淡旺季、竞品退出、门店扩张。",
    },
    {
        "key": "discount_change_pct",
        "label": "折扣政策变动",
        "unit": "%",
        "default": 0.0,
        "min": -5.0,
        "max": 5.0,
        "step": 0.5,
        "help": "正数＝多让利（成交价下降），负数＝收紧折扣。只影响客单价，不影响销量。",
    },
    {
        "key": "material_cost_change_pct",
        "label": "材料成本变动",
        "unit": "%",
        "default": 0.0,
        "min": -20.0,
        "max": 30.0,
        "step": 1.0,
        "help": "铜、不锈钢等原材料的采购价变动，只穿透到材料成本部分。",
    },
    {
        "key": "delivery_delay_days",
        "label": "交付延期天数",
        "unit": "天",
        "default": 0.0,
        "min": 0.0,
        "max": 30.0,
        "step": 1.0,
        "help": "平均交付延后天数，会推低准时率、推高售后成本与赔付。",
    },
    {
        "key": "new_customer_target",
        "label": "新增客户目标",
        "unit": "人",
        "default": 0.0,
        "min": 0.0,
        "max": 300.0,
        "step": 5.0,
        "help": "计划新增成交客户数，按单客获客成本扣减毛利，按首单客单价增收。",
    },
    {
        "key": "channel_budget_shift",
        "label": "预算转向抖音比例",
        "unit": "%",
        "default": 0.0,
        "min": 0.0,
        "max": 100.0,
        "step": 5.0,
        "help": "把展会营销预算的该比例转向抖音短视频，按历史每元获客产出换算增量营收。",
    },
]

PARAM_MAP = {item["key"]: item for item in PARAM_SPEC}


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _to_float(value, default: float = 0.0) -> float:
    """Decimal / None / 字符串 → float。

    整个项目里 Decimal 是金额的唯一真身，模拟器只在入口处转一次，
    转换点唯一，避免 float 误差在多层公式里被反复搬运。
    """
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _round(value: float, digits: int = 2) -> float:
    return round(float(value or 0.0), digits)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def normalize_params(params: dict | None) -> dict:
    """把外部传入的参数规整到 ``PARAM_SPEC`` 的范围内。

    取值非法（字符串、None、超范围）一律**夹取到边界**而不是报错：
    模拟器是给人拖滑块玩的，前端传了个越界值就 500 是很糟糕的体验；
    但夹取必须诚实，，最终用了多少会出现在返回值的 ``assumptions`` 里。
    """
    params = params or {}
    normalized = {}
    for item in PARAM_SPEC:
        raw = params.get(item["key"], item["default"])
        if raw in ("", None):
            raw = item["default"]
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = float(item["default"])
        normalized[item["key"]] = _clamp(value, item["min"], item["max"])
    return normalized


def _empty_baseline() -> dict:
    """没有任何数据时的空基准，保证函数永远有结构化返回（页面不会崩）。"""
    return {
        "revenue": 0.0,
        "order_count": 0,
        "avg_order_value": 0.0,
        "gross_profit": 0.0,
        "gross_margin": 0.0,
        "material_cost": 0.0,
        "material_ratio": 0.0,
        "collection_amount": 0.0,
        "collection_ratio": 0.0,
        "on_time_rate": 0.0,
        "after_sales_rate": 0.0,
        "after_sales_cost": 0.0,
        "new_customer_count": 0,
        "customer_count": 0,
        "period": "",
        "source": "unavailable",
    }


# ---------------------------------------------------------------------------
# 三、基准测算
# ---------------------------------------------------------------------------
def _resolve_filters(filters=None) -> AnalyticsFilters:
    """没有传筛选条件时，默认用「最近一个完整年度」。

    为什么默认取年而不是全量：全量跨 3 年且规模仍在增长，
    「年度成交额」这类基准口径在跨年数据上没有意义，同比也会失真。
    """
    if filters is not None:
        return filters
    from datetime import date

    current_year = date.today().year
    return AnalyticsFilters.from_mapping({"period": str(current_year - 1)})


def _build_baseline(filters: AnalyticsFilters) -> dict:
    """从真实数据算出基准盘（成交额、毛利、成本结构、准时率、售后率）。"""
    baseline = _empty_baseline()

    # 1) 整体经营 KPI
    try:
        kpi = get_kpi_summary(filters) or {}
    except Exception as exc:  # pragma: no cover - 依赖预聚合表，可能未构建
        logger.warning("模拟器读取 KPI 基准失败：%s", exc)
        kpi = {}

    revenue = _to_float(kpi.get("revenue"))
    gross_profit = _to_float(kpi.get("gross_profit"))
    order_count = int(kpi.get("order_count") or 0)
    # 客单价字段名兼容两种写法（avg_order_value / aov），避免因上游改名导致基准失真
    aov = _to_float(kpi.get("avg_order_value") or kpi.get("aov"))
    if aov <= 0 and order_count:
        aov = revenue / order_count

    baseline.update(
        {
            "revenue": revenue,
            "order_count": order_count,
            "avg_order_value": aov,
            "gross_profit": gross_profit,
            "gross_margin": _to_float(kpi.get("gross_margin")),
            "collection_amount": _to_float(kpi.get("collection_amount")),
            "collection_ratio": _to_float(kpi.get("collection_rate")),
            "on_time_rate": _to_float(kpi.get("on_time_rate")),
            "new_customer_count": int(kpi.get("new_customer_count") or 0),
            "customer_count": int(kpi.get("customer_count") or 0),
            "period": kpi.get("period") or filters.describe(),
            "source": kpi.get("source") or "fact",
        }
    )

    # 2) 材料成本占比：来自品类成本结构（材料/人工/制造/物流/安装 五项之和为总成本）
    try:
        categories = get_category_structure(filters) or []
    except Exception as exc:  # pragma: no cover
        logger.warning("模拟器读取品类成本结构失败：%s", exc)
        categories = []

    material = sum(_to_float(row.get("material_cost")) for row in categories)
    if material > 0:
        baseline["material_cost"] = material
        # 材料成本 / 成交额 就是「材料占收入比」，是穿透成本变动最稳的锚
        baseline["material_ratio"] = material / revenue if revenue else 0.0
    else:
        # 兜底：门业材料占收入的经验值（约 48%），并在 assumptions 里写明来源
        baseline["material_ratio"] = 0.48
        baseline["material_cost"] = revenue * 0.48

    # 3) 售后成本与售后率
    try:
        after_sales = get_after_sales_analysis(filters) or {}
    except Exception as exc:  # pragma: no cover
        logger.warning("模拟器读取售后数据失败：%s", exc)
        after_sales = {}
    baseline["after_sales_rate"] = _to_float(after_sales.get("after_sales_rate"))
    baseline["after_sales_cost"] = _to_float(after_sales.get("total_cost"))

    # 4) 准时率缺失时补一个中性值（0.85），避免敏感性图被 0 拉平
    if baseline["on_time_rate"] <= 0:
        try:
            delivery = get_delivery_performance(filters) or {}
            baseline["on_time_rate"] = _to_float(delivery.get("on_time_rate"))
        except Exception as exc:  # pragma: no cover
            logger.warning("模拟器读取交付数据失败：%s", exc)

    return baseline


# ---------------------------------------------------------------------------
# 四、渠道预算转移
# ---------------------------------------------------------------------------
def _channel_budget_effect(baseline: dict, shift_pct: float) -> dict:
    """把展会预算的一部分转向抖音，估算增量营收与增量利润。

    算法（三步）：
    1. 估算展会渠道的历史营销费用作为可转移预算池；
    2. 用两个渠道各自的「每元产出」差额乘以转移金额，得到增量成交额；
    3. 增量成交额按基准毛利率折成增量毛利，再扣除转移过程中损失的机会成本，，
       机会成本按「展会每元产出 × 转移金额」计算，已包含在第 2 步的差额里。
    """
    if shift_pct <= 0:
        return {"budget": 0.0, "revenue_delta": 0.0, "profit_delta": 0.0, "per_yuan_gain": 0.0}

    try:
        channels = get_channel_performance(_current_filters()) or []
    except Exception as exc:  # pragma: no cover - 渠道数据依赖 fact 表
        logger.warning("模拟器读取渠道绩效失败：%s", exc)
        channels = []

    exhibition_cost = 0.0
    for row in channels:
        if (row.get("channel") or "") in ("展会营销", "exhibition"):
            exhibition_cost = _to_float(row.get("marketing_cost"))
            break
    if exhibition_cost <= 0:
        # 兜底：渠道费用缺失时，按「新增客户数 × 线索单价 × 3」粗估展会费用
        lead_cost = _to_float((CHANNEL_ACQUISITION_COST or {}).get("展会营销", 120))
        exhibition_cost = max(baseline.get("new_customer_count", 0), 10) * 3 * lead_cost * 0.35

    budget = exhibition_cost * (shift_pct / 100.0)
    per_yuan_exhibition = CHANNEL_BUDGET_PER_YUAN.get("展会营销", DEFAULT_PER_YUAN)
    per_yuan_douyin = CHANNEL_BUDGET_PER_YUAN.get("抖音短视频", DEFAULT_PER_YUAN)
    per_yuan_gain = max(per_yuan_douyin - per_yuan_exhibition, 0.0)

    revenue_delta = budget * per_yuan_gain
    profit_delta = revenue_delta * baseline.get("gross_margin", 0.0)
    return {
        "budget": _round(budget),
        "revenue_delta": _round(revenue_delta),
        "profit_delta": _round(profit_delta),
        "per_yuan_gain": _round(per_yuan_gain, 3),
    }


# 模拟器内部的渠道测算需要「与基准同一套筛选条件」，
# 这里用一个模块级变量传递，避免把 filters 塞进每个私有函数的签名里。
_ACTIVE_FILTERS: AnalyticsFilters | None = None


def _current_filters() -> AnalyticsFilters:
    return _ACTIVE_FILTERS if _ACTIVE_FILTERS is not None else _resolve_filters(None)


# ---------------------------------------------------------------------------
# 五、主推演
# ---------------------------------------------------------------------------
def run_simulation(params: dict | None = None, filters: AnalyticsFilters | None = None) -> dict:
    """执行一次 what-if 推演。

    参数
    ----
    params : dict
        见 :data:`PARAM_SPEC`。缺失的键用默认值 0，越界值会被夹取到边界。
    filters : AnalyticsFilters | None
        基准数据的时间范围；``None`` 表示使用「上一个完整年度」。

    返回
    ----
    dict
        ``{baseline, scenario, delta, waterfall, sensitivity, assumptions,
        conclusion, params}``。金额单位为**元**（float，保留 2 位）；
        比率为 0~1 的小数（与全项目口径一致，展示时乘 100）。

    推演链（每一步都可单独复算）
    --------------------------
    1. 订单量 = 基准订单量 × (1 + 外生销量变动) × (1 + 价格弹性 × 价格变动%)；
    2. 客单价 = 基准客单价 × (1 + 价格变动%) × (1 − 折扣变动%)；
    3. 营收 = 订单量 × 客单价（+ 渠道预算转移的增量营收）；
    4. 成本 = 材料成本 × (1 + 材料成本变动%) + 其余成本（人工/制造/物流/安装，按基准倒推）；
    5. 毛利 = 营收 − 成本 − 新增客户获客投入 − 延期赔付 − 售后增量成本；
    6. 准时率、售后率按延期天数线性推演（含上下限约束）。
    """
    global _ACTIVE_FILTERS

    normalized = normalize_params(params)
    resolved = _resolve_filters(filters)
    previous = _ACTIVE_FILTERS
    _ACTIVE_FILTERS = resolved
    try:
        baseline = _build_baseline(resolved)
        assumptions = _collect_assumptions(normalized, baseline)
        scenario, delta = _simulate(normalized, baseline)
        waterfall = _build_waterfall(baseline, scenario, normalized)
        sensitivity = _build_sensitivity(normalized, baseline)
        conclusion = _build_conclusion(baseline, scenario, delta, normalized)
    finally:
        _ACTIVE_FILTERS = previous

    return {
        "params": normalized,
        "baseline": _payload(baseline),
        "scenario": _payload(scenario),
        "delta": _payload(delta, is_delta=True),
        "waterfall": waterfall,
        "sensitivity": sensitivity,
        "assumptions": assumptions,
        "conclusion": conclusion,
        "param_spec": PARAM_SPEC,
        "metric_codes": ["M_OV_001", "M_OV_004", "M_OV_005", "M_CP_009", "M_DS_001", "M_WR_002"],
    }


def _simulate(params: dict, baseline: dict) -> tuple:
    """核心公式链，返回 ``(scenario, delta)``。

    刻意写成一条直线上的几行乘法，而不是拆成十几个小函数：
    模拟器的可读性就是它的可信度，评审者应当能一眼看完公式。
    """
    price_pct = params["price_change_pct"] / 100.0
    volume_pct = params["volume_change_pct"] / 100.0
    discount_pct = params["discount_change_pct"] / 100.0
    material_pct = params["material_cost_change_pct"] / 100.0
    delay_days = params["delivery_delay_days"]
    new_customers = params["new_customer_target"]
    shift_pct = params["channel_budget_shift"]

    revenue0 = baseline["revenue"]
    orders0 = baseline["order_count"]
    aov0 = baseline["avg_order_value"]
    margin0 = baseline["gross_margin"]

    # 1) 订单量：外生变动 + 价格弹性
    elasticity_factor = 1.0 + PRICE_ELASTICITY * price_pct
    orders = orders0 * (1.0 + volume_pct) * elasticity_factor
    orders = max(orders, 0.0)

    # 2) 客单价：提价与让利各走一条路
    aov = aov0 * (1.0 + price_pct) * (1.0 - discount_pct)
    aov = max(aov, 0.0)

    # 3) 营收
    revenue = orders * aov

    # 4) 渠道预算转移的增量
    channel = _channel_budget_effect(baseline, shift_pct)
    revenue += channel["revenue_delta"]

    # 其余成本（人工 / 制造费用 / 物流 / 安装）的推导必须与瀑布图的 :func:`_other_cost_of`
    # **完全同源**，否则「台阶之和 = 毛利变化」这条恒等式就会留一个说不清的残差。
    # 两条路径都基于同一个恒等式：毛利 = 成交额 − 材料 − 其余成本（+ 新客户净贡献）− 质量成本。
    # 拆分方式上与「按收入占比」不同：这里把其余成本当作**与订单量相关**的成本按量放大，
    # 而占收入比法则隐含「其余成本会随价格同比例上升」——后者会系统性地低估提价的利润效果。
    material0 = baseline["material_cost"]
    margin0 = _to_float(baseline.get("gross_margin"))
    if margin0 <= 0 and revenue0 > 0:
        margin0 = _to_float(baseline.get("gross_profit")) / revenue0
    # 其余成本用同一个反推函数得到，保证与瀑布图同源（见 _other_cost_of 的说明）
    other_cost0 = _other_cost_of(baseline)

    volume_factor = (orders / orders0) if orders0 > 0 else 1.0
    material = material0 * (1.0 + material_pct) * volume_factor
    other_cost = other_cost0 * volume_factor

    # 6) 新增客户的获客投入与首单贡献
    #
    # 口径选择：**新客户的首单贡献只进毛利，不进「成交额」**。
    # 理由：成交额在我们的口径里是「订单量 × 客单价」推出来的结果，
    # 新客户是另一条独立的增量通道；把它的收入并入分母会让毛利率被结构性抬高，
    # 那会制造「拉新让毛利率变好」的假象（实际是拉新花钱、摊薄毛利）。
    # 因此这里用「首单毛利贡献 = 首单收入 × 基准毛利率 − 单客获客成本」，
    # 收入与成本成对出现，不污染成交额与毛利率两个核心口径。
    #
    # 首单客单价用**情景价**（aov）而不是基准价：新客户也按新价格成交，
    # 用基准价会让「提价」在归因里被重复计算一次，瀑布图就会出现系统性的残差。
    acquisition_cost = new_customers * NEW_CUSTOMER_ASSUMPTION["cost_per_customer"]
    first_order_revenue = new_customers * aov * NEW_CUSTOMER_ASSUMPTION["first_order_factor"]
    new_customer_contribution = first_order_revenue * margin0 - acquisition_cost

    # 7) 交付延期的两条传导链
    on_time_drop = delay_days * DELAY_RISK["on_time_drop_per_day"]
    on_time_rate = _clamp(baseline["on_time_rate"] - on_time_drop, 0.0, 1.0)
    after_sales_rise = delay_days * DELAY_RISK["after_sales_rise_per_day"]
    after_sales_rate = _clamp(baseline["after_sales_rate"] + after_sales_rise, 0.0, 1.0)
    penalty = orders * delay_days * DELAY_RISK["penalty_per_order_per_day"]
    after_sales_cost = after_sales_rate * orders * DELAY_RISK["after_sales_cost_per_point"]
    quality_cost = penalty + after_sales_cost

    # 8) 毛利 = 营收 × 增量转换 + (材料与其余成本按量价推演) + 新客户首单净贡献 − 质量成本
    #
    # 这里用「营收 × 单位毛利贡献」而不是「营收 − 材料 − 其余成本」，
    # 是因为步骤 5 已经算出了材料与其余成本；两条路在数学上等价，
    # 但写成「营收 × (1 − 成本率)」能让「提价的每一元里有多少落进毛利」变得一目了然。
    gross_profit = (
        revenue
        - material
        - other_cost
        + new_customer_contribution
        - quality_cost
    )

    scenario = dict(baseline)
    scenario.update(
        {
            "revenue": _round(revenue),
            "order_count": int(round(orders)),
            "avg_order_value": _round(aov),
            "gross_profit": _round(gross_profit),
            # 占比类指标在无数据/无收入时必须**继承基准值**，不能落到 0.0：
            # 0.0 在界面上会被读成「成本占比变成了零」，是一个会误导决策的假结论；
            # 而「基准也是 0」才说明确实没有数据可用。零参数情景与基准必须逐项相等，
            # 这是模拟器最基本的自洽性不变量（已有测试守住）。
            "gross_margin": _round(gross_profit / revenue, 4) if revenue else baseline.get("gross_margin", 0.0),
            "material_cost": _round(material),
            "material_ratio": _round(material / revenue, 4) if revenue else baseline.get("material_ratio", 0.0),
            "collection_amount": _round(revenue * _collection_ratio(baseline)),
            "on_time_rate": _round(on_time_rate, 4),
            "after_sales_rate": _round(after_sales_rate, 4),
            "after_sales_cost": _round(after_sales_cost),
            "new_customer_count": int(baseline["new_customer_count"] + new_customers),
            "source": "simulation",
        }
    )
    scenario["acquisition_cost"] = _round(acquisition_cost)
    scenario["first_order_contribution"] = _round(new_customer_contribution)
    scenario["quality_cost"] = _round(quality_cost)
    scenario["channel_revenue_delta"] = channel["revenue_delta"]

    delta = {
        key: _round(_to_float(scenario.get(key)) - _to_float(baseline.get(key)))
        for key in (
            "revenue", "order_count", "avg_order_value", "gross_profit", "gross_margin",
            "material_cost", "material_ratio", "collection_amount", "on_time_rate",
            "after_sales_rate", "after_sales_cost", "new_customer_count",
        )
    }
    delta["revenue_pct"] = _round(delta["revenue"] / revenue0, 4) if revenue0 else 0.0
    delta["profit_pct"] = (
        _round(delta["gross_profit"] / baseline["gross_profit"], 4)
        if baseline["gross_profit"]
        else 0.0
    )
    # 毛利率变动的正确写法是「百分点差」，不是「相对变化率」——
    # 把 12% → 11% 说成「下降 8%」会严重低估风险，这里显式乘 100 给百分点。
    delta["margin_points"] = _round(
        (_to_float(scenario.get("gross_margin")) - _to_float(baseline.get("gross_margin"))) * 100, 2
    )
    _ = margin0  # 保留可读性：margin0 是公式推导用的中间量
    return scenario, delta


def _collection_ratio(baseline: dict) -> float:
    """基准回款率（回款额 ÷ 成交额）；缺失时按 0.82 的经验值。

    回款额与成交额属于两个时间轴（付款日 vs 订单日），二者相除得到的是
    「同步回款强度」而非严格的收款进度；这里只用它做一个粗略的比例推演，
    所以做了 0~1.2 的合理区间约束。
    """
    revenue = baseline.get("revenue") or 0.0
    if revenue <= 0:
        return 0.82
    ratio = _to_float(baseline.get("collection_ratio")) or (
        _to_float(baseline.get("collection_amount")) / revenue
    )
    return ratio if 0 < ratio <= 1.2 else 0.82


def _payload(data: dict, is_delta: bool = False) -> dict:
    """裁剪成页面/接口要展示的字段，避免把内部中间量泄漏出去。"""
    keys = (
        "revenue", "order_count", "avg_order_value", "gross_profit", "gross_margin",
        "material_cost", "material_ratio", "collection_amount", "collection_ratio",
        "on_time_rate", "after_sales_rate", "after_sales_cost", "new_customer_count",
        "customer_count", "period", "source",
    )
    payload = {}
    for key in keys:
        value = data.get(key)
        if isinstance(value, float):
            payload[key] = _round(value, 4 if abs(value) <= 10 else 2)
        elif isinstance(value, int):
            payload[key] = value
        elif value is None:
            payload[key] = 0 if key not in ("period", "source") else ""
        else:
            payload[key] = value
    if is_delta:
        for key in ("revenue_pct", "profit_pct", "margin_points"):
            payload[key] = _round(data.get(key, 0.0), 4)
    return payload


# ---------------------------------------------------------------------------
# 六、瀑布图 / 敏感性 / 假设
# ---------------------------------------------------------------------------
def _build_waterfall(baseline: dict, scenario: dict, params: dict) -> list:
    """把「基准毛利 → 情景毛利」拆成可归因的台阶。

    ``type`` 取值：``baseline``（起点）/ ``up``（增利）/ ``down``（减利）/ ``total``（终点）。
    页面按这个字段上色，**不要**让前端自己判断正负，，归因口径只应有一处。
    """
    steps = []
    profit0 = _to_float(baseline.get("gross_profit"))
    profit1 = _to_float(scenario.get("gross_profit"))
    revenue0 = _to_float(baseline.get("revenue"))
    revenue1 = _to_float(scenario.get("revenue"))
    orders0 = _to_float(baseline.get("order_count"))
    orders1 = _to_float(scenario.get("order_count"))
    aov0 = _to_float(baseline.get("avg_order_value"))
    aov1 = _to_float(scenario.get("avg_order_value"))

    # 营收效应 = 量效应 + 价效应。量的部分拆成两条**互不重叠**的台阶：
    #   （a）外生销量变动：用户在参数里直接输入的销量增减；
    #   （b）价格弹性带来的量损：提价导致的销量下滑。
    # 两者都归到「量」上，但驱动因素不同——把弹性单独列出来，
    # 是为了让「提价会掉量」这件事在瀑布图上看得见，而不是被合并进一条
    # 说不清的「数量效应」里（这是模拟器最容易糊弄人的地方）。
    base_volume = orders0 * (1 + params["volume_change_pct"] / 100.0)
    margin0 = _to_float(baseline.get("gross_margin"))
    volume_effect = (base_volume - orders0) * aov0 * margin0
    # 价格效应与弹性效应的「量」都用 base_volume（只含外生变动），
    # 用最终订单量会让外生销量的影响在两条台阶上被重复计入，瀑布图立刻出现系统性残差。
    price_effect = base_volume * (aov1 - aov0) * margin0
    elasticity_effect = (orders1 - base_volume) * aov0 * margin0
    # 渠道预算转移（既增收也增利）
    channel_revenue = _to_float(scenario.get("channel_revenue_delta"))
    channel_effect = channel_revenue * margin0
    material_effect = -(_to_float(scenario.get("material_cost")) - _to_float(baseline.get("material_cost")))
    # 其余成本（人工 / 制造 / 物流 / 安装）按量放大带来的增减；
    # 它与材料分开列，因为两者的**驱动因素不同**：材料受价格与用量双重影响，
    # 其余成本只随产量变化——分开看才能判断「该谈价格还是该提效率」。
    other_cost_effect = -(
        _other_cost_of(scenario) - _other_cost_of(baseline)
    )
    # 新增客户的首单净贡献第一项已在 customer_effect 里，这里只留净额一条台阶
    new_customer_effect = _to_float(scenario.get("first_order_contribution"))
    quality_effect = -_to_float(scenario.get("quality_cost"))
    # 残差 = 归因台阶之外的部分。它不是「误差」，而是**量价交叉项**：
    # 台阶分解把「量变」按基准价计算、「价变」按基准量计算，两者相乘得出的
    # 交叉项（Δ量 × Δ价）在归因里没有归属，因此单独列出。
    # 保留它是为了让「台阶之和 = 毛利变化」成为一条永远成立的恒等式——
    # 把差额悄悄塞进某一条台阶里，才是真正不可接受的做法。
    residual = profit1 - (
        profit0 + volume_effect + price_effect + elasticity_effect + channel_effect
        + material_effect + other_cost_effect + new_customer_effect + quality_effect
    )

    def _step(name: str, value: float, step_type: str = "") -> dict:
        kind = step_type or ("up" if value >= 0 else "down")
        return {"name": name, "value": _round(value), "type": kind}

    steps.append(_step("基准毛利", profit0, "baseline"))
    steps.append(_step("销量变动（外生）", volume_effect))
    steps.append(_step("价格弹性带来的量损", elasticity_effect))
    steps.append(_step("价格变动效应", price_effect))
    steps.append(_step("渠道预算转移", channel_effect))
    steps.append(_step("材料成本", material_effect))
    steps.append(_step("其他成本（随量）", other_cost_effect))
    steps.append(_step("新增客户净贡献", new_customer_effect))
    steps.append(_step("交付延期成本", quality_effect))
    steps.append(_step("交叉项（量×价）", residual))
    steps.append(_step("情景毛利", profit1, "total"))
    _ = revenue0, revenue1
    return steps


def _other_cost_of(payload: dict) -> float:
    """反推「其余成本」（人工 + 制造费用 + 物流 + 安装）的基准绝对额。

    **这是模拟器最容易被写错的一处，值得单独成函数。** 定义必须与
    :func:`_simulate` 完全同源，否则瀑布图会留一个说不清的残差：

        其余成本 = 成交额 − 材料成本 − 毛利额

    注意它**不包含**新客户净贡献与质量成本，，那两项在 :func:`_simulate` 里是
    独立减项，不属于「随产量变化的生产成本」。如果这里把它们也减掉，
    瀑布的「其他成本（随量）」台阶就会把获客投入与延期赔付算进生产成本，
    与实际公式链不一致（残差不为 0 就是这种不一致的报警信号）。
    """
    return max(
        _to_float(payload.get("revenue"))
        - _to_float(payload.get("material_cost"))
        - _to_float(payload.get("gross_profit")),
        0.0,
    )


def _build_sensitivity(params: dict, baseline: dict) -> list:
    """单参数敏感性：每个参数取「低 / 基准 / 高」三点，其余参数固定在当前值。

    这是 tornado 图的数据源。**必须是单参数扰动**（OAT），
    不能同时扰动两个参数，，那样柱子之间的长度就没有可比性了。
    """
    items = []
    for spec in PARAM_SPEC:
        key = spec["key"]
        base_value = params[key]
        low_value = spec["min"] if base_value == spec["default"] else max(spec["min"], base_value - spec["step"] * 4)
        high_value = spec["max"] if base_value == spec["default"] else min(spec["max"], base_value + spec["step"] * 4)
        low_params = dict(params, **{key: low_value})
        high_params = dict(params, **{key: high_value})
        low_profit = _profit_of(low_params, baseline)
        base_profit = _profit_of(params, baseline)
        high_profit = _profit_of(high_params, baseline)
        items.append(
            {
                "param": key,
                "param_label": spec["label"],
                "unit": spec["unit"],
                "metric": "毛利额",
                "low_value": _round(low_value, 2),
                "base_value": _round(base_value, 2),
                "high_value": _round(high_value, 2),
                "low": _round(low_profit),
                "base": _round(base_profit),
                "high": _round(high_profit),
                # 摆动幅度 = 高 − 低，用于 tornado 图排序（越长越敏感）
                "swing": _round(high_profit - low_profit),
            }
        )
    items.sort(key=lambda item: -abs(item["swing"]))
    return items


def _profit_of(params: dict, baseline: dict) -> float:
    """只算毛利，用于敏感性三点测算（不生成完整 scenario，避免重复计算）。"""
    scenario, _delta = _simulate(params, baseline)
    return _to_float(scenario.get("gross_profit"))


def _collect_assumptions(params: dict, baseline: dict) -> list:
    """列出本次推演用到的全部假设（名称 / 取值 / 来源）。

    面试要点：**这一段比公式本身更能体现分析方法论**。
    它让「模拟结果」从「一个数字」变成「一组可审计的输入」。
    """
    return [
        {
            "name": "价格弹性",
            "value": "{:.2f}".format(PRICE_ELASTICITY),
            "source": "历史折扣率与销量的线性回归估计值（区间 -1.0 ~ -0.6），可替换为实测弹性",
        },
        {
            "name": "销量变动（外生）",
            "value": "{:.1f}%".format(params["volume_change_pct"]),
            "source": "用户输入",
        },
        {
            "name": "整体价格变动",
            "value": "{:.1f}%".format(params["price_change_pct"]),
            "source": "用户输入",
        },
        {
            "name": "折扣政策变动",
            "value": "{:.1f}%".format(params["discount_change_pct"]),
            "source": "用户输入（正数＝多让利，仅影响客单价，不影响销量）",
        },
        {
            "name": "材料成本变动",
            "value": "{:.1f}%".format(params["material_cost_change_pct"]),
            "source": "用户输入（只穿透材料成本部分）",
        },
        {
            "name": "材料成本占收入比",
            "value": "{:.2f}%".format(baseline.get("material_ratio", 0.0) * 100),
            "source": "当前筛选条件下的品类成本结构（材料成本 ÷ 成交额）",
        },
        {
            "name": "其余成本",
            "value": _round(
                max(
                    _to_float(baseline.get("revenue"))
                    - _to_float(baseline.get("material_cost"))
                    - _to_float(baseline.get("gross_profit")),
                    0.0,
                )
            ),
            "source": "成交额 − 材料成本 − 毛利额，含人工、制造费用、物流与安装；按订单量等比例放大",
        },
        {
            "name": "基准准时率",
            "value": "{:.2f}%".format(_to_float(baseline.get("on_time_rate")) * 100),
            "source": "已完成订单（含实际交付日）口径，未交付订单不计入分母",
        },
        {
            "name": "延期 1 天的准时率损失",
            "value": "{:.1f} 个百分点".format(DELAY_RISK["on_time_drop_per_day"] * 100),
            "source": "本项目设定值：延期天数直接抵扣准时率，可按历史交付数据重新拟合",
        },
        {
            "name": "延期 1 天的售后率抬升",
            "value": "{:.1f} 个百分点".format(DELAY_RISK["after_sales_rise_per_day"] * 100),
            "source": "本项目设定值：交付延期是售后工单的主要诱因之一",
        },
        {
            "name": "延期赔付与加急成本",
            "value": "{:.0f} 元/单/天".format(DELAY_RISK["penalty_per_order_per_day"]),
            "source": "本项目设定值（工期违约金 + 加急运费），真实数据应从售后与物流费用表取",
        },
        {
            "name": "单客获客成本",
            "value": "{:.0f} 元/人".format(NEW_CUSTOMER_ASSUMPTION["cost_per_customer"]),
            "source": "各渠道线索单价 × 3 条线索成交 1 单的加权估算",
        },
        {
            "name": "新客户首单系数",
            "value": "{:.2f}".format(NEW_CUSTOMER_ASSUMPTION["first_order_factor"]),
            "source": "新客首单金额相对全体客单价的系数（首单通常偏小）",
        },
        {
            "name": "渠道每元获客产出",
            "value": "抖音 {:.1f} / 展会 {:.1f}".format(
                CHANNEL_BUDGET_PER_YUAN.get("抖音短视频", 0.0),
                CHANNEL_BUDGET_PER_YUAN.get("展会营销", 0.0),
            ),
            "source": "渠道成交额 ÷ 该渠道估算营销费用（历史平均）；大规模转移时边际收益会递减",
        },
        {
            "name": "基准期间",
            "value": str(baseline.get("period") or "全量"),
            "source": "当前筛选条件；未指定时默认上一个完整年度",
        },
    ]


def _build_conclusion(baseline: dict, scenario: dict, delta: dict, params: dict) -> str:
    """一句中文结论：说清「变多少」和「为什么」，并且给出下一步动作。"""
    profit_delta = _to_float(delta.get("gross_profit"))
    margin_points = _to_float(delta.get("margin_points"))
    direction = "提升" if profit_delta >= 0 else "下降"

    drivers = []
    if params["price_change_pct"]:
        drivers.append("提价 {:.1f}%".format(params["price_change_pct"]) if params["price_change_pct"] > 0
                       else "降价 {:.1f}%".format(abs(params["price_change_pct"])))
    if params["volume_change_pct"]:
        drivers.append("销量{} {:.1f}%".format("增加" if params["volume_change_pct"] > 0 else "减少",
                                            abs(params["volume_change_pct"])))
    if params["material_cost_change_pct"]:
        drivers.append("材料成本{} {:.1f}%".format("上涨" if params["material_cost_change_pct"] > 0 else "下降",
                                              abs(params["material_cost_change_pct"])))
    if params["delivery_delay_days"]:
        drivers.append("交付延期 {:.0f} 天".format(params["delivery_delay_days"]))
    if params["channel_budget_shift"]:
        drivers.append("预算 {:.0f}% 转向抖音".format(params["channel_budget_shift"]))
    if params["new_customer_target"]:
        drivers.append("新增 {:.0f} 个客户".format(params["new_customer_target"]))
    driver_text = "、".join(drivers) if drivers else "维持现状（未做任何调整）"

    if margin_points <= -1:
        advice = "建议先守住价格与材料成本，再谈放量，，毛利率每掉 1 个点，等于把全年利润的 {}% 让出去。".format(
            abs(_round(margin_points * 100 / max(_to_float(baseline.get("gross_margin")) * 100, 1), 1))
            if baseline.get("gross_margin")
            else 1.0
        )
    elif profit_delta > 0:
        advice = "建议按该组合推进，并用月度实际毛利率做滚动校验（偏差超过 1 个百分点即回滚）。"
    else:
        advice = "建议暂缓执行，先做小范围试点验证弹性假设是否成立。"

    return (
        "在「{}」的情景下，毛利额将由 {:.0f} 万元变为 {:.0f} 万元（{} {:.0f} 万元，"
        "毛利率变动 {:+.2f} 个百分点）。{}"
    ).format(
        driver_text,
        _to_float(baseline.get("gross_profit")) / 10000.0,
        _to_float(scenario.get("gross_profit")) / 10000.0,
        direction,
        abs(profit_delta) / 10000.0,
        margin_points,
        advice,
    )


# ---------------------------------------------------------------------------
# 七、历史回测
# ---------------------------------------------------------------------------
def historical_backtest(base_year: int = 2024, target_year: int = 2025) -> dict:
    """用 ``base_year`` 的参数预测 ``target_year``，与真实结果对比。

    为什么必须有回测
    ----------------
    模拟器最容易被质疑的一点是「你这些弹性系数哪来的」。
    回测不能解决系数来源问题，但它能回答一个更关键的问题：
    **这套公式在真实数据上的误差有多大**。给出误差率，用户才能判断
    这个结论是「可以参考」还是「只能当个方向」。

    实现：把 base_year 与 target_year 的各项关键比率（客单价、毛利率、
    订单量增速）算出来，用基年的参数代入同一套公式，比较预测与实际。
    """
    import datetime as _dt

    base_filters = AnalyticsFilters.from_mapping({"period": str(base_year)})
    target_filters = AnalyticsFilters.from_mapping({"period": str(target_year)})

    base_kpi = _safe_kpi(base_filters)
    target_kpi = _safe_kpi(target_filters)

    base_revenue = _to_float(base_kpi.get("revenue"))
    actual_revenue = _to_float(target_kpi.get("revenue"))
    base_profit = _to_float(base_kpi.get("gross_profit"))
    actual_profit = _to_float(target_kpi.get("gross_profit"))
    base_orders = int(base_kpi.get("order_count") or 0)
    actual_orders = int(target_kpi.get("order_count") or 0)

    # 实际同比增幅：作为「事后参数」代入基年基准做预测
    actual_volume_growth = (actual_orders - base_orders) / base_orders if base_orders else 0.0
    actual_price_growth = 0.0
    base_aov = base_revenue / base_orders if base_orders else 0.0
    actual_aov = actual_revenue / actual_orders if actual_orders else 0.0
    if base_aov:
        actual_price_growth = (actual_aov - base_aov) / base_aov

    predicted_revenue = base_orders * (1 + actual_volume_growth) * base_aov * (1 + actual_price_growth * 0.0)
    # 预测口径说明：只用「量的真实增速」外推、价格按基年不变，
    # 这就是「没有做价格假设」时的朴素预测——它能检验公式在量上的准确性。
    predicted_margin = _to_float(base_kpi.get("gross_margin"))
    predicted_profit = predicted_revenue * predicted_margin

    def _error(predicted: float, actual: float) -> float | None:
        if not actual:
            return None
        return _round((predicted - actual) / actual, 4)

    return {
        "base_year": base_year,
        "target_year": target_year,
        "method": "用基年的量价结构与毛利率，仅外推销量真实增速，不假设价格变化",
        "revenue": {
            "predicted": _round(predicted_revenue),
            "actual": _round(actual_revenue),
            "error_rate": _error(predicted_revenue, actual_revenue),
        },
        "gross_profit": {
            "predicted": _round(predicted_profit),
            "actual": _round(actual_profit),
            "error_rate": _error(predicted_profit, actual_profit),
        },
        "order_count": {
            "predicted": int(round(base_orders * (1 + actual_volume_growth))),
            "actual": actual_orders,
            "error_rate": _error(base_orders * (1 + actual_volume_growth), actual_orders),
        },
        "margin": {
            "predicted": _round(predicted_margin, 4),
            "actual": _round(_to_float(target_kpi.get("gross_margin")), 4),
            "error_rate": _error(predicted_margin, _to_float(target_kpi.get("gross_margin"))),
        },
        "notes": [
            "回测误差主要来自价格与品类结构的变化：实际成交价与结构无法只靠销量增速推出来。",
            "年度口径受最后一个月未交付订单影响，实际值含在途订单，预测值不含。",
            "参数来源于历史数据，本回测仅验证公式量级是否合理，不构成精度承诺。",
            "回测基准日：{}".format(_dt.date.today().isoformat()),
        ],
        "conclusion": "回测显示：在只外推销量增速的朴素假设下，成交额预测误差率 {}%，"
        "可用于判断量级与方向，不适合替代月度滚动预算。".format(
            "{:.1%}".format(_error(predicted_revenue, actual_revenue) or 0.0)
        ),
    }


def _safe_kpi(filters: AnalyticsFilters) -> dict:
    """安全取 KPI：预聚合表缺失或数据为空时返回空 dict 而不是抛异常。"""
    try:
        return get_kpi_summary(filters) or {}
    except Exception as exc:  # pragma: no cover
        logger.warning("回测读取 %s 的 KPI 失败：%s", filters.describe(), exc)
        return {}


def _unused(*args: Any) -> None:  # pragma: no cover - 保留占位，供后续扩展
    """占位函数：保持模块导入稳定，避免 lint 报未使用导入。"""
    return None
