# -*- coding: utf-8 -*-
"""
分析层模型（analytics）
=======================
分三类，用途完全不同，不要混用：

  [1] 元数据：MetricDefinition / MetricTree / DataVersion
      ，， 把「口径」和「数据版本」变成可查询的对象。
         指标卡 hover 显示计算口径、缓存按数据版本自动失效，都靠它们。

  [2] 预聚合表（dws_*，只读）：Dashboard 直接查这些表，
      ，， 避免每次筛选取数都去扫 fact_order（2401 行还不痛，
         但真实数据 500 万行时就是几百毫秒与几秒的差别）。

  [3] 模型注册与评分：ModelVersion / ScoringResult
      ，， 让 XGBoost 从 notebook 走到线上：可查版本、可查指标、可查单订单解释。
"""
from django.db import models

from apps.core import constants as C
from apps.core.base import BaseModel, ReadOnlyModel


# ===========================================================================
# [1] 元数据
# ===========================================================================
class MetricDefinition(BaseModel):
    """
    指标口径字典（把 docs/metrics_spec.md 变成可查询的表）。

    面试场景：面试官问「你的成交额怎么算的？」
    回答不应该是「我脚本里写的」，而应该是「指标编码 M_OV_001，口径是……」
    并在界面上直接点开看到。这张表就是那个答案。
    """

    code = models.CharField("指标编码", max_length=24, unique=True,
                            help_text="规则 M_<域>_<序号>，如 M_OV_001")
    name = models.CharField("指标名称", max_length=32)
    domain = models.CharField("所属域", max_length=16, db_index=True)
    metric_type = models.CharField("指标类型", max_length=16, choices=C.MetricTypeChoices.choices,
                                   default=C.MetricTypeChoices.DERIVED)

    definition = models.TextField("业务定义", help_text="用业务语言说明这个指标衡量什么")
    formula = models.TextField("计算公式", help_text="可直接照着复算的公式")
    numerator_desc = models.CharField("分子", max_length=128, blank=True)
    denominator_desc = models.CharField("分母", max_length=128, blank=True)

    unit = models.CharField("单位", max_length=8, blank=True, help_text="元 / 单 / % / 天")
    source_tables = models.CharField("数据来源表", max_length=200, blank=True)
    caveat = models.TextField("口径注意事项", blank=True,
                              help_text="易被误读的地方，例如「取消订单不计入」")
    version = models.CharField("口径版本", max_length=8, default="v1")
    owner = models.CharField("口径负责人", max_length=16, default="数据分析")

    order = models.PositiveSmallIntegerField("排序", default=0)

    class Meta:
        db_table = "dim_metric_definition"
        verbose_name = "指标口径"
        verbose_name_plural = "指标口径"
        ordering = ["domain", "order", "code"]

    def __str__(self):
        return f"{self.code} {self.name}"

    @property
    def badge(self):
        """界面上展示的短标签。"""
        return f"{self.code} · {self.version}"


class MetricTree(BaseModel):
    """
    指标拆解树（北极星 → 一级 → 二级 → 动作指标）。

    例：成交额 ← 订单量 × 客单价 ← 新客数 × 复购率 × 客单价
    """

    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True,
                               related_name="children", verbose_name="上级指标")
    metric = models.ForeignKey(MetricDefinition, on_delete=models.CASCADE, related_name="tree_nodes",
                               verbose_name="指标")
    relation_type = models.CharField("关系类型", max_length=16, default="multiply",
                                     help_text="multiply 相乘 / add 相加 / driver 驱动")
    weight = models.DecimalField("权重", max_digits=6, decimal_places=3, default=1)
    note = models.CharField("说明", max_length=128, blank=True)

    class Meta:
        db_table = "dim_metric_tree"
        verbose_name = "指标拆解"
        verbose_name_plural = "指标拆解"
        ordering = ["parent_id", "id"]

    def __str__(self):
        parent = self.parent.metric.name if self.parent else "根"
        return f"{parent} → {self.metric.name}"


