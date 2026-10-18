# -*- coding: utf-8 -*-
"""
主数据模型（masterdata）
========================
产品 / 门店 / 业务员 / 客户 / 成本标准 / 竞品对标 / 员工 / 工时

设计说明：
  - 货币字段统一 DecimalField(14,2)，绝不用 float ，， 金额求和不能有浮点误差；
  - 业务编码（product_code 等）加唯一索引，CSV 导入时以它为 upsert 键，保证可重跑；
  - 大量使用 db_index：分析查询基本都在 品类 / 渠道 / 城市 / 日期 上做 group by。
"""
from django.db import models

from apps.core.base import BaseModel, BusinessModel
from apps.core import constants as C


class Product(BaseModel):
    """产品目录（SKU）。粒度：一个 SKU 一行。"""

    product_code = models.CharField("产品编码", max_length=16, unique=True)
    product_name = models.CharField("产品名称", max_length=64)
    category = models.CharField("品类", max_length=16, choices=C.CategoryChoices.choices,
                                db_index=True)
    material = models.CharField("材质", max_length=32, blank=True)
    surface = models.CharField("表面工艺", max_length=32, blank=True)
    style = models.CharField("风格", max_length=16, blank=True)
    feature = models.CharField("附加配置", max_length=32, blank=True)

    price_multiplier = models.DecimalField("配置价格系数", max_digits=6, decimal_places=3, default=1)
    base_price = models.DecimalField("基础价（元/樘）", max_digits=12, decimal_places=2)
    std_width = models.PositiveIntegerField("标准宽（mm）")
    std_height = models.PositiveIntegerField("标准高（mm）")
    std_area = models.BigIntegerField("标准面积（mm²）")

    lead_min = models.PositiveSmallIntegerField("最短生产周期（天）")
    lead_max = models.PositiveSmallIntegerField("最长生产周期（天）")
    defect_rate = models.DecimalField("基准售后率", max_digits=5, decimal_places=4, default=0)

    class Meta:
        db_table = "md_product"
        verbose_name = "产品"
        verbose_name_plural = "产品目录"
        ordering = ["product_code"]
        indexes = [
            models.Index(fields=["category", "base_price"], name="idx_product_cat_price"),
        ]

    def __str__(self):
        return f"{self.product_code} {self.product_name}"

    @property
    def profit_segment(self):
        """利润型 / 走量型 / 工程型，用于毛利结构分组。"""
        return C.CATEGORY_PROFIT_SEGMENT.get(self.category, "其他")


class ProductCostStandard(BaseModel):
    """
    产品成本标准（BOM 级标准成本）。

    为什么需要这张表：没有它就算不出毛利，整个项目只能停在「营收分析」层。
    成本按 M-1 起始日生效，保留历史版本，以便回答「当时的毛利是多少」。
    """

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="cost_standards",
                                verbose_name="产品")
    effective_from = models.DateField("生效日期", db_index=True)

    material_cost = models.DecimalField("材料成本", max_digits=12, decimal_places=2)
    labor_cost = models.DecimalField("人工成本", max_digits=12, decimal_places=2)
    manufacturing_cost = models.DecimalField("制造费用", max_digits=12, decimal_places=2)
    logistics_cost = models.DecimalField("物流成本", max_digits=12, decimal_places=2)
    installation_cost = models.DecimalField("安装成本", max_digits=12, decimal_places=2)

    # 材料单价（元/kg）与单樘用料，用于原材料涨价敏感性分析
    material_unit_price = models.DecimalField("材料单价（元/kg）", max_digits=8, decimal_places=2,
                                              default=0)
    material_weight_kg = models.DecimalField("单樘用料（kg）", max_digits=8, decimal_places=2,
                                             default=0)

    class Meta:
        db_table = "md_product_cost_standard"
        verbose_name = "产品成本标准"
        verbose_name_plural = "产品成本标准"
        unique_together = [("product", "effective_from")]
        ordering = ["product_id", "-effective_from"]

    def __str__(self):
        return f"{self.product.product_code} @{self.effective_from}"

    @property
    def total_standard_cost(self):
        """标准总成本（不含售后成本）。"""
        return (self.material_cost + self.labor_cost + self.manufacturing_cost
                + self.logistics_cost + self.installation_cost)


