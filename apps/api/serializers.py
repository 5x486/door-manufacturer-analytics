"""明细资源序列化器。

设计取舍
--------
1. **不写 ``fields = "__all__"``**。本项目刻意逐个声明字段，理由有三：
   a. ``__all__`` 会让字段的中文 label 丢失（模型里没写 ``verbose_name`` 时），
      前端拿到的就是 ``final_amount`` 这种英文键加空 label，做不了表头；
   b. 模型新增字段会被**静默**暴露到接口上，其中可能包含内部成本字段
      （``total_cost``、``gross_profit``），，明细接口是对外/对业务的，
      成本类字段的暴露必须是一个明确决定，而不是改模型的副作用；
   c. 逐字段声明后，``cost`` 这类敏感金额可以按需剔除。
2. **列表用精简版**（``*ListSerializer``）。明细列表接口是高频调用的，
   带上成本、售后、阶段等关联字段会让响应体积翻十倍；需要细节时走详情接口
   （``GET /api/v1/orders/`` 的列表 + 客户 360 接口已覆盖主要下钻场景）。
3. **中文 label 一律写在序列化器里**，而不是依赖模型的 ``verbose_name``：
   label 属于「接口契约」，模型字段改名不应该悄悄改变接口的字段描述。
4. **金额字段用 ``DecimalField`` 而不是 ``FloatField``**：金额永远不要变成二进制浮点，
   否则 0.1 + 0.2 这类误差会在前端累加求和时暴露出来。
"""

from __future__ import annotations

from rest_framework import serializers

__all__ = [
    "OrderListSerializer",
    "OrderSerializer",
    "OrderItemListSerializer",
    "OrderItemSerializer",
    "CustomerListSerializer",
    "CustomerSerializer",
    "ProductListSerializer",
    "ProductSerializer",
    "PaymentSerializer",
    "AfterSalesSerializer",
]


# ---------------------------------------------------------------------------
# 订单
# ---------------------------------------------------------------------------
class OrderListSerializer(serializers.Serializer):
    """订单列表（精简版）：只保留下钻表格需要的字段。"""

    order_no = serializers.CharField(label="订单编号", read_only=True)
    order_date = serializers.DateField(label="下单日期", read_only=True)
    customer_code = serializers.SerializerMethodField(label="客户编码")
    customer_name = serializers.SerializerMethodField(label="客户名称")
    customer_type = serializers.CharField(label="客户类型", read_only=True)
    channel = serializers.CharField(label="获客渠道", read_only=True)
    order_type = serializers.CharField(label="订单类型", read_only=True)
    store_name = serializers.SerializerMethodField(label="门店")
    sales_rep_name = serializers.SerializerMethodField(label="业务员")
    dominant_category = serializers.CharField(label="订单主品类", read_only=True)
    item_count = serializers.IntegerField(label="明细行数", read_only=True)
    total_amount = serializers.DecimalField(label="订单原价", max_digits=14, decimal_places=2, read_only=True)
    discount = serializers.DecimalField(label="让利率", max_digits=6, decimal_places=4, read_only=True)
    final_amount = serializers.DecimalField(label="订单终价", max_digits=14, decimal_places=2, read_only=True)
    order_status = serializers.CharField(label="订单状态", read_only=True)
    is_on_time = serializers.IntegerField(label="是否准时交付", read_only=True)

    def get_customer_code(self, obj):
        """客户编码：客户为空（散客）时返回空串而不是报错。"""
        return getattr(getattr(obj, "customer", None), "customer_code", "") or ""

    def get_customer_name(self, obj):
        return getattr(getattr(obj, "customer", None), "customer_name", "") or "散客"

    def get_store_name(self, obj):
        return getattr(getattr(obj, "store", None), "store_name", "") or ""

    def get_sales_rep_name(self, obj):
        return getattr(getattr(obj, "sales_rep", None), "rep_name", "") or ""


class OrderSerializer(OrderListSerializer):
    """订单详情（全量字段）：含交付与延期信息。"""

    delivery_type = serializers.CharField(label="交付方式", read_only=True)
    planned_delivery_date = serializers.DateField(label="计划交付日", read_only=True)
    actual_delivery_date = serializers.DateField(label="实际交付日", read_only=True)
    lead_days = serializers.IntegerField(label="计划交付周期(天)", read_only=True)
    actual_lead_days = serializers.IntegerField(label="实际交付周期(天)", read_only=True)
    delay_days = serializers.IntegerField(label="延期天数", read_only=True)
    delay_reason = serializers.CharField(label="延期原因", read_only=True)
    freight = serializers.DecimalField(label="运费", max_digits=12, decimal_places=2, read_only=True)
    order_year = serializers.IntegerField(label="订单年份", read_only=True)
    order_month = serializers.IntegerField(label="订单月份", read_only=True)
    order_quarter = serializers.IntegerField(label="订单季度", read_only=True)


