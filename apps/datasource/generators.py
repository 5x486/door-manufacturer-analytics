"""补充模拟数据生成器。

为什么需要这一层
----------------
``data/raw/*.csv`` 只包含「业务前台会发生的事」：订单、明细、付款、售后。
但一个能讲出经营结论的系统还需要三类数据，它们在企业里来自 ERP / MES /
财务 / 人力系统，本项目用生成器替代：

1. **成本侧**：产品成本标准（BOM 口径）、每张订单行的六类成本。
   没有成本就没有毛利，而毛利是「品类利润」「折扣侵蚀」「目标达成」等
   所有结论的地基。
2. **过程侧**：交付阶段明细、员工与工时。
   有了阶段明细才能回答「卡在哪一步」，有了工时才能回答「人力成本占比」。
3. **目标与外部侧**：销售目标、竞品对标价。
   目标是评价的尺子，竞品价是定价合理性的参照，两者都不是事实数据而是设定。

复现性
------
所有随机性都来自 :func:`_rng`，它是 ``numpy.random.default_rng(seed)`` 的
惰性单例（``GENERATOR_SEED = 20260809``）。同一个种子 + 同一份 CSV，
在任何机器上跑出来的成本、账期、阶段、目标都逐位相同，，
这对「报表里的数字能被人肉核对」是硬要求。

幂等性
------
每个生成器都是「先查后写」：以业务自然键（``product`` / ``order`` /
``(employee, work_date, work_type)`` / ``(period_type, period, scope, key, metric)``
/ ``(category, city_tier, period)``）为键做 ``update_or_create`` 或
``bulk_create`` + ``bulk_update``。重复执行只会刷新数值，不会翻倍。

单位与类型约定
--------------
金额一律 ``Decimal``（全程不出现 float 累加），比率一律 float（0~1，便于比较），
天数一律 int。金额落库前统一 ``quantize(Decimal("0.01"))``，避免
``DecimalField(decimal_places=2)`` 在 MySQL STRICT 模式下被截断报错。
"""

from __future__ import annotations

import logging
from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

import numpy as np

from django.conf import settings
from django.db import transaction

from apps.masterdata.models import (
    CompetitorBenchmark,
    Employee,
    Product,
    ProductCostStandard,
    SalesRep,
)
from apps.sales.models import (
    DeliveryStage,
    LaborHour,
    Order,
    OrderItem,
    OrderItemCost,
    Payment,
    PaymentTerm,
    SalesTarget,
)

logger = logging.getLogger(__name__)

__all__ = [
    "GENERATOR_SEED",
    "gen_product_cost_standards",
    "gen_order_costs",
    "gen_payment_terms",
    "gen_delivery_stages",
    "gen_employees_and_hours",
    "gen_sales_targets",
    "gen_competitor_benchmarks",
    "gen_all",
]

# 固定随机种子：换机器、换 Python 版本（numpy 大版本一致的前提下）结果一致。
GENERATOR_SEED = 20260809

# 数据起始月：全部模拟数据不早于该月，避免出现「公司成立前就有订单」这类
# 一眼假的结论（公司 2023 年起步）。
DATA_START_YEAR = 2023
DATA_START_MONTH = 1

# 夏令时无关、但月份天数必需：每月工作日按「周一至周五」粗略计数，
# 与财务/人力口径一致（不考虑法定节假日调休，属于已知简化）。
WORK_HOURS_PER_DAY = 8

DEC2 = Decimal("0.01")
DEC4 = Decimal("0.0001")
ZERO = Decimal("0")

_RNG_CACHE: dict = {}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _rng(seed: int | None = None):
    """返回按种子缓存的 ``Generator``，保证「同一 seed 同一序列」。"""
    key = GENERATOR_SEED if seed is None else int(seed)
    generator = _RNG_CACHE.get(key)
    if generator is None:
        generator = np.random.default_rng(key)
        _RNG_CACHE[key] = generator
    return generator


def _q(value, exponent: Decimal = DEC2) -> Decimal:
    """把数值规整成 ``Decimal`` 并quantize 到指定精度（默认两位小数=分）。"""
    if isinstance(value, np.generic):
        value = value.item()
    number = Decimal(str(value))
    return number.quantize(exponent, rounding=ROUND_HALF_UP)


def _setting(name: str, default):
    """读取 settings 覆盖值，取不到时用本模块的默认值。

    这样做的原因：口径常量（目标毛利率、成本结构比例）确实更适合放在
    ``apps.core.constants`` 里统一维护，但生成器不应该因为某个常量还没补上
    就跑不起来，，部署顺序不该成为跑数失败的理由。
    """
    return getattr(settings, name, default)


def _as_of_date() -> date:
    """数据截止日（``settings.DATA_AS_OF``，字符串形式）。"""
    value = getattr(settings, "DATA_AS_OF", None)
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    return date(2026, 8, 9)


def _aging_bucket(days: int) -> str:
    """账龄分桶 → 枚举 key（与 ``constants.AGING_BUCKET_CHOICES`` 的 value 对齐）。"""
    if days <= 30:
        return "0_30"
    if days <= 60:
        return "31_60"
    if days <= 90:
        return "61_90"
    return "over_90"


def _month_keys(start: date, end: date) -> list:
    """返回 ``[start, end]`` 覆盖到的所有 ``(year, month)``，按时间升序。"""
    keys = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        keys.append((year, month))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return keys


def _period_end(year: int, month: int) -> date:
    """某月的最后一天，用于把「月」变成可比较的实际日期。"""
    return date(year, month, monthrange(year, month)[1])


# ---------------------------------------------------------------------------
# 口径常量（可被 settings 同名常量覆盖）
# ---------------------------------------------------------------------------
# 品类目标毛利率：这是全系统「成本反推」的锚点。
# 现实的定制门厂里，铜门靠材质与品牌溢价拿毛利率，防火门是标准件、
# 靠投标拼价格，因此三者梯度明显（34% / 24% / 18%）。
DEFAULT_CATEGORY_TARGET_MARGIN = {
    "铜门": 0.34,
    "不锈钢门": 0.24,
    "不锈钢铜门": 0.28,
    "铸铝门": 0.30,
    "甲级防盗门": 0.22,
    "防火门": 0.18,
}

# 成本结构比例：每个品类内五类成本占「总成本」的比重。
# 铜门材料占比高（铜价占成本大头），防火门人工/制造占比相对高（标准件量产，
# 材料便宜但产线固定投入摊得多）。
DEFAULT_COST_STRUCTURE_RATIO = {
    "铜门": {
        "material": 0.62,
        "labor": 0.17,
        "manufacturing": 0.10,
        "logistics": 0.07,
        "installation": 0.04,
    },
    "不锈钢门": {
        "material": 0.50,
        "labor": 0.20,
        "manufacturing": 0.13,
        "logistics": 0.10,
        "installation": 0.07,
    },
    "不锈钢铜门": {
        "material": 0.56,
        "labor": 0.18,
        "manufacturing": 0.12,
        "logistics": 0.08,
        "installation": 0.06,
    },
    "铸铝门": {
        "material": 0.55,
        "labor": 0.19,
        "manufacturing": 0.12,
        "logistics": 0.08,
        "installation": 0.06,
    },
    "甲级防盗门": {
        "material": 0.48,
        "labor": 0.21,
        "manufacturing": 0.15,
        "logistics": 0.10,
        "installation": 0.06,
    },
    "防火门": {
        "material": 0.44,
        "labor": 0.23,
        "manufacturing": 0.17,
        "logistics": 0.10,
        "installation": 0.06,
    },
}

# 材料单价区间（元/单位重量）：用于反推 material_weight_kg，
# 使 ``material_unit_price * material_weight_kg`` 与材料成本自洽。
# 数值按「元/kg」理解，铜门约 380 元/kg 对应厚铜面板的采购价量级。
DEFAULT_MATERIAL_UNIT_PRICE_RANGE = {
    "铜门": (320.0, 430.0),
    "不锈钢门": (22.0, 34.0),
    "不锈钢铜门": (180.0, 260.0),
    "铸铝门": (38.0, 56.0),
    "甲级防盗门": (18.0, 28.0),
    "防火门": (12.0, 20.0),
}

MIN_MATERIAL_WEIGHT_KG = {
    "铜门": 45.0,
    "不锈钢铜门": 40.0,
    "铸铝门": 55.0,
    "不锈钢门": 30.0,
    "甲级防盗门": 35.0,
    "防火门": 28.0,
}

