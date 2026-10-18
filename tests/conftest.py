# -*- coding: utf-8 -*-
"""
pytest 公共 fixture（conftest）
===============================

为什么测试用 SQLite 而不是 MySQL：
  见 config/settings/test.py 的说明 ，， 建库/销毁开销在几百个用例上是分钟级浪费，
  而本项目被测的重点是「口径与业务逻辑」，不是 MySQL 方言特性。
  少数强依赖 MySQL 行为的用例请用 ``@pytest.mark.integration`` 标记，
  并在 CI 里用 `DJANGO_SETTINGS_MODULE=config.settings.local pytest -m integration` 单独跑。
"""
import datetime as dt
from decimal import Decimal

import pytest
from django.conf import settings
from django.utils import timezone


@pytest.fixture
def as_of():
    """数据基准日（全项目口径的唯一来源）。"""
    return getattr(settings, "DATA_AS_OF_DATE", dt.date(2026, 8, 9))


# ---------------------------------------------------------------------------
# 主数据工厂
# ---------------------------------------------------------------------------
@pytest.fixture
def product(db):
    from apps.masterdata.models import Product

    return Product.objects.create(
        product_code="P900",
        product_name="测试铜门",
        category="铜门",
        material="紫铜",
        surface="铜雕工艺",
        style="新中式",
        feature="静音",
        price_multiplier=Decimal("1.000"),
        base_price=Decimal("10000.00"),
        std_width=1100,
        std_height=2200,
        std_area=2420000,
        lead_min=45,
        lead_max=70,
        defect_rate=Decimal("0.06"),
    )


@pytest.fixture
def store(db):
    from apps.masterdata.models import Store

    return Store.objects.create(
        store_code="S99", store_name="测试展厅", province="浙江", city="桐乡",
        store_type="直营门店", status="营业中",
    )


@pytest.fixture
def rep(db, store):
    from apps.masterdata.models import SalesRep

    return SalesRep.objects.create(
        rep_code="R099", rep_name="测试业务员", gender="女", store=store,
        hire_date=dt.date(2023, 1, 1), level="资深", status="在职",
    )


@pytest.fixture
def customer(db):
    from apps.masterdata.models import Customer

    return Customer.objects.create(
        customer_code="C99999",
        customer_name="测试客户",
        customer_type="个人家装",
        gender="男",
        age=38,
        phone="13500000000",
        province="浙江",
        city="桐乡",
        address="桐乡市测试路1号",
        channel="抖音短视频",
        register_date=dt.date(2023, 3, 1),
        member_level="金卡",
    )


@pytest.fixture
def order_factory(db, customer, store, rep):
    """订单工厂：按需造订单，默认是一笔已完成、准时、¥20,000 的零售单。"""
    from apps.sales.models import Order

    counter = {"n": 0}

    def _make(**kwargs):
        counter["n"] += 1
        n = counter["n"]
        order_date = kwargs.pop("order_date", dt.date(2025, 3, 10))
        planned = kwargs.pop("planned_delivery_date", order_date + dt.timedelta(days=40))
        actual = kwargs.pop("actual_delivery_date", planned)
        total = kwargs.pop("total_amount", Decimal("20000.00"))
        discount = kwargs.pop("discount", Decimal("0.0200"))
        freight = kwargs.pop("freight", Decimal("0.00"))
        defaults = dict(
            order_no=f"TD{n:06d}",
            customer=customer,
            store=store,
            sales_rep=rep,
            customer_type="个人家装",
            channel="抖音短视频",
            order_type="零售",
            delivery_type="包安装",
            order_date=order_date,
            order_status="已完成",
            planned_delivery_date=planned,
            actual_delivery_date=actual,
            is_on_time=1,
            total_amount=total,
            discount=discount,
            freight=freight,
            # 成交额由公式算出，与生产口径一致：total×(1-discount)+freight
            final_amount=(total * (Decimal("1") - discount) + freight).quantize(Decimal("0.01")),
            lead_days=40,
            actual_lead_days=(actual - order_date).days if actual else None,
            dominant_category="铜门",
            item_count=1,
            order_year=order_date.year,
            order_month=order_date.month,
            order_quarter=(order_date.month - 1) // 3 + 1,
            order_weekday=order_date.weekday(),
        )
        defaults.update(kwargs)
        return Order.objects.create(**defaults)

    return _make


@pytest.fixture
def item_factory(db, product):
    from apps.sales.models import OrderItem

    def _make(order, **kwargs):
        defaults = dict(
            item_no=kwargs.pop("item_no", f"TI{order.id:06d}"),
            order=order,
            product=product,
            product_name="测试铜门",
            category="铜门",
            quantity=1,
            width_mm=1100,
            height_mm=2200,
            thickness_mm=70,
            is_custom=False,
            area_m2=Decimal("2.420"),
            unit_price=Decimal("20000.00"),
            line_amount=Decimal("20000.00"),
        )
        defaults.update(kwargs)
        return OrderItem.objects.create(**defaults)

    return _make


@pytest.fixture
def item_cost_factory(db):
    from apps.sales.models import OrderItemCost

    def _make(item, cost_type, amount):
        return OrderItemCost.objects.create(item=item, cost_type=cost_type,
                                            amount=Decimal(str(amount)), is_standard=False)

    return _make


@pytest.fixture
def authenticated_client(client, db):
    """已登录的测试客户端（admin 角色，可看全部数据）。"""
    from apps.accounts.models import User, UserRole

    user = User.objects.create_user(
        username="tester", password="testpass12345", role=UserRole.BOSS, display_name="测试老板",
    )
    client.force_login(user)
    return client


@pytest.fixture
def guest_client(client, db):
    """只读访客：验证「看不到客户联系方式」这条数据权限。"""
    from apps.accounts.models import User, UserRole

    user = User.objects.create_user(
        username="guest", password="guestpass12345", role=UserRole.GUEST, display_name="访客",
    )
    client.force_login(user)
    return client