# ---------------------------------------------------------------------------
# 订单明细
# ---------------------------------------------------------------------------
class OrderItemListSerializer(serializers.Serializer):
    """订单明细行（精简版）。"""

    order_no = serializers.SerializerMethodField(label="所属订单号")
    product_code = serializers.SerializerMethodField(label="产品编码")
    product_name = serializers.CharField(label="产品名称", read_only=True)
    category = serializers.CharField(label="品类", read_only=True)
    specification = serializers.CharField(label="规格", read_only=True)
    quantity = serializers.IntegerField(label="数量", read_only=True)
    area_m2 = serializers.DecimalField(label="面积(㎡)", max_digits=10, decimal_places=2, read_only=True)
    unit_price = serializers.DecimalField(label="单价", max_digits=14, decimal_places=2, read_only=True)
    line_amount = serializers.DecimalField(label="行金额", max_digits=14, decimal_places=2, read_only=True)

    def get_order_no(self, obj):
        return getattr(getattr(obj, "order", None), "order_no", "") or ""

    def get_product_code(self, obj):
        return getattr(getattr(obj, "product", None), "product_code", "") or ""


class OrderItemSerializer(OrderItemListSerializer):
    """订单明细（含成本与毛利）。

    说明：成本与毛利属于**内部字段**，这里显式保留是因为本接口的调用方
    是内部经营分析前端（已登录用户）。对外提供时应当改成不含成本字段的序列化器。
    """

    total_cost = serializers.DecimalField(label="明细总成本", max_digits=14, decimal_places=2, read_only=True)
    gross_profit = serializers.DecimalField(label="明细毛利额", max_digits=14, decimal_places=2, read_only=True)
    gross_margin = serializers.DecimalField(label="明细毛利率", max_digits=6, decimal_places=4, read_only=True)


# ---------------------------------------------------------------------------
# 客户
# ---------------------------------------------------------------------------
class CustomerListSerializer(serializers.Serializer):
    """客户列表（精简版）：注意 **不含明文电话**。

    电话字段一律走 ``phone_masked``，脱敏逻辑在 ``apps.accounts.models.User.mask_phone``
    （按权限判断）。接口层不做「看情况决定要不要脱敏」的分支，，
    分支越多越容易在某条路径上漏掉，统一脱敏 + 单独的授权接口才是安全写法。
    """

    customer_code = serializers.CharField(label="客户编码", read_only=True)
    customer_name = serializers.CharField(label="客户名称", read_only=True)
    customer_type = serializers.CharField(label="客户类型", read_only=True)
    channel = serializers.CharField(label="获客渠道", read_only=True)
    province = serializers.CharField(label="省份", read_only=True)
    city = serializers.CharField(label="城市", read_only=True)
    member_level = serializers.CharField(label="会员等级", read_only=True)
    total_orders = serializers.IntegerField(label="累计订单数", read_only=True)
    total_spend = serializers.DecimalField(label="累计消费额", max_digits=16, decimal_places=2, read_only=True)
    first_order_date = serializers.DateField(label="首次下单日", read_only=True)
    last_order_date = serializers.DateField(label="最近下单日", read_only=True)
    segment = serializers.CharField(label="RFM 客群", read_only=True)