class Store(BaseModel):
    """门店 / 渠道据点。"""

    store_code = models.CharField("门店编码", max_length=8, unique=True)
    store_name = models.CharField("门店名称", max_length=32)
    province = models.CharField("省份", max_length=16, blank=True)
    city = models.CharField("城市", max_length=32, blank=True)
    store_type = models.CharField("门店类型", max_length=16)
    status = models.CharField("状态", max_length=8, default="营业中")

    class Meta:
        db_table = "md_store"
        verbose_name = "门店"
        verbose_name_plural = "门店"
        ordering = ["store_code"]

    def __str__(self):
        return self.store_name


class SalesRep(BaseModel):
    """业务员（销售责任人）。"""

    rep_code = models.CharField("业务员编码", max_length=8, unique=True)
    rep_name = models.CharField("姓名", max_length=16)
    gender = models.CharField("性别", max_length=2, blank=True)
    store = models.ForeignKey(Store, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="sales_reps", verbose_name="所属门店")
    hire_date = models.DateField("入职日期", null=True, blank=True)
    level = models.CharField("职级", max_length=8, blank=True)
    status = models.CharField("状态", max_length=8, default="在职")

    class Meta:
        db_table = "md_sales_rep"
        verbose_name = "业务员"
        verbose_name_plural = "业务员"
        ordering = ["rep_code"]

    def __str__(self):
        return f"{self.rep_name}（{self.rep_code}）"


class Employee(BaseModel):
    """
    员工（含生产/安装/售后/物流），用于人效分析。

    与 SalesRep 的关系：SalesRep 是「销售责任人」业务角色（订单挂靠），
    Employee 是「人力成本与工时」主体。一名销售代表同时是一条 Employee 记录，
    通过 employee_code / rep_code 对应，但两者职责不同，故不合并。
    """

    employee_code = models.CharField("工号", max_length=12, unique=True)
    employee_name = models.CharField("姓名", max_length=16)
    gender = models.CharField("性别", max_length=2, blank=True)
    department = models.CharField("部门", max_length=16, choices=C.DepartmentChoices.choices,
                                  db_index=True)
    position = models.CharField("岗位", max_length=16, blank=True)
    hire_date = models.DateField("入职日期", null=True, blank=True)
    leave_date = models.DateField("离职日期", null=True, blank=True)
    monthly_salary = models.DecimalField("月薪（元）", max_digits=10, decimal_places=2, default=0)
    status = models.CharField("状态", max_length=8, default="在职")

    class Meta:
        db_table = "md_employee"
        verbose_name = "员工"
        verbose_name_plural = "员工"
        ordering = ["employee_code"]

    def __str__(self):
        return f"{self.employee_name}（{self.department}）"


