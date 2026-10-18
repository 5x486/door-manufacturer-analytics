# -*- coding: utf-8 -*-
"""
销售业务模型（sales）
=====================
订单 / 订单明细 / 明细成本 / 付款 / 合同账期 / 售后 / 交付阶段 / 销售目标

口径要点（与 docs/metrics_spec.md 一致，改口径必须先改这里）：
  · 有效订单 = order_status != '已取消'
  · 成交额 final_amount = total_amount × (1 - discount) + freight
  · 毛利 = 明细成交额 - 计入毛利的五类成本（材料/人工/制造/物流/安装）
  · 售后成本不进毛利，单列为质量成本
  · 未交付订单的 actual_delivery_date 为空且 is_on_time = -1（业务属性，不是脏数据）
"""
from django.db import models

from apps.core import constants as C
from apps.core.base import BaseModel, BusinessModel


class Order(BusinessModel):
    """订单主表。粒度：一笔订单。"""

    order_no = models.CharField("订单号", max_length=24, unique=True)

    customer = models.ForeignKey("masterdata.Customer", on_delete=models.PROTECT,
                                 related_name="orders", verbose_name="客户")
    store = models.ForeignKey("masterdata.Store", on_delete=models.SET_NULL, null=True,
                              related_name="orders", verbose_name="成交门店")
    sales_rep = models.ForeignKey("masterdata.SalesRep", on_delete=models.SET_NULL, null=True,
                                  related_name="orders", verbose_name="业务员")

    # 冗余维度：订单一旦成交，这些属性就固化了（客户后来改类型不影响历史口径）
    customer_type = models.CharField("客户类型", max_length=16,
                                     choices=C.CustomerTypeChoices.choices, db_index=True)
    channel = models.CharField("来源渠道", max_length=16, choices=C.ChannelChoices.choices,
                               db_index=True)
    order_type = models.CharField("订单类型", max_length=16, choices=C.OrderTypeChoices.choices,
                                 db_index=True)
    delivery_type = models.CharField("交付方式", max_length=16,
                                     choices=C.DeliveryTypeChoices.choices)

    order_date = models.DateField("下单日期", db_index=True)
    order_status = models.CharField("订单状态", max_length=16,
                                    choices=C.OrderStatusChoices.choices, db_index=True)
    planned_delivery_date = models.DateField("计划交付日期", null=True, blank=True)
    actual_delivery_date = models.DateField("实际交付日期", null=True, blank=True)

    # 1=按时, 0=延期, -1=未交付（业务属性，不参与准时率分母）
    is_on_time = models.SmallIntegerField("是否按时交付", default=-1, db_index=True)

    total_amount = models.DecimalField("商品金额（折扣前）", max_digits=14, decimal_places=2)
    discount = models.DecimalField("折扣率", max_digits=6, decimal_places=4, default=0)
    freight = models.DecimalField("运费", max_digits=12, decimal_places=2, default=0)
    final_amount = models.DecimalField("成交金额", max_digits=14, decimal_places=2, db_index=True)

    lead_days = models.PositiveSmallIntegerField("标准生产周期（天）", default=0)
    actual_lead_days = models.PositiveSmallIntegerField("实际交付周期（天）", null=True, blank=True)
    delay_days = models.IntegerField("延期天数", default=0)
    delay_reason = models.CharField("延期原因", max_length=16, choices=C.DelayReasonChoices.choices,
                                    blank=True)

    dominant_category = models.CharField("主品类", max_length=16, db_index=True)
    item_count = models.PositiveSmallIntegerField("明细行数", default=0)

    # 时间派生字段：物化存储，避免每次查询都写 extract('year', ...) 导致索引失效
    order_year = models.PositiveSmallIntegerField("下单年份", db_index=True)
    order_month = models.PositiveSmallIntegerField("下单月份")
    order_quarter = models.PositiveSmallIntegerField("下单季度")
    order_weekday = models.PositiveSmallIntegerField("下单星期", default=0)

    class Meta:
        db_table = "fact_order"
        verbose_name = "订单"
        verbose_name_plural = "订单"
        ordering = ["-order_date", "-order_no"]
        indexes = [
            models.Index(fields=["order_year", "order_month"], name="idx_order_ym"),
            models.Index(fields=["channel", "order_year"], name="idx_order_ch_year"),
            models.Index(fields=["order_status", "order_date"], name="idx_order_status_date"),
            models.Index(fields=["dominant_category", "order_year"], name="idx_order_cat_year"),
            models.Index(fields=["is_on_time", "order_year"], name="idx_order_ontime_year"),
        ]

    def __str__(self):
        return f"{self.order_no}（{self.customer.customer_name}）"

    @property
    def is_valid(self):
        """是否有效订单（计入营收）。"""
        return self.order_status != C.OrderStatusChoices.CANCELLED.value

    @property
    def discount_amount(self):
        """让利金额 = 原价 × 折扣率。"""
        return self.total_amount * self.discount

    @property
    def is_delivered(self):
        return self.order_status == C.OrderStatusChoices.DONE.value