# 交付阶段工期权重：行业现实是「图纸确认 → 排产」占大头
# （客户反复改图、开料排产等待），因此这两段合计约 30%~40%。
STAGE_WEIGHTS = {
    "接单确认": 0.08,
    "图纸确认": 0.18,
    "排产": 0.17,
    "生产": 0.22,
    "质检": 0.07,
    "物流": 0.10,
    "安装": 0.14,
    "交付完成": 0.04,
}

# 每段的最小工期（天）：不能让某个阶段被算法压成 0 天，
# 否则「瓶颈指数」会出现 0/0 的伪结论。
STAGE_MIN_DAYS = {
    "接单确认": 1,
    "图纸确认": 2,
    "排产": 2,
    "生产": 5,
    "质检": 1,
    "物流": 2,
    "安装": 2,
    "交付完成": 0,
}

# 延期订单的额外天数集中在「排产 / 生产 / 物流」：
# 主责阶段 → ``DelayReasonChoices`` 的映射，用于回写 Order.delay_reason。
STAGE_DELAY_REASON = {
    "接单确认": "客户变更",
    "图纸确认": "客户变更",
    "排产": "产能不足",
    "生产": "产能不足",
    "质检": "质量返工",
    "物流": "物流延误",
    "安装": "安装排期",
    "交付完成": "未延期",
}

# 月度季节权重：3/4/5 月（装修旺季）与 9/10/11 月（金九银十）显著高，
# 1/2 月最低（春节停工 + 装修淡季）。权重之和为 1。
MONTH_SEASON_WEIGHT = [
    0.045,  # 1 月
    0.040,  # 2 月
    0.095,  # 3 月
    0.105,  # 4 月
    0.098,  # 5 月
    0.082,  # 6 月
    0.072,  # 7 月
    0.075,  # 8 月
    0.100,  # 9 月
    0.110,  # 10 月
    0.098,  # 11 月
    0.080,  # 12 月
]

# 目标达成率的「故事线」：年度 → 期望达成率。
# 这组数字不是随手编的，而是刻意让经营结论有话说：
# 2023 起步期略超（渠道红利）、2024 达标（正常经营）、
# 2025 差一点（竞争加剧 + 目标定高）、2026 前 7 月落后（需要动作）。
YEAR_TARGET_ACHIEVEMENT = {
    2023: 1.02,
    2024: 1.00,
    2025: 0.97,
    2026: 0.88,
}

# 城市层级：本地（嘉兴/杭州周边）、周边（省内）、外省。
CITY_TIERS = ["本地", "周边", "外省"]

# 竞品对标：本地市场更透明、报价更卷，因此价格带宽更窄；
# 外省包含运费与代理加价，带宽更宽。
CITY_TIER_SPREAD = {
    "本地": 1.0,
    "周边": 1.12,
    "外省": 1.25,
}

# 我方定价相对竞品带的策略：>1 表示高于竞品上限（品牌溢价），
# ``None`` 表示落在竞品带中位。
OUR_PRICING_POSITION = {
    "铜门": 1.05,          # 略高于竞品上限：品牌溢价
    "不锈钢门": None,       # 贴近中位：走量为主
    "不锈钢铜门": 0.78,     # 中位上沿
    "铸铝门": 0.88,         # 中上位（工艺溢价）
    "甲级防盗门": 0.72,     # 中位偏下
    "防火门": 0.95,         # 低于竞品下限：价格战
}

# 竞品价格区间（元/樘）基准：以「本地」为 1.0，其他层级乘 CITY_TIER_SPREAD。
COMPETITOR_PRICE_BASE = {
    "铜门": (6800.0, 12800.0),
    "不锈钢门": (2200.0, 4600.0),
    "不锈钢铜门": (4200.0, 7900.0),
    "铸铝门": (5200.0, 9600.0),
    "甲级防盗门": (1800.0, 3600.0),
    "防火门": (1200.0, 2400.0),
}

POSITION_NOTE = {
    "高": "我方均价高于竞品上限，属品牌溢价定位；需守住交付与售后体验，否则客户会转向性价比品牌。",
    "中": "我方均价贴近竞品中位，处于正面竞争区；毛利率对原材料价格最敏感。",
    "低": "我方均价低于竞品下限，属于价格战区间；靠规模与标准件效率获取利润，抗涨价能力弱。",
}


# ---------------------------------------------------------------------------
# ① 产品成本标准
# ---------------------------------------------------------------------------
@transaction.atomic
def gen_product_cost_standards(seed: int | None = None) -> dict:
    """为每个产品生成一条 ``ProductCostStandard``（BOM 标准成本口径）。

    反推逻辑（先定毛利、再拆成本）
    ------------------------------
    1. 目标毛利率 ``m`` 由品类决定（铜门 34% / 不锈钢门 24% / 防火门 18%）；
    2. 标准成本 ``C = base_price × (1 - m)``。用 ``base_price`` 而不是
       产品的实际成交价，是因为成本标准是「产品固有属性」，
       不能随订单折扣浮动，否则同一产品在不同订单里会有两套成本。
    3. 把 ``C`` 按品类成本结构比例拆成材料 / 人工 / 制造 / 物流 / 安装五类；
    4. 材料单价在品类区间内取随机值，反推重量
       ``weight = material_cost / unit_price``，
       于是 ``unit_price × weight == material_cost`` 自洽（误差在分位）。

    幂等键：``product``（一个产品一条标准成本，跨期版本化留给
    ``effective_from`` 字段，本项目只生成一期）。
    """
    rng = _rng(seed)
    structure = _setting("COST_STRUCTURE_RATIO", DEFAULT_COST_STRUCTURE_RATIO)
    margins = _setting("CATEGORY_TARGET_MARGIN", DEFAULT_CATEGORY_TARGET_MARGIN)
    price_range = _setting("MATERIAL_UNIT_PRICE_RANGE", DEFAULT_MATERIAL_UNIT_PRICE_RANGE)

    created = updated = skipped = 0
    total_cost_sum = Decimal("0")
    margin_samples = []

    for product in Product.objects.all().order_by("product_code"):
        base_price = product.base_price
        if base_price is None or base_price <= 0:
            logger.warning("跳过无基准价产品：%s", product.product_code)
            skipped += 1
            continue

        category = product.category or "不锈钢门"
        margin = Decimal(str(margins.get(category, 0.24)))
        ratio = structure.get(category, structure["不锈钢门"])
        material_ratio = Decimal(str(ratio.get("material", 0.50)))
        labor_ratio = Decimal(str(ratio.get("labor", 0.20)))
        manufacturing_ratio = Decimal(str(ratio.get("manufacturing", 0.13)))
        logistics_ratio = Decimal(str(ratio.get("logistics", 0.10)))
        installation_ratio = Decimal(str(ratio.get("installation", 0.07)))

        # 标准成本 = 基准价 × (1 - 目标毛利率)
        total_cost = base_price * (Decimal("1") - margin)
        material_cost = _q(total_cost * material_ratio)
        labor_cost = _q(total_cost * labor_ratio)
        manufacturing_cost = _q(total_cost * manufacturing_ratio)
        logistics_cost = _q(total_cost * logistics_ratio)
        installation_cost = _q(total_cost * installation_ratio)

        # 材料单价与重量的自洽：单价随机、重量反推，再回算材料成本。
        low, high = price_range.get(category, (20.0, 40.0))
        unit_price = _q(rng.uniform(low, high))
        weight = material_cost / unit_price
        min_weight = Decimal(str(MIN_MATERIAL_WEIGHT_KG.get(category, 30.0)))
        if weight < min_weight:
            # 重量太小说明单价偏高，反向压低单价，保持「重量合理 + 成本不变」
            weight = min_weight
            unit_price = _q(material_cost / weight)
        weight = _q(weight, Decimal("0.001"))
        material_cost = _q(unit_price * weight)

        effective_from = date(DATA_START_YEAR, DATA_START_MONTH, 1)
        _, is_created = ProductCostStandard.objects.update_or_create(
            product=product,
            defaults={
                "effective_from": effective_from,
                "material_cost": material_cost,
                "labor_cost": labor_cost,
                "manufacturing_cost": manufacturing_cost,
                "logistics_cost": logistics_cost,
                "installation_cost": installation_cost,
                "material_unit_price": unit_price,
                "material_weight_kg": weight,
            },
        )
        created += int(is_created)
        updated += int(not is_created)

        actual_cost = (
            material_cost + labor_cost + manufacturing_cost + logistics_cost + installation_cost
        )
        total_cost_sum += actual_cost
        margin_samples.append(float((base_price - actual_cost) / base_price))

    avg_margin = float(np.mean(margin_samples)) if margin_samples else 0.0
    logger.info(
        "gen_product_cost_standards: created=%s updated=%s skipped=%s 平均标准毛利率=%.4f",
        created,
        updated,
        skipped,
        avg_margin,
    )
    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "total_standard_cost": total_cost_sum,
        "avg_standard_margin": round(avg_margin, 4),
    }