class Customer(BusinessModel):
    """客户实体（含未成交线索）。"""

    customer_code = models.CharField("客户编码", max_length=12, unique=True)
    customer_name = models.CharField("姓名/公司名", max_length=64)
    customer_type = models.CharField("客户类型", max_length=16, choices=C.CustomerTypeChoices.choices,
                                     db_index=True)

    gender = models.CharField("性别", max_length=2, blank=True)
    age = models.PositiveSmallIntegerField("年龄", null=True, blank=True)
    phone = models.CharField("联系电话", max_length=20, blank=True)
    province = models.CharField("省份", max_length=16, blank=True)
    city = models.CharField("城市", max_length=32, blank=True, db_index=True)
    address = models.CharField("地址", max_length=128, blank=True)

    channel = models.CharField("首次来源渠道", max_length=16, choices=C.ChannelChoices.choices,
                              db_index=True)
    # 允许为空：未成交线索中有 46 条没有建档日期。
    # 这里不填假日期、也不删行 —— 缺失是业务属性，不是数据质量事故。
    register_date = models.DateField("建档日期", null=True, blank=True, db_index=True)
    member_level = models.CharField("会员等级", max_length=8, choices=C.MemberLevelChoices.choices,
                                    default=C.MemberLevelChoices.NORMAL, db_index=True)

    # 冗余累计字段：由 ETL 重算，用于列表页排序（避免每行都去聚合订单表）
    total_orders = models.PositiveIntegerField("累计有效订单数", default=0)
    total_spend = models.DecimalField("累计有效消费", max_digits=14, decimal_places=2, default=0)

    first_order_date = models.DateField("首单日期", null=True, blank=True)
    last_order_date = models.DateField("最近下单日期", null=True, blank=True, db_index=True)

    # 是否未成交线索：1 = 仅建档未下单，是渠道转化率的分母
    is_lead = models.BooleanField("是否未成交线索", default=False, db_index=True)

    # ---- RFM 分群结果（由 rebuild_rfm 命令写入，不手工维护）----
    recency_days = models.IntegerField("最近购买距今天数", null=True, blank=True)
    frequency = models.PositiveIntegerField("购买频次", default=0)
    monetary = models.DecimalField("累计消费额", max_digits=14, decimal_places=2, default=0)
    r_score = models.PositiveSmallIntegerField("R 分值", null=True, blank=True)
    f_score = models.PositiveSmallIntegerField("F 分值", null=True, blank=True)
    m_score = models.PositiveSmallIntegerField("M 分值", null=True, blank=True)
    rfm_score = models.PositiveSmallIntegerField("RFM 总分", null=True, blank=True)
    segment = models.CharField("客户分群", max_length=16, choices=C.SegmentChoices.choices,
                               blank=True, db_index=True)
    kmeans_cluster = models.SmallIntegerField("KMeans 簇号", null=True, blank=True)

    class Meta:
        db_table = "md_customer"
        verbose_name = "客户"
        verbose_name_plural = "客户"
        ordering = ["-total_spend"]
        indexes = [
            models.Index(fields=["segment", "monetary"], name="idx_cust_seg_money"),
            models.Index(fields=["channel", "is_lead"], name="idx_cust_ch_lead"),
            models.Index(fields=["register_date", "customer_type"], name="idx_cust_reg_type"),
        ]

    def __str__(self):
        return f"{self.customer_code} {self.customer_name}"

    @property
    def display_name(self):
        """列表页展示用：公司名太长时截断。"""
        name = self.customer_name
        return name if len(name) <= 14 else name[:13] + "…"


class CompetitorBenchmark(BusinessModel):
    """
    竞品对标价（用于定价与折扣侵蚀分析）。

    数据来源假设：业务员市场询价 + 展会同行报价收集，
    按 品类 × 城市层级 记录主流竞品价格区间。
    """

    category = models.CharField("品类", max_length=16, choices=C.CategoryChoices.choices,
                                db_index=True)
    city_tier = models.CharField("城市层级", max_length=8,
                                 help_text="本地/周边/外省")
    period = models.CharField("统计期间", max_length=7, help_text="YYYY-MM")

    competitor_price_low = models.DecimalField("竞品低价", max_digits=12, decimal_places=2)
    competitor_price_high = models.DecimalField("竞品高价", max_digits=12, decimal_places=2)
    our_avg_price = models.DecimalField("我方均价", max_digits=12, decimal_places=2)

    sample_count = models.PositiveSmallIntegerField("样本数", default=0)
    note = models.CharField("备注", max_length=128, blank=True)

    class Meta:
        db_table = "md_competitor_benchmark"
        verbose_name = "竞品对标价"
        verbose_name_plural = "竞品对标价"
        unique_together = [("category", "city_tier", "period")]
        ordering = ["-period", "category"]

    def __str__(self):
        return f"{self.period} {self.category}@{self.city_tier}"

    @property
    def price_position(self):
        """我方均价在竞品区间中的相对位置（0~1，越小越便宜）。"""
        span = self.competitor_price_high - self.competitor_price_low
        if span <= 0:
            return None
        return float((self.our_avg_price - self.competitor_price_low) / span)
