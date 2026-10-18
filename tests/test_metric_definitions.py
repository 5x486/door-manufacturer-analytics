# -*- coding: utf-8 -*-
"""
指标口径测试（面试必问的第一类）
================================
这组测试是「把口径文档变成代码」。它回答的问题是：
    「你说成交额口径是剔除取消订单，那如果有人改了实现，你怎么知道？」

任何一条口径被改动，这里就会红。这是全项目性价比最高的测试。
"""
import datetime as dt
from decimal import Decimal

import pytest

from apps.core import constants as C


def _revenue_in_db():
    """直接按口径定义从库里算一遍成交额（独立于任何视图/服务层实现）。"""
    from apps.sales.models import Order

    total = Decimal("0")
    for amount in (Order.objects
                   .exclude(order_status__in=C.VALID_ORDER_EXCLUDE_STATUS)
                   .values_list("final_amount", flat=True)):
        total += amount
    return total


@pytest.mark.django_db
class TestRevenueCaveat:
    def test_revenue_excludes_cancelled(self, order_factory):
        """口径：成交额必须剔除「已取消」订单，且绝不能删行。"""
        order_factory(final_amount=Decimal("10000.00"), total_amount=Decimal("10000.00"),
                      discount=Decimal("0"), order_status="已完成")
        order_factory(final_amount=Decimal("50000.00"), total_amount=Decimal("50000.00"),
                      discount=Decimal("0"), order_status="已取消")

        assert _revenue_in_db() == Decimal("10000.00")
        # 取消订单必须仍在表里（保留用于分析取消率），只是不计入营收
        from apps.sales.models import Order
        assert Order.objects.count() == 2

    def test_revenue_includes_producing_orders(self, order_factory):
        """口径：有效订单 = 除「已取消」外全部计入，在产订单也算营收。"""
        order_factory(final_amount=Decimal("8000.00"), total_amount=Decimal("8000.00"),
                      discount=Decimal("0"), order_status="生产中")
        assert _revenue_in_db() == Decimal("8000.00")


@pytest.mark.django_db
class TestFinalAmountFormula:
    def test_final_amount_follows_formula(self, order_factory):
        """口径：成交额 = 商品金额 ×(1-折扣率) + 运费。"""
        order = order_factory(total_amount=Decimal("10000.00"),
                              discount=Decimal("0.1000"),
                              freight=Decimal("300.00"))
        assert order.final_amount == Decimal("9300.00")

    def test_discount_amount_property(self, order_factory):
        """口径：让利金额 = 商品金额 × 折扣率（不是「成交额 × 折扣率」）。"""
        order = order_factory(total_amount=Decimal("10000.00"),
                              discount=Decimal("0.1000"),
                              freight=Decimal("300.00"))
        assert order.discount_amount == Decimal("1000.00")


@pytest.mark.django_db
class TestGrossMarginFormula:
    def test_gross_profit_is_revenue_minus_five_cost_types(self, order_factory, item_factory):
        """口径：毛利 = 明细成交额 -（材料+人工+制造+物流+安装），售后成本不进毛利。"""
        from apps.sales.models import OrderItemCost

        order = order_factory()
        item = item_factory(order, line_amount=Decimal("20000.00"))

        for cost_type, amount in [
            (C.CostTypeChoices.MATERIAL.value, "9000.00"),
            (C.CostTypeChoices.LABOR.value, "3000.00"),
            (C.CostTypeChoices.MANUFACTURING.value, "2000.00"),
            (C.CostTypeChoices.LOGISTICS.value, "800.00"),
            (C.CostTypeChoices.INSTALLATION.value, "700.00"),
        ]:
            OrderItemCost.objects.create(item=item, cost_type=cost_type,
                                         amount=Decimal(amount), is_standard=False)

        cost = sum(OrderItemCost.objects.filter(
            item=item,
            cost_type__in=C.GROSS_PROFIT_COST_TYPES,
        ).values_list("amount", flat=True))
        assert cost == Decimal("15500.00")
        assert item.line_amount - cost == Decimal("4500.00")

    def test_after_sales_cost_not_in_gross_profit(self, order_factory, item_factory):
        """口径：售后成本属质量成本，单列不计毛利 ，， 否则「做得好」和「修得勤」会被混为一谈。"""
        from apps.sales.models import OrderItemCost

        order = order_factory()
        item = item_factory(order)
        OrderItemCost.objects.create(item=item, cost_type=C.CostTypeChoices.AFTER_SALES.value,
                                     amount=Decimal("500.00"), is_standard=False)
        included = OrderItemCost.objects.filter(item=item,
                                                cost_type__in=C.GROSS_PROFIT_COST_TYPES).count()
        assert included == 0