class OrderItem(BaseModel):
    """订单明细。粒度：一行一个门产品。"""

    # 业务自然键（对应原始 CSV 的 item_id）。幂等导入、数据质量校验、
    # 明细下钻都以它对外标识；自增主键只用于内部关联。
    # 允许为空：迁移时无需交互式提供默认值；导入器回填后由 DQ 规则保证非空唯一。
    item_no = models.CharField("明细编号", max_length=16, unique=True, null=True, blank=True)

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items",
                              verbose_name="订单")
    product = models.ForeignKey("masterdata.Product", on_delete=models.PROTECT,
                                related_name="order_items", verbose_name="产品")

    product_name = models.CharField("产品名称（成交时快照）", max_length=64)
    category = models.CharField("品类", max_length=16, choices=C.CategoryChoices.choices,
                                db_index=True)
    quantity = models.PositiveSmallIntegerField("数量（樘）")

    width_mm = models.PositiveIntegerField("宽（mm）")
    height_mm = models.PositiveIntegerField("高（mm）")
    thickness_mm = models.PositiveIntegerField("厚（mm）", default=0)
    is_custom = models.BooleanField("是否非标定制", default=False, db_index=True)
    area_m2 = models.DecimalField("面积（㎡）", max_digits=8, decimal_places=3, default=0)

    unit_price = models.DecimalField("成交单价", max_digits=12, decimal_places=2)
    line_amount = models.DecimalField("行金额", max_digits=14, decimal_places=2, db_index=True)

    # 成本分摊结果（由 ETL 写入）：用于明细级毛利，支撑 SKU 毛利排行
    total_cost = models.DecimalField("行总成本", max_digits=14, decimal_places=2, default=0)
    gross_profit = models.DecimalField("行毛利", max_digits=14, decimal_places=2, default=0)

    class Meta:
        db_table = "fact_order_item"
        verbose_name = "订单明细"
        verbose_name_plural = "订单明细"
        ordering = ["order_id", "id"]
        indexes = [
            models.Index(fields=["category", "is_custom"], name="idx_item_cat_custom"),
            models.Index(fields=["product", "order"], name="idx_item_product_order"),
        ]

    def __str__(self):
        return f"{self.order.order_no} / {self.product_name} ×{self.quantity}"

    @property
    def gross_margin(self):
        """行毛利率。"""
        if not self.line_amount:
            return None
        return float(self.gross_profit / self.line_amount)

    @property
    def unit_price_per_m2(self):
        """单位面积价（元/㎡），，面积定价分析用。"""
        if not self.area_m2:
            return None
        return float(self.line_amount / self.area_m2)


class OrderItemCost(BaseModel):
    """
    订单明细的成本拆分（按成本类型）。

    把「行总成本」拆成六类，才能回答「毛利被谁吃掉了」。
    粒度：明细 × 成本类型。
    """

    item = models.ForeignKey(OrderItem, on_delete=models.CASCADE, related_name="costs",
                             verbose_name="订单明细")
    cost_type = models.CharField("成本类型", max_length=16, choices=C.CostTypeChoices.choices,
                                 db_index=True)
    amount = models.DecimalField("金额", max_digits=12, decimal_places=2)

    # 标准成本 vs 实际成本：差异用于成本超支分析
    is_standard = models.BooleanField("是否标准成本", default=False)

    class Meta:
        db_table = "fact_order_item_cost"
        verbose_name = "明细成本"
        verbose_name_plural = "明细成本"
        unique_together = [("item", "cost_type", "is_standard")]
        indexes = [models.Index(fields=["cost_type"], name="idx_itemcost_type")]

    def __str__(self):
        return f"{self.item_id} {self.cost_type} {self.amount}"