class DataVersion(BaseModel):
    """
    数据版本登记（缓存一致性的关键）。

    每次 ETL 重建后更新一行：表名 + 行数 + 最大时间戳 + 构建时间。
    分析接口的缓存 key 里带上版本号 → 数据一变，缓存自动失效，
    彻底避免「改了数但页面还是旧值」这类最难排查的问题。
    """

    table_name = models.CharField("表名", max_length=64, unique=True)
    layer = models.CharField("数据层", max_length=8, default="ods",
                             help_text="ods 明细层 / dws 预聚合层")
    row_count = models.BigIntegerField("行数", default=0)
    max_event_date = models.DateField("数据最大日期", null=True, blank=True)
    version_token = models.CharField("版本令牌", max_length=32, blank=True,
                                     help_text="重建时生成的指纹，缓存 key 用它")
    built_at = models.DateTimeField("构建时间", auto_now=True)
    build_seconds = models.DecimalField("构建耗时（秒）", max_digits=8, decimal_places=2, default=0)

    class Meta:
        db_table = "dim_data_version"
        verbose_name = "数据版本"
        verbose_name_plural = "数据版本"
        ordering = ["layer", "table_name"]

    def __str__(self):
        return f"{self.table_name} @{self.built_at:%Y-%m-%d %H:%M}"


# ===========================================================================
# [2] 预聚合表（只读，由 manage.py build_dws 重建）
# ===========================================================================
class DwsBusinessMonthly(ReadOnlyModel):
    """
    月度经营指标预聚合：年 × 月 × 渠道 × 品类。

    这是首页与增长页最主要的取数表 ，， 一次查询出完 KPI + 趋势，
    不用把 2401 行订单拉进 Python 再 groupby。
    """

    period = models.CharField("期间", max_length=7, db_index=True)
    year = models.PositiveSmallIntegerField("年", db_index=True)
    month = models.PositiveSmallIntegerField("月", db_index=True)
    channel = models.CharField("渠道", max_length=16, db_index=True)
    category = models.CharField("品类", max_length=16, db_index=True)

    order_count = models.PositiveIntegerField("订单数", default=0)
    customer_count = models.PositiveIntegerField("客户数", default=0)
    new_customer_count = models.PositiveIntegerField("新客数", default=0)
    revenue = models.DecimalField("成交额", max_digits=16, decimal_places=2, default=0)
    gross_profit = models.DecimalField("毛利额", max_digits=16, decimal_places=2, default=0)
    total_cost = models.DecimalField("总成本", max_digits=16, decimal_places=2, default=0)
    discount_amount = models.DecimalField("让利金额", max_digits=16, decimal_places=2, default=0)

    avg_order_value = models.DecimalField("客单价", max_digits=14, decimal_places=2, default=0)
    gross_margin = models.DecimalField("毛利率", max_digits=8, decimal_places=4, default=0)

    class Meta:
        db_table = "dws_business_monthly"
        verbose_name = "月度经营预聚合"
        verbose_name_plural = "月度经营预聚合"
        unique_together = [("period", "channel", "category")]
        indexes = [
            models.Index(fields=["year", "channel"], name="idx_dws_biz_year_ch"),
            models.Index(fields=["period", "category"], name="idx_dws_biz_p_cat"),
        ]


class DwsCustomerCohort(ReadOnlyModel):
    """
    客户留存 Cohort 矩阵：首单月份 × 后续第 N 月。

    定制门业是低频高客单生意，留存比 GMV 更有经营含义：
    「首单后第 12 个月还在复购的客户占多少」直接决定要不要做老客运营。
    """

    cohort_month = models.CharField("首单月份", max_length=7, db_index=True)
    month_index = models.PositiveSmallIntegerField("第 N 月", db_index=True,
                                                   help_text="0 = 首单当月")

    customer_count = models.PositiveIntegerField("队列客户数", default=0)
    active_count = models.PositiveIntegerField("当月在购客户数", default=0)
    retention_rate = models.DecimalField("留存率", max_digits=8, decimal_places=4, default=0)
    revenue = models.DecimalField("当月成交额", max_digits=16, decimal_places=2, default=0)

    class Meta:
        db_table = "dws_customer_cohort"
        verbose_name = "客户留存队列"
        verbose_name_plural = "客户留存队列"
        unique_together = [("cohort_month", "month_index")]
        ordering = ["cohort_month", "month_index"]


