# -*- coding: utf-8 -*-
"""
业务枚举常量（Single Source of Truth）
======================================
所有下拉选项、模型 choices、前端筛选项、指标分组都从这里取，
杜绝「同一个品类在三个文件里写三遍、改一处漏两处」。

命名约定：
    XxxChoices  英文键 -> 中文显示值，用于 Django choices 参数
    xxx_LIST    只要中文值列表，用于前端筛选项 / 校验
"""
from django.db import models


# ---------------------------------------------------------------------------
# 产品与品类
# ---------------------------------------------------------------------------
class CategoryChoices(models.TextChoices):
    COPPER = "铜门", "铜门"
    STAINLESS = "不锈钢门", "不锈钢门"
    STAINLESS_COPPER = "不锈钢铜门", "不锈钢铜门"
    CAST_ALUMINUM = "铸铝门", "铸铝门"
    SECURITY = "甲级防盗门", "甲级防盗门"
    FIREPROOF = "防火门", "防火门"


CATEGORY_LIST = [c.value for c in CategoryChoices]

# 品类分组：区分「做利润」与「跑量」，用于毛利结构分析
CATEGORY_PROFIT_SEGMENT = {
    "铜门": "利润型",
    "铸铝门": "利润型",
    "不锈钢铜门": "利润型",
    "不锈钢门": "走量型",
    "甲级防盗门": "走量型",
    "防火门": "工程型",
}


# ---------------------------------------------------------------------------
# 客户
# ---------------------------------------------------------------------------
class CustomerTypeChoices(models.TextChoices):
    PERSONAL = "个人家装", "个人家装"
    DECORATION = "装修公司", "装修公司"
    ENGINEERING = "工程客户", "工程客户"


CUSTOMER_TYPE_LIST = [c.value for c in CustomerTypeChoices]


class ChannelChoices(models.TextChoices):
    DOUYIN = "抖音短视频", "抖音短视频"
    REFERRAL = "老客户转介", "老客户转介"
    WALK_IN = "门店到访", "门店到访"
    ONLINE = "线上渠道", "线上渠道"
    EXHIBITION = "展会营销", "展会营销"
    DECORATION_COOP = "装修公司合作", "装修公司合作"


CHANNEL_LIST = [c.value for c in ChannelChoices]

# 渠道获客成本假设（元/线索）——What-if 模拟器的弹性参数来源。
# 说明：抖音/展会为付费获客（内容制作 + 参展），转介/到访为关系型获客，
# 成本体现在「转介绍激励」而非广告费。
CHANNEL_ACQUISITION_COST = {
    "抖音短视频": 180.0,
    "老客户转介": 260.0,     # 转介绍激励（返现/礼品）
    "门店到访": 90.0,
    "线上渠道": 150.0,
    "展会营销": 620.0,
    "装修公司合作": 300.0,
}


class MemberLevelChoices(models.TextChoices):
    NORMAL = "普通", "普通"
    SILVER = "银卡", "银卡"
    GOLD = "金卡", "金卡"
    DIAMOND = "钻石", "钻石"


MEMBER_LEVEL_LIST = [c.value for c in MemberLevelChoices]


class SegmentChoices(models.TextChoices):
    HIGH_VALUE = "高价值客户", "高价值客户"
    GROWING = "成长潜力", "成长潜力"
    AT_RISK = "需挽留", "需挽留"
    NEW = "新客户", "新客户"
    DORMANT = "沉睡客户", "沉睡客户"


SEGMENT_LIST = [c.value for c in SegmentChoices]


# ---------------------------------------------------------------------------
# 订单
# ---------------------------------------------------------------------------
class OrderTypeChoices(models.TextChoices):
    RETAIL = "零售", "零售"
    DECORATION = "装企合作", "装企合作"
    ENGINEERING = "工程", "工程"


ORDER_TYPE_LIST = [c.value for c in OrderTypeChoices]


class OrderStatusChoices(models.TextChoices):
    DONE = "已完成", "已完成"
    PRODUCING = "生产中", "生产中"
    PENDING_SHIP = "待发货", "待发货"
    CANCELLED = "已取消", "已取消"


ORDER_STATUS_LIST = [c.value for c in OrderStatusChoices]