class Payment(BaseModel):
    """付款记录（定金 / 中期款 / 尾款 / 退款）。"""

    payment_no = models.CharField("付款流水号", max_length=16, unique=True)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="payments",
                              verbose_name="订单")

    payment_date = models.DateField("付款日期", db_index=True)
    stage = models.CharField("付款阶段", max_length=8, choices=C.PaymentStageChoices.choices)
    amount = models.DecimalField("金额", max_digits=14, decimal_places=2)
    payment_method = models.CharField("付款方式", max_length=16,
                                      choices=C.PaymentMethodChoices.choices, blank=True)
    is_refund = models.BooleanField("是否退款", default=False, db_index=True)

    class Meta:
        db_table = "fact_payment"
        verbose_name = "付款记录"
        verbose_name_plural = "付款记录"
        ordering = ["-payment_date", "-id"]
        indexes = [
            models.Index(fields=["order", "is_refund"], name="idx_pay_order_refund"),
            models.Index(fields=["payment_date", "stage"], name="idx_pay_date_stage"),
        ]

    def __str__(self):
        return f"{self.payment_no} {self.stage} {self.amount}"


class PaymentTerm(BaseModel):
    """
    合同账期与应收（回款分析的核心表）。

    为什么单独建一张：付款记录只说明「收了多少钱」，回答不了
    「什么时候该收」「逾期多久」「谁欠得最久」，，这才是老板关心的回款问题。
    """

    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name="payment_term",
                                 verbose_name="订单")

    contract_amount = models.DecimalField("合同金额", max_digits=14, decimal_places=2)
    paid_amount = models.DecimalField("已回款金额", max_digits=14, decimal_places=2, default=0)
    outstanding_amount = models.DecimalField("应收未收金额", max_digits=14, decimal_places=2,
                                             default=0)

    agreed_due_date = models.DateField("约定回款截止日", null=True, blank=True, db_index=True)
    last_payment_date = models.DateField("最近回款日", null=True, blank=True)

    # 账龄：按 DATA_AS_OF 计算，ETL 每次重建
    overdue_days = models.IntegerField("逾期天数", default=0)
    aging_bucket = models.CharField("账龄分桶", max_length=16, choices=C.AgingBucketChoices.choices,
                                    blank=True, db_index=True)

    is_settled = models.BooleanField("是否结清", default=False, db_index=True)
    credit_days = models.PositiveSmallIntegerField("约定账期（天）", default=0)

    class Meta:
        db_table = "fact_payment_term"
        verbose_name = "合同账期与应收"
        verbose_name_plural = "合同账期与应收"
        indexes = [
            models.Index(fields=["is_settled", "aging_bucket"], name="idx_term_settle_aging"),
        ]

    def __str__(self):
        return f"{self.order.order_no} 应收 {self.outstanding_amount}"

    @property
    def collection_rate(self):
        """回款率。"""
        if not self.contract_amount:
            return None
        return float(self.paid_amount / self.contract_amount)


class AfterSales(BaseModel):
    """售后 / 退换货记录。"""

    after_no = models.CharField("售后单号", max_length=16, unique=True)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="after_sales",
                              verbose_name="订单")
    customer = models.ForeignKey("masterdata.Customer", on_delete=models.CASCADE,
                                 related_name="after_sales", verbose_name="客户")

    report_date = models.DateField("报修日期", db_index=True)
    after_type = models.CharField("问题类型", max_length=16, choices=C.AfterSalesTypeChoices.choices,
                                 db_index=True)
    category = models.CharField("涉及品类", max_length=16, blank=True, db_index=True)

    cost = models.DecimalField("公司承担成本", max_digits=12, decimal_places=2, default=0)
    status = models.CharField("处理状态", max_length=8, choices=C.AfterSalesStatusChoices.choices,
                              default=C.AfterSalesStatusChoices.PROCESSING)
    handling_days = models.PositiveSmallIntegerField("处理时长（天）", default=0)

    # 责任归属：用于质量成本归因（生产/物流/安装/客户）
    responsibility = models.CharField("责任归属", max_length=16, blank=True, db_index=True)

    class Meta:
        db_table = "fact_after_sales"
        verbose_name = "售后记录"
        verbose_name_plural = "售后记录"
        ordering = ["-report_date", "-id"]
        indexes = [
            models.Index(fields=["after_type", "report_date"], name="idx_after_type_date"),
            models.Index(fields=["category", "report_date"], name="idx_after_cat_date"),
        ]

    def __str__(self):
        return f"{self.after_no} {self.after_type}"

    @property
    def is_quality_event(self):
        """是否属于质量事件（退款不算）。"""
        return self.after_type != C.AfterSalesTypeChoices.REFUND.value