class DwsRepurchaseInterval(ReadOnlyModel):
    """复购间隔分布：回答「客户平均多久回来一次」，用于流失判定阈值。"""

    category = models.CharField("品类", max_length=16, blank=True, db_index=True)
    channel = models.CharField("渠道", max_length=16, blank=True, db_index=True)

    interval_days = models.PositiveIntegerField("复购间隔（天）", db_index=True)
    customer_count = models.PositiveIntegerField("客户数", default=0)

    class Meta:
        db_table = "dws_repurchase_interval"
        verbose_name = "复购间隔分布"
        verbose_name_plural = "复购间隔分布"
        unique_together = [("category", "channel", "interval_days")]


class DwsCategoryMonthly(ReadOnlyModel):
    """品类月度利润结构：SKU 级毛利的月度汇总，支撑品类定价与取舍。"""

    period = models.CharField("期间", max_length=7, db_index=True)
    category = models.CharField("品类", max_length=16, db_index=True)
    product_code = models.CharField("产品编码", max_length=16, blank=True)

    qty = models.PositiveIntegerField("销量（樘）", default=0)
    revenue = models.DecimalField("收入", max_digits=16, decimal_places=2, default=0)
    material_cost = models.DecimalField("材料成本", max_digits=16, decimal_places=2, default=0)
    labor_cost = models.DecimalField("人工成本", max_digits=16, decimal_places=2, default=0)
    manufacturing_cost = models.DecimalField("制造费用", max_digits=16, decimal_places=2, default=0)
    logistics_cost = models.DecimalField("物流成本", max_digits=16, decimal_places=2, default=0)
    installation_cost = models.DecimalField("安装成本", max_digits=16, decimal_places=2, default=0)
    gross_profit = models.DecimalField("毛利", max_digits=16, decimal_places=2, default=0)
    gross_margin = models.DecimalField("毛利率", max_digits=8, decimal_places=4, default=0)
    avg_discount = models.DecimalField("平均折扣率", max_digits=6, decimal_places=4, default=0)

    class Meta:
        db_table = "dws_category_monthly"
        verbose_name = "品类月度利润"
        verbose_name_plural = "品类月度利润"
        unique_together = [("period", "category", "product_code")]
        indexes = [models.Index(fields=["period", "gross_margin"], name="idx_dws_cat_margin")]


class DwsDeliveryStage(ReadOnlyModel):
    """
    交付阶段耗时预聚合：瓶颈分析的取数表。

    bottleneck_score = 阶段平均耗时占比 × 该阶段超时率，
    用来回答「超期的时间到底卡在哪个环节」。
    """

    year = models.PositiveSmallIntegerField("年", db_index=True)
    category = models.CharField("品类", max_length=16, db_index=True)
    stage = models.CharField("阶段", max_length=16, db_index=True)
    stage_order = models.PositiveSmallIntegerField("阶段序号", default=0)

    order_count = models.PositiveIntegerField("订单数", default=0)
    avg_duration_days = models.DecimalField("平均耗时（天）", max_digits=10, decimal_places=2, default=0)
    median_duration_days = models.DecimalField("中位耗时（天）", max_digits=10, decimal_places=2, default=0)
    delay_count = models.PositiveIntegerField("超时订单数", default=0)
    delay_rate = models.DecimalField("超时率", max_digits=8, decimal_places=4, default=0)
    avg_delay_days = models.DecimalField("平均超时天数", max_digits=10, decimal_places=2, default=0)
    bottleneck_score = models.DecimalField("瓶颈指数", max_digits=10, decimal_places=4, default=0)

    class Meta:
        db_table = "dws_delivery_stage"
        verbose_name = "交付阶段预聚合"
        verbose_name_plural = "交付阶段预聚合"
        unique_together = [("year", "category", "stage")]
        ordering = ["year", "category", "stage_order"]