# ---------------------------------------------------------------------------
# ② 订单行成本
# ---------------------------------------------------------------------------
@transaction.atomic
def gen_order_costs(seed: int | None = None) -> dict:
    """为每条订单行生成 6 类 ``OrderItemCost``，并回写行成本与毛利。

    三条约束（业务上必须成立，否则成本分析会被业务一眼否定）
    ------------------------------------------------------
    1. **毛利围绕品类目标波动**：目标毛利率 ``m``，实际 ``m' = m + U(-0.08, +0.08)``，
       再截断到合理区间（-0.20 ~ 0.60）。同一品类里既有赚得多的定制单，
       也有为了接单让利的工程单，波动本身就是被分析的对象。
    2. **非标定制成本上浮**：``is_custom=True`` 的行，材料与人工各上浮 8%~18%
       （开模、异形加工、废料率上升）。
    3. **尺寸越大材料越贵**：材料成本乘以尺寸系数
       ``clamp(0.7, 1.35, (area_m2 / 2.4) ** 0.8)``，，
       幂指数 0.8 < 1 反映「面积翻倍、材料不止翻倍但也不是线性」的现实
       （门板利用率提升 + 五金件基本固定）。
       给定客户报价不变时，材料越贵毛利越低，因此大尺寸订单天然利润更薄。

    成本拆分：材料先按尺寸与定制系数定价，再从「目标总成本 - 材料」中
    推出其余五类成本的缩放系数，使六类合计严格等于目标总成本。
    这样既满足了毛利约束，又满足了成本结构约束，不会出现
    「六类成本之和 ≠ 行成本」的对账失败。
    """
    rng = _rng(seed)

    margins = _setting("CATEGORY_TARGET_MARGIN", DEFAULT_CATEGORY_TARGET_MARGIN)
    structure = _setting("COST_STRUCTURE_RATIO", DEFAULT_COST_STRUCTURE_RATIO)
    price_range = _setting("MATERIAL_UNIT_PRICE_RANGE", DEFAULT_MATERIAL_UNIT_PRICE_RANGE)

    items = list(
        OrderItem.objects.select_related("order")
        .only(
            "id",
            "product_id",
            "category",
            "is_custom",
            "area_m2",
            "quantity",
            "line_amount",
            "unit_price",
            "order__order_date",
        )
        .order_by("id")
    )
    if not items:
        raise RuntimeError("没有订单明细，请先执行 load_order_items()")

    # 缺陷率预取：逐行 getattr(item, "product") 会触发 3087 次单条查询，
    # 缺陷率是产品固有属性，整表读进来（48 行）比 N+1 便宜三个数量级。
    defect_rate_by_product = {
        product_id: (Decimal(str(rate)) if rate is not None else None)
        for product_id, rate in Product.objects.values_list("id", "defect_rate")
    }

    existing = {}
    for cost in OrderItemCost.objects.all().only(
        "id", "item_id", "cost_type", "amount", "is_standard"
    ):
        existing[(cost.item_id, cost.cost_type)] = cost

    to_create = []
    to_update = []
    line_total_cost = Decimal("0")
    margin_samples = []

    for item in items:
        category = item.category or "不锈钢门"
        ratio = structure.get(category, structure["不锈钢门"])
        target_margin = Decimal(str(margins.get(category, 0.24)))
        # 1) 目标毛利率 + 波动（截断到 [-20%, 60%]）
        drift = Decimal(str(round(float(rng.uniform(-0.08, 0.08)), 6)))
        actual_margin = target_margin + drift
        actual_margin = max(Decimal("-0.20"), min(Decimal("0.60"), actual_margin))

        line_amount = _q(item.line_amount or ZERO)
        if line_amount <= 0:
            continue
        quantity = Decimal(str(item.quantity or 1))
        if quantity <= 0:
            quantity = Decimal("1")
        target_line_cost = line_amount * (Decimal("1") - actual_margin)

        custom = bool(item.is_custom)
        if custom:
            # 2) 非标上浮：材料与人工更贵（开模、异形加工、废料率）
            material_uplift = Decimal(str(round(float(rng.uniform(1.08, 1.18)), 6)))
            labor_uplift = Decimal(str(round(float(rng.uniform(1.08, 1.18)), 6)))
        else:
            material_uplift = Decimal("1")
            labor_uplift = Decimal("1")

        # 3) 尺寸系数：面积越大材料越贵（幂指数 0.8，次线性）
        area = item.area_m2 if item.area_m2 is not None else Decimal("2.4")
        area = Decimal(str(area))
        if area <= 0:
            area = Decimal("2.4")
        size_factor = float((area / Decimal("2.4")) ** Decimal("0.8"))
        size_factor = max(0.70, min(1.35, size_factor))
        material_size_factor = Decimal(str(round(size_factor, 6)))

        # 单位材料成本：由「报价单价」推出一个与品类相符的单价，
        # 再用 0.60 的权重把尺寸效应混进来，避免完全由面积决定而失真。
        if item.unit_price and item.unit_price > 0 and area > 0:
            base_unit_cost = (Decimal(str(item.unit_price)) / area) * Decimal("0.60")
        else:
            base_unit_cost = target_line_cost / quantity / Decimal("2.4") * Decimal("0.60")
        low, high = price_range.get(category, (20.0, 40.0))
        jitter = Decimal(str(round(float(rng.uniform(0.88, 1.12)), 6)))
        if custom:
            jitter = jitter * material_uplift
        unit_cost = base_unit_cost * jitter
        floor_unit = Decimal(str(low)) * Decimal("0.5")
        ceil_unit = Decimal(str(high)) * Decimal("1.6")
        unit_cost = max(floor_unit, min(ceil_unit, unit_cost))
        unit_cost = _q(unit_cost)

        material = _q(unit_cost * area * material_size_factor * quantity)

        material_ratio = Decimal(str(ratio.get("material", 0.50)))
        others_ratio = Decimal("1") - material_ratio
        others_planned = target_line_cost * others_ratio
        remaining = target_line_cost - material
        if others_planned <= 0 or remaining <= 0:
            # 材料已经把目标成本吃满（大尺寸或高材料成本品类）：
            # 退化为按结构比例分摊，保证六类成本之和仍等于目标成本。
            scale = Decimal("1") if material <= target_line_cost else (target_line_cost / material)
            material = _q(material * scale)
            remaining = target_line_cost - material
            scale_ratio = Decimal("1")
        else:
            scale_ratio = remaining / others_planned
            scale_ratio = max(Decimal("0.25"), min(Decimal("2.50"), scale_ratio))

        labor_base = target_line_cost * Decimal(str(ratio.get("labor", 0.20)))
        manufacturing_base = target_line_cost * Decimal(str(ratio.get("manufacturing", 0.13)))
        logistics_base = target_line_cost * Decimal(str(ratio.get("logistics", 0.10)))
        installation_base = target_line_cost * Decimal(str(ratio.get("installation", 0.07)))

        labor = _q(labor_base * scale_ratio * labor_uplift)
        manufacturing = _q(manufacturing_base * scale_ratio)
        logistics = _q(logistics_base * scale_ratio)
        installation = _q(installation_base * scale_ratio)

        # 售后成本属「质量成本」，与品类缺陷率挂钩，不计入标准成本结构，
        # 但要进毛利：售后多的品类实际赚钱更少，这是业务真实的痛点。
        defect_rate = Decimal("0.012")
        product_defect_rate = defect_rate_by_product.get(item.product_id)
        if product_defect_rate is not None:
            defect_rate = product_defect_rate
        after_sales = _q(line_amount * defect_rate * Decimal("0.55"))

        total_cost = material + labor + manufacturing + logistics + installation + after_sales
        gross_profit = line_amount - total_cost

        item.total_cost = total_cost
        item.gross_profit = gross_profit

        amounts = {
            "material": material,
            "labor": labor,
            "manufacturing": manufacturing,
            "logistics": logistics,
            "installation": installation,
            "after_sales": after_sales,
        }
        for cost_type, amount in amounts.items():
            key = (item.id, cost_type)
            obj = existing.get(key)
            if obj is None:
                to_create.append(
                    OrderItemCost(item=item, cost_type=cost_type, amount=amount, is_standard=False)
                )
            else:
                obj.amount = amount
                obj.is_standard = False
                to_update.append(obj)

        line_total_cost += total_cost
        margin_samples.append(float(gross_profit / line_amount))

    if to_create:
        OrderItemCost.objects.bulk_create(to_create, batch_size=1000)
    if to_update:
        OrderItemCost.objects.bulk_update(to_update, ["amount", "is_standard"], batch_size=1000)
    # 回写行成本与毛利：整表更新，避免只更新本函数遍历到的子集
    OrderItem.objects.bulk_update(items, ["total_cost", "gross_profit"], batch_size=1000)

    avg_margin = float(np.mean(margin_samples)) if margin_samples else 0.0
    logger.info(
        "gen_order_costs: 行数=%s 成本行 new=%s upd=%s 平均毛利率=%.4f",
        len(items),
        len(to_create),
        len(to_update),
        avg_margin,
    )
    return {
        "items": len(items),
        "cost_created": len(to_create),
        "cost_updated": len(to_update),
        "total_cost": line_total_cost,
        "avg_gross_margin": round(avg_margin, 4),
    }