# 有效订单（计入营收）的状态口径：除「已取消」外全部计入
VALID_ORDER_EXCLUDE_STATUS = ["已取消"]


class DeliveryTypeChoices(models.TextChoices):
    INSTALLED = "包安装", "包安装"
    SELF_PICKUP = "自提", "自提"


DELIVERY_TYPE_LIST = [c.value for c in DeliveryTypeChoices]


# ---------------------------------------------------------------------------
# 付款与回款
# ---------------------------------------------------------------------------
class PaymentStageChoices(models.TextChoices):
    DEPOSIT = "定金", "定金"
    INTERIM = "中期款", "中期款"
    FINAL = "尾款", "尾款"


PAYMENT_STAGE_LIST = [c.value for c in PaymentStageChoices]


class PaymentMethodChoices(models.TextChoices):
    WECHAT = "微信支付", "微信支付"
    ALIPAY = "支付宝", "支付宝"
    POS = "POS刷卡", "POS刷卡"
    BANK = "银行转账", "银行转账"
    CORPORATE = "对公转账", "对公转账"


PAYMENT_METHOD_LIST = [c.value for c in PaymentMethodChoices]


class AgingBucketChoices(models.TextChoices):
    B0_30 = "0-30天", "0-30天"
    B31_60 = "31-60天", "31-60天"
    B61_90 = "61-90天", "61-90天"
    B90_PLUS = "90天以上", "90天以上"


AGING_BUCKET_LIST = [c.value for c in AgingBucketChoices]

# 账龄分桶边界（天），左闭右开
AGING_BUCKET_BOUNDS = [(0, 30), (31, 60), (61, 90), (91, 10 ** 6)]


def aging_bucket(days_overdue):
    """逾期天数 -> 账龄分桶（供 ETL 与测试共用，避免口径分叉）。"""
    if days_overdue is None or days_overdue <= 0:
        return AgingBucketChoices.B0_30.value
    if days_overdue <= 30:
        return AgingBucketChoices.B0_30.value
    if days_overdue <= 60:
        return AgingBucketChoices.B31_60.value
    if days_overdue <= 90:
        return AgingBucketChoices.B61_90.value
    return AgingBucketChoices.B90_PLUS.value


# ---------------------------------------------------------------------------
# 成本与毛利
# ---------------------------------------------------------------------------
class CostTypeChoices(models.TextChoices):
    MATERIAL = "材料成本", "材料成本"
    LABOR = "人工成本", "人工成本"
    MANUFACTURING = "制造费用", "制造费用"
    LOGISTICS = "物流成本", "物流成本"
    INSTALLATION = "安装成本", "安装成本"
    AFTER_SALES = "售后成本", "售后成本"


COST_TYPE_LIST = [c.value for c in CostTypeChoices]

# 计入「毛利」的成本类型：材料 + 人工 + 制造费用 + 物流 + 安装
# 售后成本不算毛利（属期间性质量成本），单独做质量成本分析。
GROSS_PROFIT_COST_TYPES = [
    CostTypeChoices.MATERIAL.value,
    CostTypeChoices.LABOR.value,
    CostTypeChoices.MANUFACTURING.value,
    CostTypeChoices.LOGISTICS.value,
    CostTypeChoices.INSTALLATION.value,
]

# 成本结构默认占比（用于缺明细时的拆解基准；ETL 与模拟器共用）
COST_STRUCTURE_RATIO = {
    CostTypeChoices.MATERIAL.value: 0.52,
    CostTypeChoices.LABOR.value: 0.16,
    CostTypeChoices.MANUFACTURING.value: 0.11,
    CostTypeChoices.LOGISTICS.value: 0.05,
    CostTypeChoices.INSTALLATION.value: 0.04,
    CostTypeChoices.AFTER_SALES.value: 0.02,
}

# 各品类目标毛利率（用于模拟器与毛利健康度判断）
CATEGORY_TARGET_MARGIN = {
    "铜门": 0.34,
    "铸铝门": 0.32,
    "不锈钢铜门": 0.30,
    "不锈钢门": 0.24,
    "甲级防盗门": 0.22,
    "防火门": 0.18,
}


