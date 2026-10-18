# -*- coding: utf-8 -*-
"""
数据质量规则测试（面试必问的第三类）
====================================
做法：故意插入坏数据，断言规则能捕获。
「校验脚本本身没被验证过」是数据治理最常见的漏洞 ，，
如果规则永远返回通过，那它就不是校验，只是一段打印。
"""
import datetime as dt
from decimal import Decimal

import pytest

from apps.core import constants as C


def _run(rule_code):
    """跑单条规则，返回 (status, issue_rows)。"""
    from apps.dq import checks

    rule = checks.build_check_index()[rule_code]
    ctx = checks.CheckContext()
    outcome = rule["func"](ctx)
    from apps.dq.checks import _evaluate_status
    status = _evaluate_status(rule["severity"], outcome["issue_rows"], rule["threshold"])
    return status, outcome["issue_rows"], outcome["sample_keys"]


@pytest.mark.django_db
class TestPrimaryKeyRules:
    def test_duplicate_order_no_is_caught(self, order_factory):
        """主键唯一：订单号重复必须被捕获。"""
        from apps.sales.models import Order
        from django.db import IntegrityError

        order = order_factory()
        # 模型层已加 unique 约束，所以这里验证「约束存在」这件事本身
        with pytest.raises(IntegrityError):
            Order.objects.create(
                order_no=order.order_no, customer=order.customer, customer_type="个人家装",
                channel="抖音短视频", order_type="零售", delivery_type="包安装",
                order_date=dt.date(2025, 1, 1), order_status="已完成", is_on_time=1,
                total_amount=Decimal("100"), discount=Decimal("0"), freight=Decimal("0"),
                final_amount=Decimal("100"), dominant_category="铜门", item_count=1,
                order_year=2025, order_month=1, order_quarter=1, order_weekday=0,
            )

    def test_empty_table_passes(self, db):
        """空库也必须通过：规则不能把「没有数据」当成失败，否则首日就报假警。"""
        status, issue_rows, _ = _run("PK_ORDERS_NO")
        assert status == "pass"
        assert issue_rows == 0


@pytest.mark.django_db
class TestAmountRules:
    def test_final_amount_formula_violation_is_caught(self, order_factory):
        """金额对账：成交额与公式不一致必须被抓到（这是最容易悄悄发生的口径漂移）。"""
        from apps.sales.models import Order

        order = order_factory(total_amount=Decimal("10000.00"), discount=Decimal("0.1000"),
                              freight=Decimal("0.00"))
        # 人为破坏：改成不符合公式的数。
        # 必须用 queryset.update 而不是 order.save()：模型层会按公式重算，
        # 用 save 根本造不出坏数据（这本身是好事）。update 模拟「上游系统灌进脏数据」。
        Order.objects.filter(pk=order.pk).update(final_amount=Decimal("1234.56"))

        status, issue_rows, keys = _run("AMT_FINAL_FORMULA")
        assert status in ("fail", "warn")
        assert issue_rows == 1
        assert order.order_no in keys

    def test_correct_formula_passes(self, order_factory):
        """公式正确的订单不能误报（误报会让团队忽略告警）。"""
        order_factory(total_amount=Decimal("10000.00"), discount=Decimal("0.1000"),
                      freight=Decimal("300.00"))
        status, issue_rows, _ = _run("AMT_FINAL_FORMULA")
        assert status == "pass"
        assert issue_rows == 0

    def test_negative_cost_is_caught(self, order_factory, item_factory, item_cost_factory):
        """成本为负说明成本归集逻辑出错，必须阻断。"""
        order = order_factory()
        item = item_factory(order, line_amount=Decimal("20000.00"))
        item_cost_factory(item, C.CostTypeChoices.MATERIAL.value, "-100.00")

        status, issue_rows, _ = _run("AMT_COST_POSITIVE")
        assert status in ("fail", "warn")
        assert issue_rows >= 1


@pytest.mark.django_db
class TestDateRules:
    def test_delivery_before_order_is_caught(self, order_factory):
        """日期合理性：交付早于下单是时间倒挂，一定有问题。"""
        # 同样必须绕过模型层：actual_lead_days 有 CHECK 约束，factory 造不出倒挂日期
        from apps.sales.models import Order

        order = order_factory(order_date=dt.date(2025, 5, 10),
                              planned_delivery_date=dt.date(2025, 5, 10),
                              actual_delivery_date=dt.date(2025, 5, 10))
        Order.objects.filter(pk=order.pk).update(actual_delivery_date=dt.date(2025, 5, 1))
        status, issue_rows, _ = _run("DATE_DELIVERY_AFTER_ORDER")
        assert status in ("fail", "warn")
        assert issue_rows == 1

    def test_future_date_beyond_as_of_is_caught(self, order_factory):
        """数据截止日之后的记录不允许存在（否则「未来订单」会污染趋势分析）。"""
        order_factory(order_date=dt.date(2099, 1, 1))
        status, issue_rows, _ = _run("DATE_NO_FUTURE")
        assert status in ("fail", "warn")
        assert issue_rows >= 1


@pytest.mark.django_db
class TestBusinessRules:
    def test_cancelled_order_with_delivery_is_caught(self, order_factory):
        """业务合理性：已取消订单不该有交付结论。"""
        order_factory(order_status="已取消",
                      actual_delivery_date=dt.date(2025, 4, 1),
                      is_on_time=0)
        status, issue_rows, _ = _run("BIZ_CANCELLED_NO_DELIVERY")
        assert status in ("fail", "warn")
        assert issue_rows == 1

    def test_valid_order_without_items_is_caught(self, order_factory):
        """有效订单必须有明细，否则「订单量有、收入无」会对不上账。"""
        order_factory(order_status="已完成")
        status, issue_rows, _ = _run("BIZ_VALID_ORDER_HAS_ITEM")
        assert status in ("fail", "warn")
        assert issue_rows >= 1


@pytest.mark.django_db
class TestRunPersists:
    def test_run_all_checks_writes_run_and_results(self):
        """整批校验必须落库：能看历史趋势，能回答「上周还好这周为什么坏了」。"""
        from apps.dq import checks
        from apps.dq.models import DQResult, DQRun

        run = checks.run_all_checks(trigger="manual")
        assert DQRun.objects.filter(pk=run.pk).exists()
        results = list(DQResult.objects.filter(run=run))
        assert len(results) == run.total_checks
        assert all(r.rule is not None for r in results), "结果行必须挂上规则，否则成为死信"

    def test_healthy_flag_reflects_error_level_failures(self, order_factory):
        """error 级失败必须让整体不健康（CI 靠这个信号失败）。"""
        from apps.dq import checks

        order = order_factory(final_amount=Decimal("99999.00"))
        run = checks.run_all_checks(trigger="ci")
        if run.failed > 0:
            assert run.is_healthy is False
        else:
            assert run.is_healthy is True
        assert order.pk