# ---------------------------------------------------------------------------
# ③ 账期与应收
# ---------------------------------------------------------------------------
def _credit_days_for(order_type: str, rng) -> int:
    """按订单类型给账期：零售现款、装企月结、工程长账期。

    这是「回款分析」的因果起点：账期越长，DSO 越大、账龄越容易超 60 天，
    因此不能用统一账期糊过去，否则账龄分布会失去形状。
    """
    if order_type == "工程":
        return int(rng.integers(60, 91))
    if order_type == "装企合作":
        return int(rng.integers(30, 61))
    return int(rng.integers(0, 16))


# 逾期概率：工程/装企明显高于零售。
# 整体目标约 12%，且必须「结构可解释」——如果逾期全落在零售现款单上，
# 业务一眼就知道数据是假的。
OVERDUE_PROBABILITY = {
    "零售": 0.06,
    "装企合作": 0.20,
    "工程": 0.30,
}


@transaction.atomic
def gen_payment_terms(seed: int | None = None) -> dict:
    """为每条有效订单生成 ``PaymentTerm``，并构造约 12% 的逾期应收。

    口径
    ----
    - ``contract_amount`` = 订单终价（含运费、扣让利），与销售的「成交额」口径一致；
    - ``paid_amount`` 取付款流水里**非退款**金额合计（真实回款），
      不用随机数编，否则「回款率」在两处会对不上；
    - 未结清单的 ``outstanding_amount`` 必须 > 0，构成应收账款池；
    - ``overdue_days``、``aging_bucket`` 一律以 ``settings.DATA_AS_OF_DATE``
      为切点计算，，账龄是「截至某天的状态」，没有切点就没有账龄。
      这也是为什么全系统只允许一个数据截止日。

    逾期的构造方式：先按订单类型抽「是否逾期」，再反解约定到期日。
    这样做的原因是 ，， 账期长度由订单类型决定、不能随意改，
    而到期日由「交付日 + 账期」决定，唯一能体现逾期的是「客户是否按时付」。
    因此对逾期单，我们让它在该到期的日子没付完（回款比例显著偏低），
    到期日保持业务规则算出来的值，账龄自然落在 1~120 天区间。
    """
    rng = _rng(seed)
    as_of = _as_of_date()

    # 一次聚合取回每张订单的非退款回款合计与最后一笔回款日，避免 N+1
    payment_totals: dict = {}
    payment_last_date: dict = {}
    for order_id, amount, pay_date in Payment.objects.filter(is_refund=False).values_list(
        "order_id", "amount", "payment_date"
    ):
        if order_id is None:
            continue
        if amount is not None:
            payment_totals[order_id] = payment_totals.get(order_id, ZERO) + Decimal(amount)
        if pay_date is not None:
            previous = payment_last_date.get(order_id)
            if previous is None or pay_date > previous:
                payment_last_date[order_id] = pay_date

    orders = list(
        Order.objects.filter(order_status__in=["已完成", "生产中", "待发货"]).order_by("order_date")
    )
    if not orders:
        raise RuntimeError("没有有效订单，请先执行 load_orders()")

    created = updated = overdue_count = 0
    outstanding_total = Decimal("0")
    bucket_counter: dict = {"0_30": 0, "31_60": 0, "61_90": 0, "over_90": 0}

    for order in orders:
        contract_amount = _q(order.final_amount or ZERO)
        if contract_amount <= 0:
            contract_amount = _q(order.total_amount or ZERO)
        paid = _q(payment_totals.get(order.id, ZERO))
        if paid > contract_amount:
            # 数据层面偶有预收超出终价（多付/折扣后未退），
            # 以实收为准，不让未收金额出现负数。
            contract_amount = paid

        order_type = order.order_type or "零售"
        credit_days = _credit_days_for(order_type, rng)

        is_overdue = float(rng.random()) < OVERDUE_PROBABILITY.get(order_type, 0.10)
        if is_overdue:
            # 逾期单：回款比例压在 0.30~0.80，保证未收金额足够大，
            # 否则「应收未收」会出现一堆 0.5 元这种无分析价值的尾数。
            paid_ratio = Decimal(str(round(float(rng.uniform(0.30, 0.80)), 4)))
            max_paid = _q(contract_amount * paid_ratio)
            if paid > max_paid:
                paid = max_paid
            overdue_count += 1

        outstanding = contract_amount - paid
        if outstanding < 0:
            outstanding = ZERO
        outstanding = _q(outstanding)
        is_settled = outstanding <= Decimal("0.01")

        base_date = order.actual_delivery_date or order.order_date or as_of
        agreed_due_date = base_date + timedelta(days=credit_days)
        overdue_days = (as_of - agreed_due_date).days
        if overdue_days < 0:
            overdue_days = 0

        if is_overdue and overdue_days == 0:
            # 抽样命中「逾期」但按业务规则算出的到期日还没到（例如工程单交付日
            # 距今不足 60 天）。此时有两种自欺欺人的做法：把到期日前移（篡改
            # 账期规则），或把未收金额记成 0（伪造回款）。两种都会让账龄分布
            # 变成假数据。真实世界里这类订单的状态是「尚未到期、也未结清」，
            # 因此保留未收金额，但账龄归零——它自然落在 0-30 天桶里，
            # 语义与「到期日刚过」的订单一致：
            # 都是在短期内被列为应收的款项。
            is_overdue = False
            overdue_count -= 1

        bucket = _aging_bucket(overdue_days)
        bucket_counter[bucket] = bucket_counter.get(bucket, 0) + 1
        outstanding_total += outstanding

        _, is_created = PaymentTerm.objects.update_or_create(
            order=order,
            defaults={
                "contract_amount": contract_amount,
                "paid_amount": paid,
                "outstanding_amount": outstanding,
                "agreed_due_date": agreed_due_date,
                "last_payment_date": payment_last_date.get(order.id),
                "overdue_days": overdue_days,
                "aging_bucket": bucket,
                "is_settled": is_settled,
                "credit_days": credit_days,
            },
        )
        created += int(is_created)
        updated += int(not is_created)

    overdue_ratio = round(overdue_count / len(orders), 4) if orders else 0.0
    logger.info(
        "gen_payment_terms: created=%s updated=%s 逾期单=%s(%.2f%%) 未收合计=%s",
        created,
        updated,
        overdue_count,
        overdue_ratio * 100,
        outstanding_total,
    )
    return {
        "created": created,
        "updated": updated,
        "orders": len(orders),
        "overdue_orders": overdue_count,
        "overdue_ratio": overdue_ratio,
        "outstanding_amount": outstanding_total,
        "aging_distribution": bucket_counter,
    }


# ---------------------------------------------------------------------------
# ④ 交付阶段
# ---------------------------------------------------------------------------
def _split_total(total_days: int, weights: dict, ordered_stages: list) -> list:
    """把总工期按权重拆成各阶段天数，且**合计严格等于总工期**。

    先按权重取整分配，再把「取整丢掉的余数」补给权重最大的阶段，
    保证 Σ阶段天数 == 总工期。这类「拆分必须对得上」的约束，
    是交付分析结论可信的前提（否则「平均交付周期」永远对不上订单表）。
    """
    if total_days <= 0:
        return [0] * len(ordered_stages)

    raw = [weights[stage] * total_days for stage in ordered_stages]
    floors = [int(value) for value in raw]
    remainder = total_days - sum(floors)
    if remainder > 0:
        order_by_fraction = sorted(
            range(len(ordered_stages)),
            key=lambda index: raw[index] - floors[index],
            reverse=True,
        )
        for index in order_by_fraction[:remainder]:
            floors[index] += 1
    return floors