class DwsReceivableAging(ReadOnlyModel):
    """
    应收账款账龄快照（as_of_date 口径）。

    「谁欠我钱、欠了多久、还能不能收回来」是制造企业最痛的问题，
    这张表把它变成一张可排序可下钻的清单。
    """

    as_of_date = models.DateField("快照日期", db_index=True)
    aging_bucket = models.CharField("账龄分桶", max_length=16,
                                    choices=C.AgingBucketChoices.choices, db_index=True)
    customer = models.ForeignKey("masterdata.Customer", on_delete=models.CASCADE,
                                 related_name="aging_snapshots", verbose_name="客户")

    order_count = models.PositiveIntegerField("涉及订单数", default=0)
    outstanding_amount = models.DecimalField("应收未收金额", max_digits=16, decimal_places=2, default=0)
    max_overdue_days = models.IntegerField("最长逾期天数", default=0)

    class Meta:
        db_table = "dws_receivable_aging"
        verbose_name = "应收账龄"
        verbose_name_plural = "应收账龄"
        unique_together = [("as_of_date", "aging_bucket", "customer")]
        indexes = [models.Index(fields=["as_of_date", "aging_bucket"], name="idx_dws_ar_date_bucket")]


class DwsWorkforceMonthly(ReadOnlyModel):
    """人效月度预聚合：人均产值 / 人均毛利 / 工时结构。"""

    period = models.CharField("期间", max_length=7, db_index=True)
    department = models.CharField("部门", max_length=16, db_index=True)

    headcount = models.DecimalField("平均在职人数", max_digits=8, decimal_places=2, default=0)
    labor_cost = models.DecimalField("人力成本", max_digits=16, decimal_places=2, default=0)
    revenue_allocated = models.DecimalField("分摊产值", max_digits=16, decimal_places=2, default=0)
    gross_profit_allocated = models.DecimalField("分摊毛利", max_digits=16, decimal_places=2, default=0)
    total_hours = models.DecimalField("总工时", max_digits=12, decimal_places=2, default=0)

    revenue_per_head = models.DecimalField("人均产值", max_digits=16, decimal_places=2, default=0)
    profit_per_head = models.DecimalField("人均毛利", max_digits=16, decimal_places=2, default=0)
    labor_cost_ratio = models.DecimalField("人力成本占比", max_digits=8, decimal_places=4, default=0)

    class Meta:
        db_table = "dws_workforce_monthly"
        verbose_name = "人效月度预聚合"
        verbose_name_plural = "人效月度预聚合"
        unique_together = [("period", "department")]


class DwsRepMonthly(ReadOnlyModel):
    """业务员月度业绩预聚合：人效排行的取数表。"""

    period = models.CharField("期间", max_length=7, db_index=True)
    sales_rep = models.ForeignKey("masterdata.SalesRep", on_delete=models.CASCADE,
                                  related_name="monthly_perf", verbose_name="业务员")

    order_count = models.PositiveIntegerField("订单数", default=0)
    customer_count = models.PositiveIntegerField("成交客户数", default=0)
    new_customer_count = models.PositiveIntegerField("新客数", default=0)
    revenue = models.DecimalField("成交额", max_digits=16, decimal_places=2, default=0)
    gross_profit = models.DecimalField("毛利", max_digits=16, decimal_places=2, default=0)
    collection_amount = models.DecimalField("回款额", max_digits=16, decimal_places=2, default=0)
    avg_discount = models.DecimalField("平均折扣率", max_digits=6, decimal_places=4, default=0)
    on_time_rate = models.DecimalField("准时交付率", max_digits=8, decimal_places=4, default=0)

    class Meta:
        db_table = "dws_rep_monthly"
        verbose_name = "业务员月度业绩"
        verbose_name_plural = "业务员月度业绩"
        unique_together = [("period", "sales_rep")]
        indexes = [models.Index(fields=["period", "revenue"], name="idx_dws_rep_p_rev")]