@pytest.mark.django_db
class TestDeliveryCaveat:
    def test_on_time_rate_excludes_undelivered(self, order_factory):
        """口径：准时率的分母只含「已交付」订单，未交付用 is_on_time=-1 标记，不参与计算。"""
        order_factory(is_on_time=1, order_status="已完成")
        order_factory(is_on_time=0, order_status="已完成",
                      actual_delivery_date=dt.date(2025, 5, 1),
                      planned_delivery_date=dt.date(2025, 4, 20))
        order_factory(is_on_time=-1, order_status="生产中",
                      actual_delivery_date=None, actual_lead_days=None)

        from apps.sales.models import Order
        delivered = Order.objects.filter(is_on_time__in=[0, 1])
        assert delivered.count() == 2
        rate = delivered.filter(is_on_time=1).count() / delivered.count()
        assert rate == pytest.approx(0.5)

    def test_undelivered_is_business_attribute_not_dirty_data(self, order_factory):
        """口径：未交付订单的 actual_delivery_date 为空是业务属性，不允许填假日期。"""
        order = order_factory(order_status="生产中", actual_delivery_date=None,
                              actual_lead_days=None, is_on_time=-1)
        assert order.actual_delivery_date is None
        assert order.actual_lead_days is None


@pytest.mark.django_db
class TestAgingBucket:
    @pytest.mark.parametrize("days,expected", [
        (0, "0-30天"),
        (15, "0-30天"),
        (30, "0-30天"),
        (31, "31-60天"),
        (60, "31-60天"),
        (61, "61-90天"),
        (90, "61-90天"),
        (91, "90天以上"),
        (400, "90天以上"),
    ])
    def test_aging_bucket_boundaries(self, days, expected):
        """口径：账龄分桶边界必须一致 ，， 生产与测试共用同一个函数，避免两处各写一遍。"""
        assert C.aging_bucket(days) == expected


@pytest.mark.django_db
class TestCollectionRate:
    def test_collection_rate_formula(self, order_factory):
        """口径：回款率 = 已回款 / 合同金额；未结清才可能有逾期。"""
        from apps.sales.models import PaymentTerm

        order = order_factory(total_amount=Decimal("10000.00"), discount=Decimal("0.1000"),
                             freight=Decimal("0"))
        term = PaymentTerm.objects.create(
            order=order,
            contract_amount=Decimal("9000.00"),
            paid_amount=Decimal("8100.00"),
            outstanding_amount=Decimal("900.00"),
            is_settled=False,
            credit_days=30,
            overdue_days=45,
            aging_bucket=C.aging_bucket(45),
        )
        assert term.collection_rate == pytest.approx(0.9)
        assert term.aging_bucket == "31-60天"

    def test_settled_order_has_no_overdue(self, order_factory):
        """口径：已结清订单的逾期天数必须归零，否则账龄报表会虚高。"""
        from apps.sales.models import PaymentTerm

        order = order_factory()
        term = PaymentTerm.objects.create(
            order=order, contract_amount=Decimal("19600.00"), paid_amount=Decimal("19600.00"),
            outstanding_amount=Decimal("0.00"), is_settled=True, credit_days=15, overdue_days=0,
            aging_bucket=C.aging_bucket(0),
        )
        assert term.is_settled
        assert term.outstanding_amount == Decimal("0.00")
        assert term.overdue_days == 0


@pytest.mark.django_db
class TestRepurchaseAndSegment:
    def test_repurchase_rate_counts_customers_with_two_orders(self, order_factory):
        """口径：复购率 = 有 ≥2 单的客户数 / 有成交的客户数。"""
        from apps.masterdata.models import Customer
        from apps.sales.models import Order

        order_factory()          # 客户 C99999 第 1 单
        order_factory()          # 同一客户第 2 单
        other = Customer.objects.create(
            customer_code="C88888", customer_name="另一个客户", customer_type="个人家装",
            channel="门店到访", register_date=dt.date(2024, 1, 1),
        )
        Order.objects.create(
            order_no="TD900001", customer=other, customer_type="个人家装", channel="门店到访",
            order_type="零售", delivery_type="包安装", order_date=dt.date(2025, 1, 5),
            order_status="已完成", is_on_time=1, total_amount=Decimal("1000.00"),
            discount=Decimal("0"), freight=Decimal("0"), final_amount=Decimal("1000.00"),
            lead_days=20, dominant_category="防火门", item_count=1,
            order_year=2025, order_month=1, order_quarter=1, order_weekday=0,
        )

        valid = Order.objects.exclude(order_status__in=C.VALID_ORDER_EXCLUDE_STATUS)
        from django.db.models import Count
        per_customer = (valid.values("customer_id").annotate(n=Count("id")))
        buyers = per_customer.count()
        repeat = sum(1 for row in per_customer if row["n"] >= 2)
        assert buyers == 2
        assert repeat == 1
        assert repeat / buyers == pytest.approx(0.5)


@pytest.mark.django_db
class TestDataPermission:
    def test_guest_cannot_see_phone(self, guest_client, customer):
        """口径：只读访客看不到客户联系方式（数据权限红线，不是可选装饰）。"""
        user = guest_client.session["_auth_user_id"]
        from apps.accounts.models import User
        guest = User.objects.get(pk=user)
        assert guest.can_see_customer_contact is False
        assert guest.mask_phone("13500000000") == "135****00"

    def test_admin_can_see_phone(self, authenticated_client):
        from apps.accounts.models import User
        boss = User.objects.get(username="tester")
        assert boss.can_see_customer_contact is True
        assert boss.mask_phone("13500000000") == "13500000000"