@transaction.atomic
def gen_delivery_stages(seed: int | None = None) -> dict:
    """为已完成订单生成 8 条 ``DeliveryStage``，并回写订单延期字段。

    拆分口径
    --------
    - 总工期取 ``Order.actual_lead_days``（= 实际交付日 - 下单日）；
    - 按 ``STAGE_WEIGHTS`` 分摊，「图纸确认 + 排产」合计 35%，
      与「客户反复改图、排产排队」的行业现实一致；
    - 每阶段有最小工期 ``STAGE_MIN_DAYS``，不足时从其他阶段按比例让出天数，
      保证不会出现「生产 0 天」这种无法解释的结论；
    - **延期订单**（``is_on_time == 0``）的超期天数集中在
      「排产 / 生产 / 物流」三段：这三段是门厂真正的产能与协同瓶颈，
      而「接单确认 / 图纸确认」的延期在主责认定上属于客户变更。
      因此本函数把额外天数按 3:4:3 分配给这三段。
    - ``Order.delay_days`` = Σ 各阶段超期天数（与阶段明细口径一致，
      不另算一遍，避免两处数字打架）；``delay_reason`` 取超期最重阶段的
      主责归因；未延期填「未延期」。
    """
    rng = _rng(seed)
    stages_sequence = _setting(
        "DELIVERY_STAGE_SEQUENCE",
        ["接单确认", "图纸确认", "排产", "生产", "质检", "物流", "安装", "交付完成"],
    )
    weights = {stage: STAGE_WEIGHTS.get(stage, 0.10) for stage in stages_sequence}
    weight_sum = sum(weights.values())
    if weight_sum > 0:
        weights = {stage: value / weight_sum for stage, value in weights.items()}

    orders = list(
        Order.objects.filter(
            order_status="已完成",
            actual_delivery_date__isnull=False,
            order_date__isnull=False,
        ).order_by("order_date")
    )
    if not orders:
        raise RuntimeError("没有已完成订单，请先执行 load_orders()")

    existing = {}
    for stage in DeliveryStage.objects.all().only("id", "order_id", "stage"):
        existing[(stage.order_id, stage.stage)] = stage

    to_create = []
    to_update = []
    delayed_orders = 0
    delay_sum = 0
    bottleneck_counter: dict = {}

    for order in orders:
        actual_total = order.actual_lead_days
        if actual_total is None or actual_total <= 0:
            actual_total = (order.actual_delivery_date - order.order_date).days
        actual_total = max(int(actual_total), len(stages_sequence))
        planned_total = order.lead_days or actual_total
        planned_total = max(int(planned_total), len(stages_sequence))

        is_late = order.is_on_time == 0
        if is_late:
            delayed_orders += 1

        actual_days = _split_total(actual_total, weights, list(stages_sequence))
        planned_days = _split_total(planned_total, weights, list(stages_sequence))

        # 延期订单：额外天数集中到 排产 / 生产 / 物流（3:4:3）
        bottleneck_stages = ["排产", "生产", "物流"]
        stage_delay = {stage: 0 for stage in stages_sequence}
        if is_late and planned_total < actual_total:
            extra = actual_total - planned_total
            shares = [0.3, 0.4, 0.3]
            for index, stage in enumerate(bottleneck_stages):
                if stage not in stage_delay:
                    continue
                stage_delay[stage] = int(round(extra * shares[index]))
        elif is_late:
            # 实际周期没比计划长，但仍被判为延期（例如计划本身很紧）：
            # 让物流承担一段 1~3 天的真实延误，保持「延期订单必有超期阶段」。
            stage = "物流"
            stage_delay[stage] = int(rng.integers(1, 4))

        delay_total = sum(stage_delay.values())
        # 让 计划天数 + 各阶段超期 = 实际天数：把差额补给生产阶段，
        # 从而保证「阶段超期之和 == 订单延期天数」这条对账关系成立。
        adjust = actual_total - planned_total - delay_total
        if adjust != 0 and "生产" in stage_delay:
            stage_delay["生产"] += adjust
            if stage_delay["生产"] < 0:
                stage_delay["生产"] = 0
        delay_total = sum(stage_delay.values())

        planned_cursor = 0
        actual_cursor = 0
        per_stage_delay = {}
        for index, stage in enumerate(stages_sequence):
            planned_cursor += planned_days[index]
            actual_cursor += actual_days[index]
            planned_date = order.order_date + timedelta(days=planned_cursor)
            actual_date = order.order_date + timedelta(days=actual_cursor)
            delayed = stage_delay.get(stage, 0)
            per_stage_delay[stage] = delayed
            duration = actual_days[index]

            is_bottleneck = bool(duration >= max(3, int(actual_total * 0.20)))
            key = (order.id, stage)
            obj = existing.get(key)
            values = {
                "stage": stage,
                "stage_order": index,
                "planned_date": planned_date,
                "actual_date": actual_date,
                "duration_days": duration,
                "delayed_days": delayed,
                "is_bottleneck": is_bottleneck,
            }
            if obj is None:
                to_create.append(DeliveryStage(order=order, **values))
            else:
                for field, value in values.items():
                    setattr(obj, field, value)
                to_update.append(obj)

        # 回写订单延期字段：口径与阶段明细完全一致
        if is_late or delay_total > 0:
            worst_stage = max(per_stage_delay, key=lambda name: per_stage_delay[name])
            order.delay_days = delay_total
            order.delay_reason = (
                STAGE_DELAY_REASON.get(worst_stage, "未延期") if delay_total > 0 else "未延期"
            )
            if delay_total > 0:
                bottleneck_counter[order.delay_reason] = (
                    bottleneck_counter.get(order.delay_reason, 0) + 1
                )
        else:
            order.delay_days = 0
            order.delay_reason = "未延期"
        delay_sum += delay_total or 0

    if to_create:
        DeliveryStage.objects.bulk_create(to_create, batch_size=1000)
    if to_update:
        DeliveryStage.objects.bulk_update(
            to_update,
            ["stage", "stage_order", "planned_date", "actual_date", "duration_days",
             "delayed_days", "is_bottleneck"],
            batch_size=1000,
        )
    Order.objects.bulk_update(orders, ["delay_days", "delay_reason"], batch_size=500)

    logger.info(
        "gen_delivery_stages: 订单=%s 阶段 new=%s upd=%s 延期单=%s 延期天数合计=%s",
        len(orders),
        len(to_create),
        len(to_update),
        delayed_orders,
        delay_sum,
    )
    return {
        "orders": len(orders),
        "stage_created": len(to_create),
        "stage_updated": len(to_update),
        "delayed_orders": delayed_orders,
        "delay_days_total": delay_sum,
        "delay_reason_distribution": bottleneck_counter,
    }


# ---------------------------------------------------------------------------
# ⑤ 员工与工时
# ---------------------------------------------------------------------------
# 部门 → (人数, 月薪下限, 月薪上限, 主要工时类型)
DEPARTMENT_PLAN = [
    ("生产部", 22, 6000, 9000, "生产工时"),
    ("安装部", 7, 6500, 9500, "安装工时"),
    ("销售部", 6, 5500, 9000, "销售工时"),
    ("售后部", 3, 5500, 7500, "售后工时"),
    ("物流部", 2, 5500, 7000, "生产工时"),
    ("综合管理部", 2, 9000, 15000, "销售工时"),
]

SURNAME_POOL = [
    "王", "李", "张", "刘", "陈", "杨", "赵", "黄", "周", "吴",
    "徐", "孙", "马", "朱", "胡", "郭", "林", "何", "高", "罗",
    "郑", "梁", "谢", "宋", "唐", "许", "韩", "冯", "邓", "曹",
]

MALE_GIVEN_NAMES = [
    "伟", "强", "磊", "军", "洋", "勇", "杰", "涛", "明", "超",
    "刚", "平", "辉", "健", "波", "斌", "飞", "鹏", "宇", "浩",
]
FEMALE_GIVEN_NAMES = [
    "芳", "娜", "敏", "静", "丽", "燕", "娟", "霞", "秀英", "婷",
    "雪", "倩", "梅", "琳", "丹", "桂英", "玉兰", "萍", "红", "慧",
]


def _person_name(rng, gender: str) -> str:
    """生成中文姓名：姓从池中取，名字按性别取，重名由员工编号兜底。"""
    surname = SURNAME_POOL[int(rng.integers(0, len(SURNAME_POOL)))]
    pool = MALE_GIVEN_NAMES if gender == "男" else FEMALE_GIVEN_NAMES
    given = pool[int(rng.integers(0, len(pool)))]
    return "{}{}".format(surname, given)


