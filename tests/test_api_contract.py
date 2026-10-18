# -*- coding: utf-8 -*-
"""
API 契约测试（面试必问的第四类）
================================
契约测试断言的是「接口的形状」，不是「某个具体数值」。
理由：数值随数据变化，形状一旦变了，前端就会静默错位（拿到 undefined 而不是报错）。
"""
import pytest


@pytest.mark.django_db
class TestResponseEnvelope:
    def test_ok_shape(self):
        """统一响应包：code / message / data / meta 四个键必须都在。"""
        from apps.api.utils import ok

        # ok() 返回 DRF Response（不是 dict），断言必须走 .data。
        # 契约测试的价值就在于把「响应形状被改坏」变成红灯，而不是让前端静默错位。
        body = ok({"x": 1}, metric_codes=["M_OV_001"]).data
        assert set(["code", "message", "data", "meta"]) <= set(body.keys())
        assert body["code"] == 0
        assert body["data"] == {"x": 1}

    def test_meta_carries_data_version(self):
        """meta 必须带数据版本：前端要能显示「这份数是几点几分算出来的」。"""
        from apps.api.utils import ok

        body = ok({"x": 1}).data
        assert "data_version" in body["meta"], "缺少数据版本，缓存失效与排查都无从下手"
        assert body["meta"]["data_version"]

    def test_metric_definitions_attached(self):
        """传了指标编码就必须能在 meta 里查到口径 ，， 这是「口径嵌进产品」的实现方式。"""
        from apps.api.utils import metric_meta

        metas = metric_meta(["M_OV_001"])
        assert isinstance(metas, list)

    def test_jsonable_converts_decimal(self):
        """金额是 Decimal，必须能安全序列化；否则接口会在 JSON 编码阶段炸掉。"""
        from decimal import Decimal

        from apps.api.utils import jsonable

        assert jsonable(Decimal("123.45")) == 123.45
        assert jsonable({"a": Decimal("1.00")}) == {"a": 1.0}
        assert jsonable([Decimal("2.5")]) == [2.5]


@pytest.mark.django_db
class TestApiPages:
    """逐个接口请求，断言状态码与响应形状（不依赖具体数据量）。"""

    ENDPOINTS = [
        "/api/v1/kpi/",
        "/api/v1/trend/",
        "/api/v1/channel/",
        "/api/v1/category/",
        "/api/v1/profit/sku/",
        "/api/v1/customer/segments/",
        "/api/v1/delivery/",
        "/api/v1/cashflow/",
        "/api/v1/workforce/",
        "/api/v1/dq/",
        "/api/v1/model/",
        "/api/v1/metrics/",
    ]

    @pytest.mark.parametrize("url", ENDPOINTS)
    def test_endpoint_returns_200_and_envelope(self, authenticated_client, url):
        resp = authenticated_client.get(url)
        assert resp.status_code == 200, f"{url} 返回 {resp.status_code}"
        body = resp.json()
        assert set(["code", "message", "data", "meta"]) <= set(body.keys()), \
            f"{url} 响应包缺少四要素：{list(body.keys())}"
        if body["code"] != 0:
            # 空库下允许「无数据降级」，但降级必须给出可读原因，否则调用方无从下手
            assert body["message"] or body["meta"].get("warnings"), \
                f"{url} 降级时既没有 message 也没有 warnings"

    def test_anonymous_is_blocked(self, client, db):
        """经营数据含客户联系方式，匿名必须被拦（不是「可读」，是「不可读」）。"""
        resp = client.get("/api/v1/kpi/")
        # DRF SessionAuthentication 无凭据时为 403，也可能 302 跳登录
        assert resp.status_code in (401, 403, 302)

    def test_openapi_schema_is_valid(self, authenticated_client):
        """schema 能生成就说明所有 @extend_schema 的声明没写错。"""
        resp = authenticated_client.get("/api/schema/")
        assert resp.status_code == 200
        assert b"openapi" in resp.content[:400]


@pytest.mark.django_db
class TestAnalyticsFilters:
    def test_empty_list_means_no_filter(self):
        """约定：空列表 = 不过滤（而不是「过滤出空集」）。这条搞错会让页面白屏。"""
        from apps.analytics.services.filters import AnalyticsFilters

        f = AnalyticsFilters.from_mapping({})
        from apps.sales.models import Order

        assert f.apply_orders(Order.objects.all()).count() == Order.objects.count()

    def test_invalid_value_is_dropped(self):
        """非法入参必须被丢弃而不是原样透传 ，， 否则 SQL 里会出现谁也认不出的值。"""
        from apps.analytics.services.filters import AnalyticsFilters

        f = AnalyticsFilters.from_mapping({"channels": "不存在的渠道", "categories": "铜门"})
        assert "不存在的渠道" not in (f.channels or [])
        assert "铜门" in (f.categories or [])

    def test_cache_key_is_stable(self):
        """同样的筛选条件必须给出同样的缓存 key，否则缓存永远命中不了。"""
        from apps.analytics.services.filters import AnalyticsFilters

        a = AnalyticsFilters.from_mapping({"years": "2025", "channels": "抖音短视频"})
        b = AnalyticsFilters.from_mapping({"channels": "抖音短视频", "years": "2025"})
        assert a.cache_key() == b.cache_key()


@pytest.mark.django_db
class TestSimulator:
    def test_zero_params_equals_baseline(self):
        """模拟器最重要的性质：参数全为 0 时，情景必须严格等于基准。"""
        from apps.analytics.services.simulator import run_simulation

        result = run_simulation({})
        assert "baseline" in result
        assert "scenario" in result
        assert "assumptions" in result

        # 不变量：零参数时情景必须逐项等于基准。
        # 它抓到过一个真 bug：material_ratio 在情景里被算成 0.0 而基准是 0.48，
        # 原因是情景缺少分支时给了默认值而不是继承基准，会误导成「成本占比归零」。
        base, scen = result["baseline"], result["scenario"]
        shared = set(base) & set(scen)
        assert shared, "baseline 与 scenario 没有可比字段，返回结构被改坏了"
        for key in sorted(shared):
            if isinstance(base[key], (int, float)) and isinstance(scen[key], (int, float)):
                assert scen[key] == pytest.approx(base[key], rel=1e-6, abs=1e-6), \
                    f"{key} 零参数下与基准不一致：base={base[key]} scenario={scen[key]}"

    def test_assumptions_are_explicit(self):
        """假设必须显式列出 ，， 不给假设的模拟器等于拍脑袋。"""
        from apps.analytics.services.simulator import run_simulation

        result = run_simulation({"price_change_pct": 5})
        assert result["assumptions"], "缺少假设清单"
        assert all("source" in a or "note" in a or "value" in a for a in result["assumptions"])