# ---------------------------------------------------------------------------
# 售后
# ---------------------------------------------------------------------------
class AfterSalesTypeChoices(models.TextChoices):
    INSTALL = "安装问题", "安装问题"
    SIZE_ERROR = "尺寸误差", "尺寸误差"
    COLOR_DIFF = "色差", "色差"
    HARDWARE = "五金损坏", "五金损坏"
    DEFORMATION = "门体变形", "门体变形"
    LOGISTICS_DAMAGE = "物流破损", "物流破损"
    CONSULT = "售后咨询", "售后咨询"
    REFUND = "退款", "退款"


AFTER_SALES_TYPE_LIST = [c.value for c in AfterSalesTypeChoices]

# 退款类记录不计入「售后率」分子（它不是质量事件，是资金事件）
AFTER_SALES_QUALITY_TYPES = [
    t for t in AFTER_SALES_TYPE_LIST if t != AfterSalesTypeChoices.REFUND.value
]


class AfterSalesStatusChoices(models.TextChoices):
    DONE = "已处理", "已处理"
    PROCESSING = "处理中", "处理中"


# ---------------------------------------------------------------------------
# 交付履约
# ---------------------------------------------------------------------------
class DeliveryStageChoices(models.TextChoices):
    ORDER_CONFIRM = "接单确认", "接单确认"
    DRAWING_CONFIRM = "图纸确认", "图纸确认"
    SCHEDULING = "排产", "排产"
    PRODUCTION = "生产", "生产"
    QC = "质检", "质检"
    LOGISTICS = "物流", "物流"
    INSTALL = "安装", "安装"
    DONE = "交付完成", "交付完成"


DELIVERY_STAGE_LIST = [c.value for c in DeliveryStageChoices]

# 交付阶段顺序（甘特图与瓶颈分析依赖此顺序，不可打乱）
DELIVERY_STAGE_SEQUENCE = DELIVERY_STAGE_LIST


class DelayReasonChoices(models.TextChoices):
    CUSTOMER_CHANGE = "客户变更", "客户变更"
    MATERIAL_SHORTAGE = "缺料", "缺料"
    CAPACITY = "产能不足", "产能不足"
    LOGISTICS = "物流延误", "物流延误"
    INSTALL = "安装排期", "安装排期"
    QUALITY_REWORK = "质量返工", "质量返工"
    NONE = "未延期", "未延期"


DELAY_REASON_LIST = [r.value for r in DelayReasonChoices]


# ---------------------------------------------------------------------------
# 销售目标
# ---------------------------------------------------------------------------
class TargetScopeChoices(models.TextChoices):
    COMPANY = "全公司", "全公司"
    CHANNEL = "渠道", "渠道"
    STORE = "门店", "门店"
    REP = "业务员", "业务员"
    CATEGORY = "品类", "品类"


TARGET_SCOPE_LIST = [s.value for s in TargetScopeChoices]


class TargetMetricChoices(models.TextChoices):
    REVENUE = "成交额", "成交额"
    GROSS_PROFIT = "毛利额", "毛利额"
    ORDER_COUNT = "订单量", "订单量"
    NEW_CUSTOMER = "新增客户", "新增客户"
    COLLECTION = "回款额", "回款额"


TARGET_METRIC_LIST = [m.value for m in TargetMetricChoices]


# ---------------------------------------------------------------------------
# 员工 / 人效
# ---------------------------------------------------------------------------
class DepartmentChoices(models.TextChoices):
    SALES = "销售部", "销售部"
    PRODUCTION = "生产部", "生产部"
    INSTALL = "安装部", "安装部"
    AFTER_SALES = "售后部", "售后部"
    LOGISTICS = "物流部", "物流部"
    MANAGEMENT = "综合管理", "综合管理"


DEPARTMENT_LIST = [d.value for d in DepartmentChoices]


class WorkTypeChoices(models.TextChoices):
    PRODUCTION = "生产工时", "生产工时"
    INSTALL = "安装工时", "安装工时"
    SALES = "销售工时", "销售工时"
    AFTER_SALES = "售后工时", "售后工时"


WORK_TYPE_LIST = [w.value for w in WorkTypeChoices]


# ---------------------------------------------------------------------------
# 数据质量
# ---------------------------------------------------------------------------
class DQSeverityChoices(models.TextChoices):
    ERROR = "error", "错误（阻断）"
    WARNING = "warning", "警告（提示）"
    INFO = "info", "提示"


