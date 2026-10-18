# -*- coding: utf-8 -*-
"""
ETL 幂等性测试（面试必问的第二类）
==================================
幂等 = 同一条命令跑两遍，结果必须完全一样。

为什么这条最重要：经营看板最怕的不是「算错」，而是「同样的筛选条件，
今天看是 6,000 万，明天看是 5,998 万」，， 数据团队会因此彻底失去信任。
而幂等性事故的典型来源就是 ETL 用 INSERT 而不是 UPSERT。

这里不做端到端（那需要跑 seed_demo，属于 integration），
而是针对「重建式写入」的核心语义做单元级断言：跑两遍，行数与金额都不变。
"""
from decimal import Decimal

import pytest

from apps.core import constants as C


@pytest.mark.django_db
class TestRebuildIsIdempotent:
    def test_rebuild_business_monthly_twice_gives_same_rows(self, order_factory, item_factory,
                                                           item_cost_factory):
        """DwsBusinessMonthly 重建两遍：行数与金额必须完全一致。"""
        from apps.analytics import etl
        from apps.analytics.models import DwsBusinessMonthly

        for i in range(3):
            order = order_factory(order_date=__import__("datetime").date(2025, 3, 10 + i),
                                  total_amount=Decimal("10000.00"),
                                  discount=Decimal("0.1000"),
                                  freight=Decimal("0"))
            item = item_factory(order, line_amount=order.final_amount,
                                item_no=f"TI{i:06d}")
            item_cost_factory(item, C.CostTypeChoices.MATERIAL.value, "5000.00")

        first = etl.rebuild_business_monthly()
        rows_1 = DwsBusinessMonthly.objects.count()
        revenue_1 = sum(DwsBusinessMonthly.objects.values_list("revenue", flat=True))

        second = etl.rebuild_business_monthly()
        rows_2 = DwsBusinessMonthly.objects.count()
        revenue_2 = sum(DwsBusinessMonthly.objects.values_list("revenue", flat=True))

        assert rows_1 == rows_2, "重建后行数变化说明写入不是幂等的"
        assert revenue_1 == revenue_2, "重建后金额变化说明有重复累加"
        assert first["rows"] == second["rows"]

    def test_dws_tables_are_read_only(self):
        """预聚合表必须拒绝直接写入 ，， 否则「预聚合与明细不一致」会变成常规事故。"""
        from apps.analytics.models import DwsBusinessMonthly

        with pytest.raises(NotImplementedError):
            DwsBusinessMonthly(period="2025-03", year=2025, month=3,
                               channel="抖音短视频", category="铜门").save()

    def test_rebuild_records_data_version(self, order_factory):
        """每次重建都要登记数据版本：缓存靠它失效，排查靠它对齐。"""
        from apps.analytics import etl
        from apps.analytics.models import DataVersion

        order_factory()
        etl.rebuild_business_monthly()
        row = DataVersion.objects.filter(table_name="dws_business_monthly").first()
        assert row is not None
        assert row.layer == "dws"
        assert row.version_token, "没有版本令牌，缓存就失去了失效依据"


@pytest.mark.django_db
class TestCohortRebuild:
    def test_cohort_month_index_zero_is_first_month(self, order_factory):
        """队列口径：month_index=0 表示首单当月，其留存率必然是 1.0。"""
        from apps.analytics import etl
        from apps.analytics.models import DwsCustomerCohort

        order_factory(order_date=__import__("datetime").date(2025, 3, 10))
        etl.rebuild_customer_cohort()

        row = DwsCustomerCohort.objects.filter(cohort_month="2025-03", month_index=0).first()
        assert row is not None
        assert row.customer_count >= 1
        assert row.retention_rate == Decimal("1.0000")


@pytest.mark.django_db
class TestRepurchaseInterval:
    def test_interval_only_from_second_order(self, order_factory):
        """口径：复购间隔只统计「相邻两单之间」，一单客户不产生任何间隔。"""
        import datetime as dt

        from apps.analytics import etl
        from apps.analytics.models import DwsRepurchaseInterval

        order_factory(order_date=dt.date(2025, 1, 10))
        order_factory(order_date=dt.date(2025, 3, 11))   # 间隔 60 天
        etl.rebuild_repurchase_interval()

        total = sum(DwsRepurchaseInterval.objects.values_list("customer_count", flat=True))
        assert total >= 1
        assert DwsRepurchaseInterval.objects.filter(interval_days=60).exists()