# ===========================================================================
# [3] 模型注册与在线评分
# ===========================================================================
class ModelVersion(BaseModel):
    """
    模型版本注册表（让模型从 notebook 走到线上）。

    每次训练产出一个版本：算法、训练窗口、评估指标、产物路径。
    页面可以直接对比「v3 相比 v2 的 AUC 提升」，也能回答
    「你这个模型什么时候训练的、用的什么数据」。
    """

    MODEL_TASK_CHOICES = [
        ("repurchase", "复购预测"),
        ("ltv", "客户终身价值预测"),
        ("order_forecast", "订单量预测"),
        ("churn", "流失预警"),
        ("clustering", "客户分群"),
    ]

    task = models.CharField("任务", max_length=16, choices=MODEL_TASK_CHOICES, db_index=True)
    version = models.CharField("版本号", max_length=16)
    algo = models.CharField("算法", max_length=32)

    train_period_start = models.DateField("训练数据起始", null=True, blank=True)
    train_period_end = models.DateField("训练数据截止", null=True, blank=True)
    sample_count = models.PositiveIntegerField("样本量", default=0)
    feature_count = models.PositiveSmallIntegerField("特征数", default=0)

    # 评估指标（不同任务用不同字段，空的就代表该任务不适用）
    auc = models.DecimalField("AUC", max_digits=6, decimal_places=4, null=True, blank=True)
    ks = models.DecimalField("KS", max_digits=6, decimal_places=4, null=True, blank=True)
    mae = models.DecimalField("MAE", max_digits=12, decimal_places=4, null=True, blank=True)
    mape = models.DecimalField("MAPE", max_digits=8, decimal_places=4, null=True, blank=True)
    r2 = models.DecimalField("R²", max_digits=8, decimal_places=4, null=True, blank=True)

    # 基线对照：证明「复杂模型比简单规则好」，这是面试必答项
    baseline_name = models.CharField("基线方法", max_length=32, blank=True)
    baseline_mae = models.DecimalField("基线 MAE", max_digits=12, decimal_places=4, null=True, blank=True)
    baseline_auc = models.DecimalField("基线 AUC", max_digits=6, decimal_places=4, null=True, blank=True)

    feature_importance = models.JSONField("特征重要性", default=dict, blank=True)
    artifact_path = models.CharField("模型产物路径", max_length=200, blank=True)
    is_active = models.BooleanField("是否为线上版本", default=False, db_index=True)
    note = models.TextField("备注", blank=True)

    trained_at = models.DateTimeField("训练时间", auto_now_add=True)

    class Meta:
        db_table = "dim_model_version"
        verbose_name = "模型版本"
        verbose_name_plural = "模型版本"
        unique_together = [("task", "version")]
        ordering = ["task", "-trained_at"]

    def __str__(self):
        return f"{self.get_task_display()} {self.version}"

    @property
    def improvement_vs_baseline(self):
        """相对基线的误差改善比例（MAE 口径，越小越好 → 正数为改善）。"""
        if not self.mae or not self.baseline_mae:
            return None
        return float((self.baseline_mae - self.mae) / self.baseline_mae)


class ScoringResult(BaseModel):
    """
    在线评分结果。

    粒度：订单 / 客户 × 模型版本。
    记录「什么时候用哪个模型给谁打了多少分、主要影响因素是什么」，，
    这样模型出问题时能追溯，业务提问时能解释。
    """

    SUBJECT_CHOICES = [("order", "订单"), ("customer", "客户")]

    model_version = models.ForeignKey(ModelVersion, on_delete=models.CASCADE,
                                      related_name="scores", verbose_name="模型版本")
    subject_type = models.CharField("对象类型", max_length=8, choices=SUBJECT_CHOICES, db_index=True)
    subject_key = models.CharField("对象标识", max_length=32, db_index=True,
                                   help_text="订单号或客户编码")

    score = models.DecimalField("评分", max_digits=10, decimal_places=6)
    predicted_label = models.CharField("预测标签", max_length=16, blank=True)
    predicted_value = models.DecimalField("预测值", max_digits=16, decimal_places=2,
                                          null=True, blank=True)

    # 可解释性：Top 贡献因子（人话解释，供业务直接读）
    top_factors = models.JSONField("主要影响因素", default=list, blank=True)
    explanation = models.CharField("人话解释", max_length=255, blank=True)

    scored_at = models.DateTimeField("评分时间", auto_now_add=True, db_index=True)

    class Meta:
        db_table = "fact_scoring_result"
        verbose_name = "模型评分结果"
        verbose_name_plural = "模型评分结果"
        unique_together = [("model_version", "subject_type", "subject_key")]
        indexes = [
            models.Index(fields=["subject_type", "score"], name="idx_score_type_score"),
        ]

    def __str__(self):
        return f"{self.subject_key} → {self.score}"