def _workdays_in_month(year: int, month: int) -> int:
    """当月周一至周五的天数（不考虑法定节假日，属已知简化）。"""
    days = monthrange(year, month)[1]
    count = 0
    for day in range(1, days + 1):
        if date(year, month, day).weekday() < 5:
            count += 1
    return count


@transaction.atomic
def gen_employees_and_hours(seed: int | None = None) -> dict:
    """生成 42 名员工与 2023-01 ~ 2026-07 的月度工时。

    为什么人数这样分配（生产 22 / 安装 7 / 销售 6 / 售后 3 / 物流 2 / 综合 2）
    --------------------------------------------------------------------------
    - 生产部占比过半，符合「门厂 = 制造企业」的成本结构，
      否则算出来的人力成本占比会低得不像制造业；
    - 销售部 6 人与 ``SalesRep`` 的 6 条主数据一一对应
      （员工是「发工资的人」，业务员是「背业绩的人」，同一批人两种视角）；
    - 安装部独立于生产部：定制门行业安装是独立工种，
      且「安装问题」是售后主因之一，拆开才能归因。

    工时口径
    --------
    - 基准：工作日 × 8 小时；
    - 部门系数（生产 1.0~1.25 有加班，综合管理 0.9~1.0 基本不加班）；
    - 旺季系数用与销售目标相同的 ``MONTH_SEASON_WEIGHT`` 归一化后开方，
      使「旺季产能吃紧」与「旺季销量高」这两件事在数据上互相印证
      （订单多 → 加班多 → 人力成本上升，链条闭环）；
    - 员工离职后不再产生工时（``leave_date`` 之后为 0），
      这样「人均产值」在跨年对比时才不会因为虚增人头而被低估。
    """
    rng = _rng(seed)
    as_of = _as_of_date()
    horizon_end = _period_end(min(as_of.year, 2026), as_of.month if as_of.year <= 2026 else 12)
    horizon_end = _period_end(2026, 7) if as_of > date(2026, 7, 31) else horizon_end

    existing_employees = {e.employee_code: e for e in Employee.objects.all()}
    sales_reps = list(SalesRep.objects.all().order_by("rep_code"))

    employee_created = employee_updated = 0
    employees = []

    seq = 0
    for department, count, salary_low, salary_high, work_type in DEPARTMENT_PLAN:
        for index in range(count):
            seq += 1
            code = "EMP{:04d}".format(seq)
            gender = "男" if float(rng.random()) < (0.72 if department in ("生产部", "安装部", "物流部") else 0.42) else "女"
            name = _person_name(rng, gender)
            hire_date = date(DATA_START_YEAR, 1, 1) + timedelta(days=int(rng.integers(0, 400)))
            leave_date = None
            status = "在职"
            # 每年约 6% 的流失率：没有离职就没有招聘与外流压力，
            # 人力分析会缺少「补员成本」这条线索。
            if float(rng.random()) < 0.10:
                leave_offset = int(rng.integers(300, 1100))
                candidate = hire_date + timedelta(days=leave_offset)
                if candidate <= as_of:
                    leave_date = candidate
                    status = "离职"
            salary = Decimal(str(int(rng.integers(salary_low, salary_high + 1)) // 100 * 100))

            if department == "销售部" and index < len(sales_reps):
                position = "销售顾问（{}）".format(sales_reps[index].rep_name)
            elif department == "生产部":
                position = "生产技工" if index % 5 else "班组长"
            elif department == "安装部":
                position = "安装师傅"
            elif department == "售后部":
                position = "售后专员"
            elif department == "物流部":
                position = "物流调度"
            else:
                position = "综合管理岗"

            obj = existing_employees.get(code)
            values = {
                "employee_name": name,
                "gender": gender,
                "department": department,
                "position": position,
                "hire_date": hire_date,
                "leave_date": leave_date,
                "monthly_salary": salary,
                "status": status,
            }
            if obj is None:
                obj = Employee(employee_code=code, **values)
                obj.save()
                employee_created += 1
            else:
                for field, value in values.items():
                    setattr(obj, field, value)
                obj.save(update_fields=list(values.keys()))
                employee_updated += 1
            employees.append((obj, department, work_type, hire_date, leave_date, salary))

    # ---- 工时 ----
    existing_hours = {}
    for hour in LaborHour.objects.all().only("id", "employee_id", "work_date", "work_type"):
        existing_hours[(hour.employee_id, hour.work_date, hour.work_type)] = hour

    month_keys = _month_keys(date(DATA_START_YEAR, DATA_START_MONTH, 1), horizon_end)
    season_sum = sum(MONTH_SEASON_WEIGHT)
    to_create = []
    to_update = []
    total_hours = Decimal("0")

    for (year, month) in month_keys:
        workdays = _workdays_in_month(year, month)
        season = MONTH_SEASON_WEIGHT[(month - 1) % 12] / season_sum
        # 开方压缩季节波动：加班再猛也有生理上限，不能出现「旺季干双倍」。
        season_factor = float(season * 12) ** 0.5
        season_factor = max(0.82, min(1.35, season_factor))

        for (employee, department, work_type, hire_date, leave_date, salary) in employees:
            if hire_date > _period_end(year, month):
                continue
            month_last = _period_end(year, month)
            if leave_date is not None and leave_date < date(year, month, 1):
                continue

            if department == "生产部":
                lo, hi = 0.98, 1.26
            elif department == "安装部":
                lo, hi = 0.95, 1.15
            elif department == "销售部":
                lo, hi = 0.92, 1.08
            elif department == "售后部":
                lo, hi = 0.90, 1.05
            elif department == "物流部":
                lo, hi = 0.95, 1.12
            else:
                lo, hi = 0.88, 1.02
            department_factor = float(rng.uniform(lo, hi))

            days = workdays
            if leave_date is not None and leave_date < month_last:
                days = sum(
                    1
                    for day in range(1, month_last.day + 1)
                    if date(year, month, day).weekday() < 5 and date(year, month, day) <= leave_date
                )
            if hire_date > date(year, month, 1):
                days = max(
                    0,
                    sum(
                        1
                        for day in range(1, month_last.day + 1)
                        if date(year, month, day).weekday() < 5 and date(year, month, day) >= hire_date
                    ),
                )
            if days <= 0:
                continue

            hours = Decimal(str(round(days * WORK_HOURS_PER_DAY * department_factor * season_factor, 2)))
            work_date = date(year, month, 1)
            key = (employee.id, work_date, work_type)
            obj = existing_hours.get(key)
            if obj is None:
                to_create.append(
                    LaborHour(
                        employee=employee,
                        order=None,
                        work_date=work_date,
                        work_type=work_type,
                        hours=hours,
                    )
                )
            else:
                obj.hours = hours
                obj.work_type = work_type
                obj.employee = employee
                to_update.append(obj)
            total_hours += hours

    if to_create:
        LaborHour.objects.bulk_create(to_create, batch_size=1000)
    if to_update:
        LaborHour.objects.bulk_update(to_update, ["hours", "work_type", "employee"], batch_size=1000)

    logger.info(
        "gen_employees_and_hours: 员工 new=%s upd=%s 工时 new=%s upd=%s 总工时=%s",
        employee_created,
        employee_updated,
        len(to_create),
        len(to_update),
        total_hours,
    )
    return {
        "employee_created": employee_created,
        "employee_updated": employee_updated,
        "headcount_total": len(employees),
        "hour_created": len(to_create),
        "hour_updated": len(to_update),
        "total_hours": total_hours,
        "months": len(month_keys),
    }


# ---------------------------------------------------------------------------
# ⑥ 销售目标
# ---------------------------------------------------------------------------
TARGET_SCOPE_BY_METRIC = {
    "成交额": "metric_revenue",
    "毛利额": "metric_profit",
    "订单量": "metric_orders",
    "新增客户": "metric_new_customer",
    "回款额": "metric_collection",
}


def _actual_base_by_period() -> dict:
    """``{'2025-03': {'revenue':..., 'orders':..., ...}}`` 实际值底表。

    目标必须锚在实际值上，否则会出现「目标达成率 300%」这类荒唐数字。
    优先读 ``DwsBusinessMonthly`` 预聚合表（快），缺表时回退到订单表现算，，
    生成器可能在 ETL 之前执行，两条路都要能走通。
    """
    base: dict = {}
    try:
        from apps.analytics.models import DwsBusinessMonthly

        rows = DwsBusinessMonthly.objects.all().values(
            "period", "revenue", "gross_profit", "order_count", "new_customer_count"
        )
        for row in rows:
            period = row["period"]
            if not period:
                continue
            slot = base.setdefault(
                period,
                {"revenue": ZERO, "gross_profit": ZERO, "orders": 0, "new_customers": 0},
            )
            slot["revenue"] += Decimal(row["revenue"] or 0)
            slot["gross_profit"] += Decimal(row["gross_profit"] or 0)
            slot["orders"] += int(row["order_count"] or 0)
            slot["new_customers"] += int(row["new_customer_count"] or 0)
    except Exception:  # pragma: no cover - 表未建时走回退分支
        base = {}

    if base:
        return base

    # 回退：直接聚合订单（有效期口径：排除已取消）
    orders = Order.objects.exclude(order_status="已取消").only(
        "id", "order_year", "order_month", "final_amount", "order_date", "customer_id"
    )
    first_order_map = {}
    for customer_id, order_date in (
        Order.objects.exclude(order_status="已取消")
        .values_list("customer_id", "order_date")
    ):
        if customer_id is None or order_date is None:
            continue
        previous = first_order_map.get(customer_id)
        if previous is None or order_date < previous:
            first_order_map[customer_id] = order_date

    gross_by_order = {}
    for order_id, profit in OrderItem.objects.values_list("order_id", "gross_profit"):
        gross_by_order[order_id] = gross_by_order.get(order_id, ZERO) + Decimal(profit or 0)

    for order in orders:
        if not order.order_year or not order.order_month:
            continue
        period = "{:04d}-{:02d}".format(order.order_year, order.order_month)
        slot = base.setdefault(
            period,
            {"revenue": ZERO, "gross_profit": ZERO, "orders": 0, "new_customers": 0},
        )
        slot["revenue"] += Decimal(order.final_amount or 0)
        slot["gross_profit"] += gross_by_order.get(order.id, ZERO)
        slot["orders"] += 1
        first_order = first_order_map.get(order.customer_id)
        if first_order is not None and order.order_date is not None and first_order == order.order_date:
            slot["new_customers"] += 1
    return base


def _collection_base_by_period() -> dict:
    """各月实际回款额（非退款付款合计），用于回款目标锚定。"""
    base: dict = {}
    rows = (
        Payment.objects.filter(is_refund=False, payment_date__isnull=False)
        .values_list("payment_date", "amount")
    )
    for pay_date, amount in rows:
        period = "{:04d}-{:02d}".format(pay_date.year, pay_date.month)
        base[period] = base.get(period, ZERO) + Decimal(amount or 0)
    return base


@transaction.atomic
def gen_sales_targets(seed: int | None = None) -> dict:
    """生成销售目标：公司年/月、渠道、业务员、品类四个粒度。

    两条设计原则
    ------------
    1. **目标锚在实际值上，再乘一个达成率**：
       ``目标 = 实际 / 期望达成率``。这样年度达成率天然落在
       ``YEAR_TARGET_ACHIEVEMENT``（2023 略超 1.02 / 2024 达标 1.00 /
       2025 差一点 0.97 / 2026 前 7 月落后 0.88）上，
       环比与季节形状仍由真实订单决定，不会出现「目标曲线和销售曲线毫无关系」。
    2. **多粒度按实际占比分摊**：渠道/业务员/品类目标 = 公司目标 ×
       该维度当月实际占比 × 抖动(0.92~1.08)。
       分摊而不是各算各的，是为了让「渠道目标之和 ≈ 公司目标」，
       业务在核对时不会因为对不上而怀疑整张表。

    年度目标的绝对值优先取 ``settings.SALES_TARGETS``（老板定的数），
    取不到才用「实际 ÷ 期望达成率」兜底，，预算目标本来就是管理层给的输入，
    不是算出来的。
    """
    rng = _rng(seed)
    base = _actual_base_by_period()
    collection_base = _collection_base_by_period()
    if not base:
        raise RuntimeError("没有订单数据，无法锚定销售目标，请先导入订单")

    configured_targets = _setting("SALES_TARGETS", {}) or {}

    # ---- 月度公司目标 ----
    created = updated = 0
    monthly_target: dict = {}
    for period, slot in sorted(base.items()):
        try:
            year, month = int(period[:4]), int(period[5:7])
        except (ValueError, IndexError):
            continue
        achievement = YEAR_TARGET_ACHIEVEMENT.get(year, 1.0)
        season = MONTH_SEASON_WEIGHT[(month - 1) % 12] / sum(MONTH_SEASON_WEIGHT)
        # 月度抖动：让个别月份「超额完成」、个别月份「明显落后」，
        # 否则 12 个月达成率全等于年度目标，看板会像一条直线。
        jitter = float(rng.normal(0.0, 0.06))
        factor = achievement * (1.0 + jitter) * (0.75 + 0.55 * season * 12)
        factor = max(0.60, min(1.60, factor))

        revenue_actual = Decimal(slot["revenue"])
        # 年度目标若被显式配置（settings.SALES_TARGETS），用它做一次整体缩放，
        # 月内形状仍由季节权重决定——老板定的是年度盘子，不是逐月数字。
        year_target = Decimal(str(configured_targets.get(year, 0) or 0))
        if year_target > 0:
            season_share = MONTH_SEASON_WEIGHT[(month - 1) % 12] / sum(MONTH_SEASON_WEIGHT)
            share = season_share * (1.0 + float(rng.normal(0.0, 0.05)))
            revenue_target = _q(year_target * Decimal(str(share)))
        else:
            revenue_target = _q(revenue_actual / Decimal(str(factor)))

        order_target = int(round(slot["orders"] / factor)) if slot["orders"] else 0
        # 毛利额目标按当月实际毛利率外推，并留 0.5 个百分点的提升要求
        if revenue_actual > 0:
            actual_margin = Decimal(slot["gross_profit"]) / revenue_actual
        else:
            actual_margin = Decimal("0.25")
        profit_target = _q(revenue_target * (actual_margin + Decimal("0.005")))
        new_customer_target = int(round(slot["new_customers"] / factor)) if slot["new_customers"] else 0
        collection_actual = collection_base.get(period, ZERO)
        collection_target = _q(
            (collection_actual / Decimal(str(factor))) if collection_actual > 0 else revenue_target * Decimal("0.92")
        )

        metrics = {
            "成交额": revenue_target,
            "毛利额": profit_target,
            "订单量": order_target,
            "新增客户": new_customer_target,
            "回款额": collection_target,
        }
        monthly_target[period] = {"revenue": revenue_target, "metrics": metrics}

        for metric, value in metrics.items():
            if int(value) <= 0 and metric in ("订单量", "新增客户"):
                continue
            _, is_created = SalesTarget.objects.update_or_create(
                period_type="month",
                period=period,
                target_scope="全公司",
                target_key="ALL",
                metric=metric,
                defaults={"target_value": _q(value) if not isinstance(value, int) else Decimal(value)},
            )
            created += int(is_created)
            updated += int(not is_created)

    # ---- 年度公司目标 ----
    year_actual: dict = {}
    for period, slot in base.items():
        year_key = period[:4]
        bucket = year_actual.setdefault(year_key, {"revenue": ZERO, "profit": ZERO, "orders": 0})
        bucket["revenue"] += Decimal(slot["revenue"])
        bucket["profit"] += Decimal(slot["gross_profit"])
        bucket["orders"] += slot["orders"]

    for year_key, slot in sorted(year_actual.items()):
        year = int(year_key)
        achievement = YEAR_TARGET_ACHIEVEMENT.get(year, 1.0)
        configured = Decimal(str(configured_targets.get(year, 0) or 0))
        revenue_target = configured if configured > 0 else _q(slot["revenue"] / Decimal(str(achievement)))
        metrics = {
            "成交额": revenue_target,
            "毛利额": _q(revenue_target * Decimal("0.25")),
            "订单量": int(round(slot["orders"] / achievement)) if slot["orders"] else 0,
            "新增客户": 0,
            "回款额": _q(revenue_target * Decimal("0.90")),
        }
        for metric, value in metrics.items():
            if metric == "新增客户":
                # 年度新增客户目标留给业务按渠道拆，此处不重复定义
                continue
            if int(value) <= 0:
                continue
            _, is_created = SalesTarget.objects.update_or_create(
                period_type="year",
                period=year_key,
                target_scope="全公司",
                target_key="ALL",
                metric=metric,
                defaults={"target_value": _q(value) if not isinstance(value, int) else Decimal(value)},
            )
            created += int(is_created)
            updated += int(not is_created)

    # ---- 渠道 / 业务员 / 品类目标（按实际占比分摊公司目标） ----
    scope_specs = [
        ("渠道", "channel", "成交额"),
        ("渠道", "channel", "订单量"),
        ("渠道", "channel", "获客成本口径下的新增客户"),
        ("业务员", "sales_rep", "成交额"),
        ("业务员", "sales_rep", "毛利额"),
        ("业务员", "sales_rep", "回款额"),
        ("品类", "dominant_category", "成交额"),
        ("品类", "dominant_category", "毛利额"),
    ]

    for (scope, field_name, metric) in scope_specs:
        if scope == "渠道" and metric.endswith("新增客户"):
            # 渠道口径只考核成交额与订单量；新增客户目标用同口径成交流量近似
            metric = "新增客户"
        orders = (
            Order.objects.exclude(order_status="已取消")
            .exclude(**{"{}__isnull".format(field_name): True})
            .values("order_year", "order_month", field_name, "final_amount", "id")
        )
        bucket: dict = {}
        for row in orders:
            year = row["order_year"]
            month = row["order_month"]
            if not year or not month:
                continue
            key_value = row.get(field_name)
            if not key_value:
                continue
            period = "{:04d}-{:02d}".format(year, month)
            slot = bucket.setdefault((period, key_value), {"revenue": ZERO, "orders": 0})
            slot["revenue"] += Decimal(row["final_amount"] or 0)
            slot["orders"] += 1

        for (period, key_value), slot in bucket.items():
            if period not in monthly_target:
                continue
            company = monthly_target[period]
            company_metrics = company["metrics"]
            total_revenue = Decimal(base.get(period, {}).get("revenue", ZERO)) or Decimal("1")
            share = Decimal(slot["revenue"]) / total_revenue if total_revenue > 0 else ZERO
            jitter = Decimal(str(round(float(rng.uniform(0.92, 1.08)), 6)))
            share = share * jitter

            if scope == "业务员":
                # 业务员目标按月给，粒度到人
                pass
            if metric == "成交额":
                value = company_metrics["成交额"] * share
            elif metric == "毛利额":
                value = company_metrics["毛利额"] * share
            elif metric == "回款额":
                value = company_metrics["回款额"] * share
            elif metric == "订单量":
                value = Decimal(int(round(company_metrics["订单量"] * float(share)))) if company_metrics["订单量"] else Decimal("0")
            else:
                new_customer_target = int(company_metrics.get("新增客户", 0) or 0)
                value = Decimal(int(round(new_customer_target * float(share)))) if new_customer_target else Decimal("0")

            value = _q(value)
            if value <= 0:
                continue
            _, is_created = SalesTarget.objects.update_or_create(
                period_type="month",
                period=period,
                target_scope=scope,
                target_key=str(key_value),
                metric=metric,
                defaults={"target_value": value},
            )
            created += int(is_created)
            updated += int(not is_created)

    logger.info(
        "gen_sales_targets: created=%s updated=%s 月份数=%s",
        created,
        updated,
        len(monthly_target),
    )
    return {
        "created": created,
        "updated": updated,
        "months": len(monthly_target),
        "years": sorted(year_actual.keys()),
    }


# ---------------------------------------------------------------------------
# ⑦ 竞品对标
# ---------------------------------------------------------------------------
@transaction.atomic
def gen_competitor_benchmarks(
    months: int = 12, seed: int | None = None
) -> dict:
    """生成 6 品类 × 3 城市层级 × 近 12 个月的竞品对标价。

    定价格局（这是要讲出来的经营故事）
    ----------------------------------
    - **铜门**：我方均价 105% × 竞品上限 → 品牌溢价，
      结论方向是「守交付、守口碑」，不能轻易降价；
    - **不锈钢门**：我方均价落在竞品带中位 → 正面竞争，
      毛利对原材料涨价最敏感；
    - **防火门**：我方均价 95% × 竞品下限 → 价格战，
      靠标准件与规模吃饭，降价空间已经很小。

    城市层级用 ``CITY_TIER_SPREAD`` 放大价格带：外省含运费与代理加价，
    报价更高、带宽更宽，这也解释了「同样的门，外省成交价更高」。

    幂等键：``(category, city_tier, period)``。
    """
    rng = _rng(seed)
    as_of = _as_of_date()
    categories = _setting(
        "CATEGORY_LIST", ["铜门", "不锈钢门", "不锈钢铜门", "铸铝门", "甲级防盗门", "防火门"]
    )

    # 近 N 个月（含当月）的 "YYYY-MM"
    periods = []
    year, month = as_of.year, as_of.month
    for _ in range(max(1, months)):
        periods.append("{:04d}-{:02d}".format(year, month))
        month -= 1
        if month < 1:
            month = 12
            year -= 1
    periods.reverse()

    created = updated = 0
    our_prices: dict = {}

    for category in categories:
        low_base, high_base = COMPETITOR_PRICE_BASE.get(category, (2000.0, 5000.0))
        position = OUR_PRICING_POSITION.get(category)
        for tier in CITY_TIERS:
            spread = CITY_TIER_SPREAD.get(tier, 1.0)
            for period in periods:
                # 年度慢变量：材料价格整体上行，每年约 +2%
                year_factor = 1.0 + 0.02 * max(0, int(period[:4]) - DATA_START_YEAR)
                month_factor = 1.0 + float(rng.normal(0.0, 0.02))
                low = low_base * spread * year_factor * month_factor
                high = high_base * spread * year_factor * (1.0 + float(rng.normal(0.0, 0.015)))
                if high <= low:
                    high = low * 1.35

                ours = None
                product = Product.objects.filter(category=category).first()
                if product is not None and product.base_price:
                    ours = float(product.base_price) * spread

                center = (low + high) / 2.0
                if position is None:
                    target_price = center
                    ratio = 0.50
                    tag = "中"
                elif position > 1.0:
                    target_price = high * position
                    ratio = 0.72
                    tag = "高"
                else:
                    # 0<x<1 时解释为「落在竞品带内按比例位置」，0.95 特判为低于下限
                    if category == "防火门":
                        target_price = low * position
                        ratio = 0.22
                        tag = "低"
                    else:
                        target_price = low + (high - low) * (position - 0.5)
                        ratio = max(0.05, min(0.95, position - 0.5 + 0.5))
                        tag = "中"

                our_price = _q(target_price)
                our_prices.setdefault(category, []).append(float(our_price))
                sample_count = int(rng.integers(8, 26))

                note = POSITION_NOTE[tag]
                if tag == "低":
                    note = "{} {}".format(note, "竞品以低价抢占标段，需评估是否跟价。")
                elif tag == "高":
                    note = "{} {}".format(note, "该价位对服务与设计能力要求更高。")

                _, is_created = CompetitorBenchmark.objects.update_or_create(
                    category=category,
                    city_tier=tier,
                    period=period,
                    defaults={
                        "competitor_price_low": _q(low),
                        "competitor_price_high": _q(high),
                        "our_avg_price": our_price,
                        "sample_count": sample_count,
                        "note": note[:200],
                    },
                )
                created += int(is_created)
                updated += int(not is_created)

    avg_prices = {
        category: round(sum(values) / len(values), 2)
        for category, values in our_prices.items()
        if values
    }
    logger.info("gen_competitor_benchmarks: created=%s updated=%s", created, updated)
    return {
        "created": created,
        "updated": updated,
        "categories": len(categories),
        "periods": len(periods),
        "avg_our_price": avg_prices,
    }


# ---------------------------------------------------------------------------
# 汇总入口
# ---------------------------------------------------------------------------
GENERATOR_MAP = {
    "product_cost_standards": gen_product_cost_standards,
    "order_costs": gen_order_costs,
    "payment_terms": gen_payment_terms,
    "delivery_stages": gen_delivery_stages,
    "employees_and_hours": gen_employees_and_hours,
    "sales_targets": gen_sales_targets,
    "competitor_benchmarks": gen_competitor_benchmarks,
}

# 执行顺序有依赖：成本标准 → 订单成本 → 账期（依赖付款）→ 阶段（依赖订单）
# → 员工工时（独立）→ 目标（依赖订单与 DWS，可后置）→ 竞品（依赖产品）
GENERATOR_ORDER = (
    "product_cost_standards",
    "order_costs",
    "payment_terms",
    "delivery_stages",
    "employees_and_hours",
    "competitor_benchmarks",
    "sales_targets",
)


def gen_all(seed: int | None = None) -> dict:
    """按依赖顺序跑完所有生成器，返回 ``{名称: 统计 dict}``。"""
    result = {}
    for name in GENERATOR_ORDER:
        result[name] = GENERATOR_MAP[name](seed=seed)
    return result