class DeliveryStage(BaseModel):
    """
    订单交付阶段记录（履约过程的时间戳）。

    这是把「交付准时率 79.7%」这句话，变成「瓶颈在排产，占了 42% 的超期天数」
    的关键表。没有它，交付分析只能停在结果层。
    阶段顺序见 C.DELIVERY_STAGE_SEQUENCE。
    """

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="delivery_stages",
                              verbose_name="订单")
    stage = models.CharField("阶段", max_length=16, choices=C.DeliveryStageChoices.choices,
                             db_index=True)
    stage_order = models.PositiveSmallIntegerField("阶段序号", default=0)

    planned_date = models.DateField("计划完成日", null=True, blank=True)
    actual_date = models.DateField("实际完成日", null=True, blank=True)

    duration_days = models.PositiveSmallIntegerField("实际耗时（天）", default=0)
    delayed_days = models.IntegerField("超出计划天数", default=0)
    is_bottleneck = models.BooleanField("是否瓶颈阶段", default=False)

    class Meta:
        db_table = "fact_delivery_stage"
        verbose_name = "交付阶段"
        verbose_name_plural = "交付阶段"
        unique_together = [("order", "stage")]
        ordering = ["order_id", "stage_order"]
        indexes = [
            models.Index(fields=["stage", "delayed_days"], name="idx_stage_delay"),
        ]

    def __str__(self):
        return f"{self.order.order_no} / {self.stage}"


class SalesTarget(BaseModel):
    """
    销售目标（用于「目标 vs 实际」达成分析）。

    粒度：期间 × 范围（全公司/渠道/门店/业务员/品类）× 指标。
    target_scope 与 target_key 组合定位：scope=渠道 时 key='抖音短视频'。
    """

    period_type = models.CharField("期间类型", max_length=8, default="month",
                                   help_text="month / year")
    period = models.CharField("期间", max_length=7, db_index=True,
                              help_text="month 用 YYYY-MM；year 用 YYYY")
    target_scope = models.CharField("目标范围", max_length=8, choices=C.TargetScopeChoices.choices,
                                    db_index=True)
    target_key = models.CharField("范围取值", max_length=32, blank=True,
                                  help_text="渠道名 / 门店编码 / 业务员编码 / 品类名；全公司留空")
    metric = models.CharField("指标", max_length=16, choices=C.TargetMetricChoices.choices)

    target_value = models.DecimalField("目标值", max_digits=14, decimal_places=2)

    class Meta:
        db_table = "fact_sales_target"
        verbose_name = "销售目标"
        verbose_name_plural = "销售目标"
        unique_together = [("period_type", "period", "target_scope", "target_key", "metric")]
        ordering = ["-period", "target_scope"]

    def __str__(self):
        return f"{self.period} {self.target_scope}{self.target_key} {self.metric}={self.target_value}"


class LaborHour(BaseModel):
    """
    工时记录（人效分析用）。

    粒度：员工 × 日期 × 订单 × 工时类型。
    这是「人均产值」「单订单工时」的分子/分母来源。
    """

    employee = models.ForeignKey("masterdata.Employee", on_delete=models.CASCADE,
                                 related_name="labor_hours", verbose_name="员工")
    work_date = models.DateField("日期", db_index=True)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, null=True, blank=True,
                              related_name="labor_hours", verbose_name="关联订单")
    work_type = models.CharField("工时类型", max_length=16, choices=C.WorkTypeChoices.choices,
                                 db_index=True)
    hours = models.DecimalField("工时（小时）", max_digits=6, decimal_places=2)

    class Meta:
        db_table = "fact_labor_hour"
        verbose_name = "工时记录"
        verbose_name_plural = "工时记录"
        indexes = [
            models.Index(fields=["work_date", "work_type"], name="idx_labor_date_type"),
        ]

    def __str__(self):
        return f"{self.employee.employee_name} {self.work_date} {self.hours}h"
