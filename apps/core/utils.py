"""通用计算与格式化工具。

刻意不依赖 Django：这些函数是「经营分析口径」的载体，需要能被脚本、
Notebook、离线跑批任务直接复用，也要能在不启动 settings 的纯单元测试里
快速验证。任何与 ORM、请求上下文有关的逻辑都不应出现在这里。

数值口径约定：
- 金额一律用 ``Decimal`` 参与运算，避免 float 在累加大量订单后出现分位误差；
- 除法统一走 :func:`safe_div`，把「分母为 0」变成可预期的返回值而不是异常，
  因为经营报表里 0 是常态（新门店、新品类、去年同期无数据）。
"""

from calendar import monthrange
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

__all__ = [
    "DECIMAL_ZERO",
    "to_decimal",
    "safe_div",
    "growth_rate",
    "qoq",
    "yoy",
    "period_range",
    "fmt_money",
    "fmt_pct",
]

DECIMAL_ZERO = Decimal("0")


def to_decimal(value, default=None):
    """把任意数值/字符串安全转成 ``Decimal``。

    用于处理 pandas / Excel 导入进来的脏数据（''、'--'、'1,280.00'、None）。
    转换失败时返回 ``default``，而不是抛异常中断整批导入。
    """
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        # bool 是 int 的子类，显式拒绝以免把 True 静默算成 1 元
        return default
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return default
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("，", "").replace("￥", "")
        if not cleaned:
            return default
        try:
            return Decimal(cleaned)
        except (InvalidOperation, ValueError):
            return default
    return default


def safe_div(numerator, denominator, default=None):
    """安全除法：分母为 0 / None 或任一操作数不可解析时返回 ``default``。

    默认返回 ``None`` 而非 0，是为了让上层能区分「真的是 0」与
    「无法计算」，前者可以画进趋势图，后者应显示为「，」。
    需要拿到 0 的地方显式传 ``default=DECIMAL_ZERO``。
    """
    left = to_decimal(numerator)
    right = to_decimal(denominator)
    if left is None or right is None or right == 0:
        return default
    return left / right


def growth_rate(current, previous, default=None, as_pct=False):
    """通用增长率：``(current - previous) / previous``。

    ``previous`` 为 0 或负数时直接返回 ``default``：从 0 增长的百分比在业务上
    无意义，硬算出来的「+∞%」会污染图表和排行榜。
    ``as_pct=True`` 时返回百分数（乘 100）。
    """
    cur = to_decimal(current)
    prev = to_decimal(previous)
    if cur is None or prev is None or prev <= 0:
        return default
    rate = (cur - prev) / prev
    if as_pct:
        rate = rate * Decimal("100")
    return rate


def qoq(current, previous_quarter, default=None, as_pct=True):
    """环比：本期 vs 上一期（相邻季度或相邻月份）。

    单独包一层而不是直接调 ``growth_rate``，是为了让调用点语义清晰
    （``qoq(m_07, m_06)`` 比 ``growth_rate(m_07, m_06)`` 更难写错参数顺序）。
    """
    return growth_rate(current, previous_quarter, default=default, as_pct=as_pct)


def yoy(current, same_period_last_year, default=None, as_pct=True):
    """同比：本期 vs 去年同期。

    同比能剔除门业明显的季节性（装修旺季集中在春秋两季），
    因此经营看板默认展示同比，环比只作为辅助指标。
    """
    return growth_rate(current, same_period_last_year, default=default, as_pct=as_pct)


def period_range(year, month):
    """返回指定年月的起止日期 ``(first_day, last_day)``，闭区间。

    用闭区间而不是半开区间，是因为业务同事看报表时习惯「3 月 1 日到 3 月 31 日」；
    转成 ORM 查询时由调用方写成 ``__range`` 或 ``>= first, <= last``。
    """
    year = int(year)
    month = int(month)
    if not 1 <= month <= 12:
        raise ValueError("month 必须在 1-12 之间，实际收到：{}".format(month))
    last_day = monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def _quantize(value, places):
    """按指定位数四舍五入，统一用 ROUND_HALF_UP 贴合财务习惯。"""
    exponent = Decimal(1).scaleb(-places)
    return value.quantize(exponent, rounding=ROUND_HALF_UP)


def fmt_money(value, places=2, unit="", thousands=True, default="-"):
    """金额格式化：千分位 + 固定小数位。

    空值返回 ``default``（默认一个短横线），让报表里的空缺一眼可辨，
    而不是显示成 0.00 被误读为「本期没有收入」。
    """
    number = to_decimal(value)
    if number is None:
        return default
    number = _quantize(number, places)
    if thousands:
        text = "{:,.{}f}".format(number, places)
    else:
        text = "{:.{}f}".format(number, places)
    return "{}{}".format(text, unit) if unit else text


def fmt_pct(value, places=1, already_pct=False, with_sign=False, default="-"):
    """百分比格式化。

    ``already_pct`` 用来区分入参是比率（0.1234）还是已经乘过 100 的百分数（12.34），
    这两种数据在项目里都会出现（数据库存比率、SQL 聚合有时直接返回百分数），
    由调用方显式声明，避免出现「1234.0%」这种事故。
    """
    number = to_decimal(value)
    if number is None:
        return default
    pct = number if already_pct else number * Decimal("100")
    pct = _quantize(pct, places)
    if with_sign and pct > 0:
        return "+{:.{}f}%".format(pct, places)
    return "{:.{}f}%".format(pct, places)