class DQStatusChoices(models.TextChoices):
    PASS = "pass", "通过"
    FAIL = "fail", "失败"
    WARN = "warn", "警告"


# ---------------------------------------------------------------------------
# 指标口径
# ---------------------------------------------------------------------------
class MetricTypeChoices(models.TextChoices):
    ADDITIVE = "additive", "可加（直接求和）"
    RATIO = "ratio", "比率（需分子分母）"
    DERIVED = "derived", "派生（公式计算）"


# 指标口径编码规则：M_<域>_<序号>，域缩写见下
METRIC_DOMAIN_PREFIX = {
    "经营总览": "OV",
    "增长渠道": "GR",
    "品类利润": "PF",
    "客户价值": "CU",
    "交付售后": "DL",
    "回款现金": "CF",
    "人效门店": "WF",
}


# ===========================================================================
# 兼容别名（给页面层与筛选层用）
# ---------------------------------------------------------------------------
# 背景：本文件的枚举以「中文值直接入库」为原则（见 models.py），
# 而页面层需要 (值, 显示名) 二元组来渲染下拉框。
# 这里提供三个别名族，避免每个调用方各写一份：
#
#   xxx_CHOICES   二元组列表，值 = 中文（页面提交后筛选器认识它）；
#                 同时可被 dict() 直接反查，例如 dict(AGING_BUCKET_CHOICES)["0-30天"]。
#   xxx_VALUES    纯值列表，供白名单校验（筛选器用它过滤非法入参）。
#
# 为什么不写英文 key：一旦下拉框提交英文 key，而 QuerySet 过滤用的是中文值，
# 会出现「筛选条件看着生效、实际全被丢弃」的静默失效 —— 这类问题极难排查。
# ===========================================================================
CATEGORY_CHOICES = [(c.value, c.label) for c in CategoryChoices]
CATEGORY_VALUES = CATEGORY_LIST

CHANNEL_CHOICES = [(c.value, c.label) for c in ChannelChoices]
CHANNEL_VALUES = CHANNEL_LIST

CUSTOMER_TYPE_CHOICES = [(c.value, c.label) for c in CustomerTypeChoices]
CUSTOMER_TYPE_VALUES = CUSTOMER_TYPE_LIST

ORDER_TYPE_CHOICES = [(c.value, c.label) for c in OrderTypeChoices]
ORDER_TYPE_VALUES = ORDER_TYPE_LIST

ORDER_STATUS_CHOICES = [(c.value, c.label) for c in OrderStatusChoices]
ORDER_STATUS_VALUES = ORDER_STATUS_LIST

SEGMENT_CHOICES = [(c.value, c.label) for c in SegmentChoices]
SEGMENT_VALUES = SEGMENT_LIST

AGING_BUCKET_CHOICES = [(c.value, c.label) for c in AgingBucketChoices]
AGING_BUCKET_VALUES = AGING_BUCKET_LIST

DELIVERY_TYPE_CHOICES = [(c.value, c.label) for c in DeliveryTypeChoices]
DELIVERY_TYPE_VALUES = DELIVERY_TYPE_LIST

PAYMENT_STAGE_CHOICES = [(c.value, c.label) for c in PaymentStageChoices]
PAYMENT_METHOD_CHOICES = [(c.value, c.label) for c in PaymentMethodChoices]
COST_TYPE_CHOICES = [(c.value, c.label) for c in CostTypeChoices]
AFTER_SALES_TYPE_CHOICES = [(c.value, c.label) for c in AfterSalesTypeChoices]
DELIVERY_STAGE_CHOICES = [(c.value, c.label) for c in DeliveryStageChoices]
DELAY_REASON_CHOICES = [(c.value if hasattr(c, "value") else c, c.label if hasattr(c, "label") else c)
                        for c in DelayReasonChoices]
MEMBER_LEVEL_CHOICES = [(c.value, c.label) for c in MemberLevelChoices]
DEPARTMENT_CHOICES = [(c.value, c.label) for c in DepartmentChoices]
WORK_TYPE_CHOICES = [(c.value, c.label) for c in WorkTypeChoices]
TARGET_METRIC_CHOICES = [(c.value, c.label) for c in TargetMetricChoices]
TARGET_SCOPE_CHOICES = [(c.value, c.label) for c in TargetScopeChoices]