class CustomerSerializer(CustomerListSerializer):
    """客户详情（含 RFM 细分与脱敏电话）。"""

    gender = serializers.CharField(label="性别", read_only=True)
    age = serializers.IntegerField(label="年龄", read_only=True)
    phone_masked = serializers.SerializerMethodField(label="联系电话（脱敏）")
    address = serializers.CharField(label="地址", read_only=True)
    register_date = serializers.DateField(label="注册日期", read_only=True)
    is_lead = serializers.BooleanField(label="是否线索客户", read_only=True)
    recency_days = serializers.IntegerField(label="最近消费间隔(天)", read_only=True)
    frequency = serializers.IntegerField(label="消费频次", read_only=True)
    monetary = serializers.DecimalField(label="消费金额", max_digits=16, decimal_places=2, read_only=True)
    r_score = serializers.IntegerField(label="R 分值", read_only=True)
    f_score = serializers.IntegerField(label="F 分值", read_only=True)
    m_score = serializers.IntegerField(label="M 分值", read_only=True)
    rfm_score = serializers.CharField(label="RFM 总分", read_only=True)
    kmeans_cluster = serializers.CharField(label="聚类簇", read_only=True)

    def get_phone_masked(self, obj):
        """脱敏电话：优先用账号的 ``mask_phone``；无权限时只留首尾。"""
        phone = getattr(obj, "phone", "") or ""
        if not phone:
            return ""
        request = self.context.get("request")
        user = getattr(request, "user", None) if request is not None else None
        masker = getattr(user, "mask_phone", None)
        if callable(masker):
            try:
                return masker(phone) or _fallback_mask(phone)
            except Exception:  # pragma: no cover - 脱敏失败必须更严格
                return _fallback_mask(phone)
        return _fallback_mask(phone)

    # 兼容旧字段名：部分调用方历史上用的是 ``phone``
    phone = serializers.SerializerMethodField(label="联系电话（脱敏，兼容字段）")

    def get_phone(self, obj):
        return self.get_phone_masked(obj)


def _fallback_mask(phone: str) -> str:
    """本地兜底脱敏：保留前 3 位与后 2 位。"""
    text = str(phone)
    if len(text) >= 7:
        return "{}****{}".format(text[:3], text[-2:])
    return "***"


# ---------------------------------------------------------------------------
# 产品
# ---------------------------------------------------------------------------
class ProductListSerializer(serializers.Serializer):
    """产品列表（精简版）。"""

    product_code = serializers.CharField(label="产品编码", read_only=True)
    product_name = serializers.CharField(label="产品名称", read_only=True)
    category = serializers.CharField(label="品类", read_only=True)
    specification = serializers.CharField(label="规格", read_only=True)
    unit = serializers.CharField(label="计量单位", read_only=True)
    standard_price = serializers.DecimalField(label="标准售价", max_digits=14, decimal_places=2, read_only=True)
    is_active = serializers.BooleanField(label="是否在售", read_only=True)


class ProductSerializer(ProductListSerializer):
    """产品详情（含成本标准）。"""

    material = serializers.CharField(label="材质", read_only=True)
    surface_treatment = serializers.CharField(label="表面处理", read_only=True)
    standard_cost = serializers.DecimalField(label="标准成本", max_digits=14, decimal_places=2, read_only=True)
    standard_margin = serializers.DecimalField(label="标准毛利率", max_digits=6, decimal_places=4, read_only=True)


# ---------------------------------------------------------------------------
# 付款
# ---------------------------------------------------------------------------
class PaymentSerializer(serializers.Serializer):
    """付款流水。

    ``is_refund`` 必须返回：不区分退款金额会让「回款额」虚高，
    这是收付款分析里最容易出错的地方。
    """

    payment_no = serializers.CharField(label="付款单号", read_only=True)
    order_no = serializers.SerializerMethodField(label="所属订单号")
    payment_date = serializers.DateField(label="付款日期", read_only=True)
    stage = serializers.CharField(label="付款阶段", read_only=True)
    amount = serializers.DecimalField(label="付款金额", max_digits=14, decimal_places=2, read_only=True)
    payment_method = serializers.CharField(label="付款方式", read_only=True)
    is_refund = serializers.BooleanField(label="是否为退款", read_only=True)

    def get_order_no(self, obj):
        return getattr(getattr(obj, "order", None), "order_no", "") or ""


# ---------------------------------------------------------------------------
# 售后
# ---------------------------------------------------------------------------
class AfterSalesSerializer(serializers.Serializer):
    """售后工单。"""

    after_no = serializers.CharField(label="工单编号", read_only=True)
    order_no = serializers.SerializerMethodField(label="所属订单号")
    customer_name = serializers.SerializerMethodField(label="客户名称")
    report_date = serializers.DateField(label="报修日期", read_only=True)
    after_type = serializers.CharField(label="售后类型", read_only=True)
    category = serializers.CharField(label="涉及品类", read_only=True)
    responsibility = serializers.CharField(label="责任方", read_only=True)
    status = serializers.CharField(label="处理状态", read_only=True)
    handling_days = serializers.IntegerField(label="处理天数", read_only=True)
    cost = serializers.DecimalField(label="售后成本", max_digits=12, decimal_places=2, read_only=True)

    def get_order_no(self, obj):
        return getattr(getattr(obj, "order", None), "order_no", "") or ""

    def get_customer_name(self, obj):
        customer = getattr(obj, "customer", None) or getattr(getattr(obj, "order", None), "customer", None)
        return getattr(customer, "customer_name", "") or ""
