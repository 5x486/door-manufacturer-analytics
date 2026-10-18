"""API 视图（v1）。

接口清单与页面模块一一对应，**取数一律走 :mod:`apps.analytics.selectors`**。
这一点是刻意的：页面与接口共用同一批函数，才能保证「看板上的数字」与
「接口返回的数字」永远是同一个数，，分析系统最常见的信任崩塌，
就是业务在页面上看到一个数、在接口里看到另一个数。

三个贯穿全文件的约定
--------------------
1. **统一响应包**：所有接口都用 :func:`apps.api.utils.ok` 包装，
   返回 ``{code, message, data, meta}``；``meta`` 里带数据版本、筛选条件与指标口径。
2. **结果缓存**：分析类 GET 接口加 :func:`apps.api.utils.cache_analytics`，
   key 里含「筛选条件摘要 + 数据版本」，，数据重跑即自动失效，不会返回旧数。
3. **降级而非失败**：预聚合表（``dws_*``）是分阶段构建的，取数失败是部署期常态。
   每个接口都用 ``try/except`` 包住，失败时返回 ``code=50000`` 与中文提示，
   并把原因写进 ``meta.warnings``，，让调用方看到「为什么没有数」，而不是一个空对象。
"""

from __future__ import annotations

import logging

from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status as drf_status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.analytics.selectors import (
    get_after_sales_analysis,
    get_cashflow_weekly,
    get_category_structure,
    get_channel_performance,
    get_churn_risk_list,
    get_cohort_matrix,
    get_customer_360,
    get_customer_segments,
    get_data_quality_summary,
    get_delivery_performance,
    get_kpi_summary,
    get_model_overview,
    get_monthly_trend,
    get_profit_by_sku,
    get_profit_quadrant,
    get_receivables,
    get_rep_ranking,
    get_stage_bottleneck,
    get_store_comparison,
    get_valid_orders,
)
from apps.analytics.services.simulator import historical_backtest, run_simulation
from apps.api.pagination import MAX_PAGE_SIZE
from apps.api.utils import (
    CODE_SERVER_ERROR,
    cache_analytics,
    data_version_token,
    ok,
    resolve_filters,
)

logger = logging.getLogger(__name__)

__all__ = ["FRAMEWORK_FILTER_PARAMETERS"]

# 所有分析接口共用的筛选参数（与 AnalyticsFilters.from_request 的解析入口一一对应）。
# 抽成常量是因为「同一个筛选参数在 20 个接口上的描述必须一致」——
# 复制粘贴 20 遍必然会有一处写错，而写错的那一处会被前端当成另一套语义。
FRAMEWORK_FILTER_PARAMETERS = [
    OpenApiParameter("period", OpenApiTypes.STR, OpenApiParameter.QUERY,
                     description="期间：`2025`（整年）或 `2025-03`（整月）", required=False),
    OpenApiParameter("years", OpenApiTypes.STR, OpenApiParameter.QUERY,
                     description="年份列表，逗号分隔，例如 `2024,2025`", required=False),
    OpenApiParameter("date_from", OpenApiTypes.DATE, OpenApiParameter.QUERY,
                     description="自定义起始日期（与 period 取交集，更严格的一端生效）", required=False),
    OpenApiParameter("date_to", OpenApiTypes.DATE, OpenApiParameter.QUERY,
                     description="自定义结束日期", required=False),
    OpenApiParameter("channels", OpenApiTypes.STR, OpenApiParameter.QUERY,
                     description="渠道列表，逗号分隔（空表示不限）", required=False),
    OpenApiParameter("categories", OpenApiTypes.STR, OpenApiParameter.QUERY,
                     description="品类列表，逗号分隔（传英文 key，如 `copper`）", required=False),
    OpenApiParameter("customer_types", OpenApiTypes.STR, OpenApiParameter.QUERY,
                     description="客户类型列表，逗号分隔", required=False),
    OpenApiParameter("order_types", OpenApiTypes.STR, OpenApiParameter.QUERY,
                     description="订单类型列表，逗号分隔", required=False),
    OpenApiParameter("stores", OpenApiTypes.STR, OpenApiParameter.QUERY,
                     description="门店编码或门店名列表，逗号分隔", required=False),
    OpenApiParameter("segments", OpenApiTypes.STR, OpenApiParameter.QUERY,
                     description="RFM 客群列表，逗号分隔", required=False),
]

# 常见错误响应说明（挂在每个接口上，避免每个装饰器复制一遍）
ERROR_RESPONSES = {500: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT}


class AnalyticsAPIView(APIView):
    """分析类接口基类。

    只做三件事：解析筛选条件、统一异常降级、暴露 ``view_name`` 供缓存 key 使用。
    真正的取数逻辑写在各自的 ``get`` 里，，刻意不做「通用 selectors 分发器」，
    那种设计会让接口清单与 selectors 的对应关系隐式化，
    半年后没人知道 ``/kpi/`` 到底调了哪个函数。
    """

    #: 指标口径：子类覆盖为本次返回涉及的指标编码，会写进 ``meta.metric_definitions``
    metric_codes: list = []

    def build_filters(self, request):
        """解析筛选条件（失败时退回全量，绝不因筛选参数写错而 500）。"""
        return resolve_filters(request)

    def fail(self, label, exc, request=None):
        """降级响应：返回统一错误结构 + 中文原因 + 数据版本。"""
        logger.warning("接口降级 [%s]：%s: %s", label, type(exc).__name__, exc)
        return Response(
            {
                "code": CODE_SERVER_ERROR,
                "message": "「{}」暂时无法计算（{}），请稍后重试或检查预聚合表是否已构建".format(
                    label, type(exc).__name__
                ),
                "data": None,
                "meta": {
                    "data_version": data_version_token(),
                    "warnings": ["{} 取数失败，已降级返回空结果".format(label)],
                },
            },
            status=drf_status.HTTP_200_OK,
        )

    def cached_ok(self, request, data, label, metric_codes=None, warnings=None):
        """统一成功响应：把口径与筛选条件一起返回。"""
        if metric_codes is None:
            metric_codes = self.metric_codes
        return ok(data, request=request, metric_codes=metric_codes, warnings=warnings)


# ---------------------------------------------------------------------------
# 一、经营总览
# ---------------------------------------------------------------------------
class KpiView(AnalyticsAPIView):
    """成交额、订单量、客单价、毛利额、毛利率、准时率、回款额、目标达成与同比环比。"""

    metric_codes = [
        "M_OV_001", "M_OV_002", "M_OV_003", "M_OV_004", "M_OV_005",
        "M_OV_006", "M_OV_007", "M_OV_008", "M_CW_001", "M_DS_001",
    ]

    @extend_schema(
        tags=["kpi"],
        summary="经营总览 KPI",
        description=(
            "返回统计期的核心经营指标。金额单位：元；比率取值 0~1（展示时乘 100）。\n\n"
            "注意两点口径：\n"
            "1. `revenue`（成交额）与 `collection_amount`（回款额）分属两个时间轴"
            "（订单日 vs 付款日），不应相除后解释为「收款进度」；\n"
            "2. `on_time_rate` 的分母只含已交付订单，`delivery_sample_orders` 可用来判断样本量是否足够。"
        ),
        parameters=FRAMEWORK_FILTER_PARAMETERS,
        responses=ERROR_RESPONSES,
    )
    @cache_analytics()
    def get(self, request):
        filters = self.build_filters(request)
        try:
            data = get_kpi_summary(filters)
        except Exception as exc:  # pragma: no cover
            return self.fail("经营总览指标", exc, request)
        warnings = ["预聚合表未构建，本次为实时计算口径"] if data.get("source") == "fact" else None
        return self.cached_ok(request, data, "经营总览指标", warnings=warnings)


# ---------------------------------------------------------------------------
# 二、增长与渠道
# ---------------------------------------------------------------------------
class TrendView(AnalyticsAPIView):
    """月度/季度/年度趋势序列（可叠加去年同期）。"""

    metric_codes = ["M_OV_001", "M_OV_002", "M_OV_003", "M_OV_004", "M_OV_005"]

    @extend_schema(
        tags=["growth"],
        summary="趋势序列",
        description=(
            "返回按期间升序排列的趋势数组，每项含 `period` / `label` 与全部可选指标值，"
            "以及 `previous_revenue`（去年同期）与 `yoy`（同比，0~1）。\n\n"
            "`metric` 参数只决定每项里的 `metric_value` 取哪一个指标，"
            "**不裁剪其它字段**，，这样一张图切换指标时不必重新请求。"
        ),
        parameters=FRAMEWORK_FILTER_PARAMETERS + [
            OpenApiParameter("metric", OpenApiTypes.STR, OpenApiParameter.QUERY,
                             description="主指标：revenue / gross_profit / order_count / customer_count / avg_order_value",
                             required=False, enum=["revenue", "gross_profit", "order_count", "customer_count", "avg_order_value"]),
            OpenApiParameter("grain", OpenApiTypes.STR, OpenApiParameter.QUERY,
                             description="粒度：month（默认）/ quarter / year",
                             required=False, enum=["month", "quarter", "year"]),
        ],
        responses=ERROR_RESPONSES,
    )
    @cache_analytics()
    def get(self, request):
        filters = self.build_filters(request)
        metric = request.GET.get("metric") or "revenue"
        grain = request.GET.get("grain") or "month"
        if metric not in ("revenue", "gross_profit", "order_count", "customer_count", "avg_order_value"):
            metric = "revenue"
        if grain not in ("month", "quarter", "year"):
            grain = "month"
        try:
            data = get_monthly_trend(filters, metric=metric, grain=grain)
        except Exception as exc:  # pragma: no cover
            return self.fail("趋势序列", exc, request)
        return self.cached_ok(request, {"items": data, "metric": metric, "grain": grain}, "趋势序列")


class ChannelView(AnalyticsAPIView):
    """渠道绩效：成交额、毛利、获客成本与 ROI。"""

    metric_codes = ["M_GR_001", "M_GR_002", "M_GR_003", "M_GR_004", "M_GR_005"]

    @extend_schema(
        tags=["growth"],
        summary="渠道绩效",
        description=(
            "按渠道返回成交额、毛利额、客单价、占比、新增客户数、获客成本与 ROI。\n\n"
            "**重要口径提示**：`marketing_cost` 与 `cost_per_customer` 是估算值，，"
            "按「新增客户 × 3 条线索 × 渠道线索单价（constants）」推算，"
            "真实系统应接入渠道费用台账。字段 `caveat` 里会原样返回这条提示，"
            "前端应当把它展示给用户，避免估算值被当作财务数据引用。"
        ),
        parameters=FRAMEWORK_FILTER_PARAMETERS,
        responses=ERROR_RESPONSES,
    )
    @cache_analytics()
    def get(self, request):
        filters = self.build_filters(request)
        try:
            data = get_channel_performance(filters)
        except Exception as exc:  # pragma: no cover
            return self.fail("渠道绩效", exc, request)
        return self.cached_ok(request, {"items": data}, "渠道绩效")


# ---------------------------------------------------------------------------
# 三、品类与利润
# ---------------------------------------------------------------------------
class CategoryView(AnalyticsAPIView):
    """品类结构：收入、五项成本、毛利与单位面积造价。"""

    metric_codes = ["M_CP_002", "M_CP_003", "M_CP_004", "M_CP_009"]

    @extend_schema(
        tags=["profit"],
        summary="品类收入与成本结构",
        description=(
            "按品类返回成交额、五项成本（材料/人工/制造/物流/安装）、毛利额与毛利率，"
            "以及 `cr3_basis`（收入前三品类的集中度）。\n\n"
            "`unit_price_per_m2`（元/㎡）在 DWS 预聚合路径下无法计算（预聚合表没有面积列），"
            "此时返回 `null` 而不是 0，，0 会被误读成「每平米造价为零」。"
        ),
        parameters=FRAMEWORK_FILTER_PARAMETERS,
        responses=ERROR_RESPONSES,
    )
    @cache_analytics()
    def get(self, request):
        filters = self.build_filters(request)
        try:
            data = get_category_structure(filters)
        except Exception as exc:  # pragma: no cover
            return self.fail("品类结构", exc, request)
        return self.cached_ok(request, {"items": data}, "品类结构")


class ProfitSkuView(AnalyticsAPIView):
    """SKU 毛利排名（同时给出收入榜，两榜差异本身就是结论）。"""

    metric_codes = ["M_CP_004", "M_CP_006", "M_CP_007"]

    @extend_schema(
        tags=["profit"],
        summary="SKU 毛利排名",
        description=(
            "返回 `by_profit`（按毛利额降序）与 `by_revenue`（按成交额降序）两个榜。\n\n"
            "两个榜的差异是核心信息：卖得最多的不一定最赚钱。"
            "`profit_rank` 字段给出该 SKU 在毛利榜上的名次，便于与收入榜名次做差。"
        ),
        parameters=FRAMEWORK_FILTER_PARAMETERS + [
            OpenApiParameter("limit", OpenApiTypes.INT, OpenApiParameter.QUERY,
                             description="每个榜返回的条数，默认 20，上限 200", required=False),
        ],
        responses=ERROR_RESPONSES,
    )
    @cache_analytics()
    def get(self, request):
        filters = self.build_filters(request)
        try:
            limit = int(request.GET.get("limit") or 20)
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(limit, 200))
        try:
            data = get_profit_by_sku(filters, limit=limit)
        except Exception as exc:  # pragma: no cover
            return self.fail("SKU 毛利排名", exc, request)
        return self.cached_ok(request, data, "SKU 毛利排名")


class ProfitQuadrantView(AnalyticsAPIView):
    """营收占比 × 毛利占比 四象限。"""

    metric_codes = ["M_CP_004", "M_CP_006"]

    @extend_schema(
        tags=["profit"],
        summary="SKU 利润四象限",
        description=(
            "按「营收占比 vs 毛利占比」把 SKU 分到 star / volume / niche / drag 四个象限。\n\n"
            "分界线取**平均占比**（即 1 ÷ SKU 数），而不是中位数：四象限的语义就是"
            "「高于/低于平均贡献」，用中位数会让象限划分随样本数漂移，不利于跨期比较。"
            "分界线数值在 `thresholds` 里返回，前端画参考线时应当直接用它，不要自己算。"
        ),
        parameters=FRAMEWORK_FILTER_PARAMETERS,
        responses=ERROR_RESPONSES,
    )
    @cache_analytics()
    def get(self, request):
        filters = self.build_filters(request)
        try:
            data = get_profit_quadrant(filters)
        except Exception as exc:  # pragma: no cover
            return self.fail("利润四象限", exc, request)
        return self.cached_ok(request, data, "利润四象限")


# ---------------------------------------------------------------------------
# 四、客户价值与留存
# ---------------------------------------------------------------------------
class CustomerSegmentsView(AnalyticsAPIView):
    """RFM 客群分布。"""

    metric_codes = ["M_CV_001", "M_CV_002"]

    @extend_schema(
        tags=["customer"],
        summary="RFM 客群分布",
        description=(
            "按 `Customer.segment` 统计各客群的人数、累计消费、人均消费与消费占比。\n\n"
            "口径：客群来自 `Customer.segment`（由 ml 应用定期回写），本接口**只统计不重新判定**，，"
            "保证「看板上的分群」与「营销名单」永远一致。`avg_recency_days` 在无样本时为 `null`。"
        ),
        parameters=FRAMEWORK_FILTER_PARAMETERS,
        responses=ERROR_RESPONSES,
    )
    @cache_analytics()
    def get(self, request):
        filters = self.build_filters(request)
        try:
            data = get_customer_segments(filters)
        except Exception as exc:  # pragma: no cover
            return self.fail("客户分群", exc, request)
        return self.cached_ok(request, {"items": data}, "客户分群")


class CustomerCohortView(AnalyticsAPIView):
    """客户留存队列矩阵。"""

    metric_codes = ["M_CV_005"]

    @extend_schema(
        tags=["customer"],
        summary="客户留存队列矩阵",
        description=(
            "返回 `cohorts`（每个队列的留存率序列）、`average_retention`（平均留存曲线）与 `max_index`。\n\n"
            "**关键口径**：`retention` 数组里的 `null` 表示「该队列还没到这个月」，"
            "不是「留存率为 0」。前端画热力图时必须跳过 `null` 而不是填 0，，"
            "把未来画成归零是留存分析最常见的可视化错误。"
        ),
        parameters=FRAMEWORK_FILTER_PARAMETERS + [
            OpenApiParameter("max_index", OpenApiTypes.INT, OpenApiParameter.QUERY,
                             description="最大观察月数，默认 12", required=False),
        ],
        responses=ERROR_RESPONSES,
    )
    @cache_analytics()
    def get(self, request):
        filters = self.build_filters(request)
        try:
            max_index = int(request.GET.get("max_index") or 12)
        except (TypeError, ValueError):
            max_index = 12
        max_index = max(1, min(max_index, 36))
        try:
            data = get_cohort_matrix(filters, max_index=max_index)
        except Exception as exc:  # pragma: no cover
            return self.fail("留存矩阵", exc, request)
        return self.cached_ok(request, data, "留存矩阵")


class CustomerChurnView(AnalyticsAPIView):
    """流失预警名单（规则法）。"""

    metric_codes = ["M_CV_006", "M_CV_004"]

    @extend_schema(
        tags=["customer"],
        summary="流失预警名单",
        description=(
            "判定规则：`monetary` 在全体客户 Top 30% 之内，且 `recency_days` > 平均复购间隔 × 2。\n\n"
            "为什么用规则而不是模型：规则可解释、可复现，业务拿到名单就能直接打电话，"
            "并能说清「为什么他在名单里」。模型（`/model/`）用于更精细的排序，"
            "两者冲突时以规则为准做触达。`reason` 字段是可直接展示给销售的中文解释。"
        ),
        parameters=FRAMEWORK_FILTER_PARAMETERS + [
            OpenApiParameter("limit", OpenApiTypes.INT, OpenApiParameter.QUERY,
                             description="返回条数，默认 100，上限 500", required=False),
        ],
        responses=ERROR_RESPONSES,
    )
    @cache_analytics()
    def get(self, request):
        filters = self.build_filters(request)
        try:
            limit = int(request.GET.get("limit") or 100)
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 500))
        try:
            data = get_churn_risk_list(filters, limit=limit)
        except Exception as exc:  # pragma: no cover
            return self.fail("流失预警", exc, request)
        return self.cached_ok(request, {"items": data, "count": len(data)}, "流失预警")


class Customer360View(AnalyticsAPIView):
    """单客户全景（档案 / 价值 / 订单 / 付款 / 售后 / 时间线 / 风险）。"""

    metric_codes = ["M_CV_001", "M_CV_006", "M_CW_001", "M_CW_003"]

    @extend_schema(
        tags=["customer"],
        summary="客户 360",
        description=(
            "返回 6 个区块：`profile` / `value_summary` / `orders` / `payments` / "
            "`after_sales` / `timeline` / `risk_flags`。\n\n"
            "`timeline` 把订单、交付、付款、售后四类事件按时间倒序合并，，"
            "这是排查「这个客户为什么突然不来了」最有效的一屏，"
            "能看到「交付延期 → 售后工单 → 未再下单」的完整因果链。\n\n"
            "找不到客户时返回 `found=false`（HTTP 200），而不是 404："
            "对前端来说「客户不存在」是一个正常的业务结果，不是异常。\n\n"
            "**隐私**：`profile.phone` 为明文，仅限有权限的登录会话；"
            "对外提供时应替换为脱敏字段。"
        ),
        parameters=[],
        responses=ERROR_RESPONSES,
    )
    @cache_analytics()
    def get(self, request, customer_code):
        try:
            data = get_customer_360(customer_code)
        except Exception as exc:  # pragma: no cover
            return self.fail("客户 360", exc, request)
        if not data or not data.get("found"):
            return ok(
                data or {"found": False, "customer_code": customer_code},
                request=request,
                metric_codes=self.metric_codes,
                warnings=["未找到客户编码 {}".format(customer_code)],
                message="客户不存在",
            )
        return self.cached_ok(request, data, "客户 360")


# ---------------------------------------------------------------------------
# 五、交付与售后
# ---------------------------------------------------------------------------
class DeliveryView(AnalyticsAPIView):
    """交付绩效总览。"""

    metric_codes = ["M_DS_001", "M_DS_002", "M_DS_003"]

    @extend_schema(
        tags=["delivery"],
        summary="交付绩效",
        description=(
            "返回准时率、平均/中位/最长交付周期、延期订单数与延期原因分布。\n\n"
            "**口径**：准时率的分母只含 `is_on_time in (0, 1)` 的订单（即已交付订单），"
            "在途订单不计入，，把在途算进分母会让准时率随订单积压虚假下降。"
            "`sample_orders` 字段用于判断样本量是否足以支撑结论。"
        ),
        parameters=FRAMEWORK_FILTER_PARAMETERS,
        responses=ERROR_RESPONSES,
    )
    @cache_analytics()
    def get(self, request):
        filters = self.build_filters(request)
        try:
            data = get_delivery_performance(filters)
        except Exception as exc:  # pragma: no cover
            return self.fail("交付绩效", exc, request)
        return self.cached_ok(request, data, "交付绩效")


class DeliveryBottleneckView(AnalyticsAPIView):
    """交付阶段瓶颈排名。"""

    metric_codes = ["M_DS_004", "M_DS_005"]

    @extend_schema(
        tags=["delivery"],
        summary="交付阶段瓶颈",
        description=(
            "按 `bottleneck_score`（= 阶段耗时占比 × 超时率）降序返回各交付阶段。\n\n"
            "用乘积而不是「耗时」单一维度：耗时长但不超时的阶段（例如生产本身就需要时间）"
            "不是瓶颈；只有**又慢又常超时**的阶段才值得投资源去改进。"
        ),
        parameters=FRAMEWORK_FILTER_PARAMETERS,
        responses=ERROR_RESPONSES,
    )
    @cache_analytics()
    def get(self, request):
        filters = self.build_filters(request)
        try:
            data = get_stage_bottleneck(filters)
        except Exception as exc:  # pragma: no cover
            return self.fail("阶段瓶颈", exc, request)
        return self.cached_ok(request, {"items": data}, "阶段瓶颈")


class AfterSalesView(AnalyticsAPIView):
    """售后分析。"""

    metric_codes = ["M_DS_006", "M_DS_007"]

    @extend_schema(
        tags=["delivery"],
        summary="售后分析",
        description=(
            "返回售后发生率、售后总成本与成本率，以及按类型 / 品类 / 状态 / 责任方的分布。\n\n"
            "成本率的分母用**同筛选条件下的成交额**，而不是全公司成交额，，"
            "否则按品类筛选时，成本率会被其他品类的收入摊薄。\n"
            "`quality_event_ratio` 只统计质量类售后（尺寸误差、门体变形、五金损坏、色差），"
            "把「售后咨询」这类非质量问题排除在外。"
        ),
        parameters=FRAMEWORK_FILTER_PARAMETERS,
        responses=ERROR_RESPONSES,
    )
    @cache_analytics()
    def get(self, request):
        filters = self.build_filters(request)
        try:
            data = get_after_sales_analysis(filters)
        except Exception as exc:  # pragma: no cover
            return self.fail("售后分析", exc, request)
        return self.cached_ok(request, data, "售后分析")


# ---------------------------------------------------------------------------
# 六、回款与现金流
# ---------------------------------------------------------------------------
class CashflowView(AnalyticsAPIView):
    """应收账款：账龄分布与明细。"""

    metric_codes = ["M_CW_003", "M_CW_004", "M_CW_005", "M_CW_006"]

    @extend_schema(
        tags=["cashflow"],
        summary="应收与账龄",
        description=(
            "返回应收余额、逾期额与占比、90 天以上金额、客户数，以及四个账龄桶与明细清单。\n\n"
            "账龄一律以 `settings.DATA_AS_OF_DATE` 为切点，响应里的 `as_of_date` 就是该切点，，"
            "脱离切点的账龄没有意义，前端必须把 `as_of_date` 展示出来。\n"
            "`aging_bucket` 取值：`0_30` / `31_60` / `61_90` / `over_90`，"
            "四个桶在响应里**始终齐全**（无数据时金额为 0），便于前端画固定四段的堆叠柱。"
        ),
        parameters=FRAMEWORK_FILTER_PARAMETERS + [
            OpenApiParameter("bucket", OpenApiTypes.STR, OpenApiParameter.QUERY,
                             description="只返回包含该账龄桶的客户明细",
                             required=False, enum=["0_30", "31_60", "61_90", "over_90"]),
            OpenApiParameter("limit", OpenApiTypes.INT, OpenApiParameter.QUERY,
                             description="客户明细条数，默认 200，上限 1000", required=False),
        ],
        responses=ERROR_RESPONSES,
    )
    @cache_analytics()
    def get(self, request):
        filters = self.build_filters(request)
        bucket = request.GET.get("bucket") or None
        try:
            limit = int(request.GET.get("limit") or 200)
        except (TypeError, ValueError):
            limit = 200
        limit = max(1, min(limit, 1000))
        try:
            data = get_receivables(filters, bucket=bucket, limit=limit)
        except Exception as exc:  # pragma: no cover
            return self.fail("应收账款", exc, request)
        return self.cached_ok(request, data, "应收账款")


class CashflowForecastView(AnalyticsAPIView):
    """未来 N 周回款预测。"""

    metric_codes = ["M_CW_007"]

    @extend_schema(
        tags=["cashflow"],
        summary="回款预测（按周）",
        description=(
            "基于历史回款行为分布的滚动预测。**三条假设必须随结果一起展示给用户**：\n\n"
            "1. 应收余额在未来 N 周内按恒定周回收率回笼（回收率 = 过去 13 周实际回款 ÷ 期末应收余额，"
            "并收敛到 1.5%~8% 的合理区间）；\n"
            "2. 忽略客户经营突变、催收加强与季节性（门业 12 月~次年 1 月回款偏慢）；\n"
            "3. 只含**已发生的应收**，不含未来新签合同的回款。\n\n"
            "它是排产与备料的现金参考，不是财务预算数字。"
        ),
        parameters=FRAMEWORK_FILTER_PARAMETERS + [
            OpenApiParameter("weeks", OpenApiTypes.INT, OpenApiParameter.QUERY,
                             description="预测周数，默认 13，上限 52", required=False),
        ],
        responses=ERROR_RESPONSES,
    )
    @cache_analytics()
    def get(self, request):
        filters = self.build_filters(request)
        try:
            weeks = int(request.GET.get("weeks") or 13)
        except (TypeError, ValueError):
            weeks = 13
        weeks = max(1, min(weeks, 52))
        try:
            data = get_cashflow_weekly(filters, weeks=weeks)
        except Exception as exc:  # pragma: no cover
            return self.fail("回款预测", exc, request)
        total = sum(float(row.get("expected_amount") or 0) for row in data)
        return self.cached_ok(
            request,
            {
                "items": data,
                "weeks": weeks,
                "total_expected": round(total, 2),
                "assumptions": [
                    "恒定周回收率：过去 13 周实际回款 ÷ 期末应收余额，收敛到 1.5%~8%",
                    "忽略客户经营突变、催收加强与季节性（12 月至次年 1 月回款偏慢）",
                    "只含已发生的应收，不含未来新签合同的回款",
                ],
            },
            "回款预测",
        )


# ---------------------------------------------------------------------------
# 七、人效与门店
# ---------------------------------------------------------------------------
class WorkforceView(AnalyticsAPIView):
    """人效：业务员排名 + 门店对比。"""

    metric_codes = ["M_WF_001", "M_WF_002", "M_WF_005", "M_WF_006"]

    @extend_schema(
        tags=["workforce"],
        summary="人效与门店",
        description=(
            "一次返回两个区块：`reps`（业务员业绩排名）与 `stores`（门店对比）。\n\n"
            "合并为一个接口是因为它们**必须一起看**：只看业务员排名会忽略门店供给差异，"
            "只看门店对比会把「人效」误读成「店效」。\n\n"
            "`revenue_per_rep`（门店人均成交额）= 门店成交额 ÷ 该门店业务员数，"
            "用于剔除门店规模差异；业务员数为 0 时返回 `null` 而不是 0。"
        ),
        parameters=FRAMEWORK_FILTER_PARAMETERS + [
            OpenApiParameter("limit", OpenApiTypes.INT, OpenApiParameter.QUERY,
                             description="业务员排名条数，默认不限（返回全部）", required=False),
        ],
        responses=ERROR_RESPONSES,
    )
    @cache_analytics()
    def get(self, request):
        filters = self.build_filters(request)
        raw_limit = request.GET.get("limit")
        limit = None
        if raw_limit:
            try:
                limit = max(1, min(int(raw_limit), 200))
            except (TypeError, ValueError):
                limit = None
        try:
            reps = get_rep_ranking(filters, limit=limit)
            stores = get_store_comparison(filters)
        except Exception as exc:  # pragma: no cover
            return self.fail("人效与门店", exc, request)
        return self.cached_ok(request, {"reps": reps, "stores": stores}, "人效与门店")


# ---------------------------------------------------------------------------
# 八、决策模拟
# ---------------------------------------------------------------------------
class SimulateView(AnalyticsAPIView):
    """What-if 决策模拟（POST）。"""

    metric_codes = ["M_OV_001", "M_OV_004", "M_OV_005", "M_CP_009", "M_DS_001"]

    @extend_schema(
        tags=["simulate"],
        summary="决策模拟（what-if）",
        description=(
            "用可解释的弹性公式推演「调价 / 让利 / 材料涨价 / 延期 / 加投放」对毛利的影响。\n\n"
            "请求体（全部可选，缺省为 0）：\n"
            "- `price_change_pct`：整体价格变动 %（-15~15）\n"
            "- `volume_change_pct`：销量外生变动 %（-30~30）\n"
            "- `discount_change_pct`：折扣政策变动 %（-5~5，正数＝多让利）\n"
            "- `material_cost_change_pct`：材料成本变动 %（-20~30）\n"
            "- `delivery_delay_days`：平均交付延期天数（0~30）\n"
            "- `new_customer_target`：新增客户目标数（0~300）\n"
            "- `channel_budget_shift`：展会预算转向抖音的比例 %（0~100）\n\n"
            "响应含 `baseline` / `scenario` / `delta` / `waterfall`（瀑布归因）/ "
            "`sensitivity`（单参数敏感性）/ `assumptions`（假设清单，必须展示）/ `conclusion`。\n\n"
            "**设计原则**：所有假设都出现在 `assumptions` 里（名称/取值/来源），"
            "因为一个不给假设的模拟器等于半个算命先生。"
        ),
        parameters=FRAMEWORK_FILTER_PARAMETERS,
        request=None,
        responses=ERROR_RESPONSES,
    )
    def post(self, request):
        filters = self.build_filters(request)
        payload = request.data if hasattr(request, "data") else {}
        try:
            result = run_simulation(payload, filters)
        except Exception as exc:  # pragma: no cover
            return self.fail("决策模拟", exc, request)
        return ok(result, request=request, metric_codes=self.metric_codes)


# ---------------------------------------------------------------------------
# 九、数据质量 / 模型 / 指标字典
# ---------------------------------------------------------------------------
class DqView(AnalyticsAPIView):
    """最近一次数据质量校验的摘要。"""

    @extend_schema(
        tags=["dq"],
        summary="数据质量摘要",
        description=(
            "返回最近一次 `run_all_checks` 的结果：通过 / 警告 / 失败计数、"
            "执行耗时、是否健康，以及逐条规则的结果明细。\n\n"
            "没有任何校验记录时返回 `has_run=false`，**而不是一堆 0**，，"
            "0 会被误读成「完美通过」，而真实情况是「根本还没跑过」。"
            "`sample_keys` 是问题样本的业务主键，最多 10 条。"
        ),
        parameters=[],
        responses=ERROR_RESPONSES,
    )
    @cache_analytics(timeout=60)
    def get(self, request):
        try:
            data = get_data_quality_summary()
        except Exception as exc:  # pragma: no cover
            return self.fail("数据质量摘要", exc, request)
        return self.cached_ok(request, data, "数据质量摘要")


class ModelView(AnalyticsAPIView):
    """模型在线情况。"""

    @extend_schema(
        tags=["ml"],
        summary="模型在线情况",
        description=(
            "返回各任务模型的版本列表（算法 / 训练窗口 / 样本量 / AUC / MAE / MAPE / R² / 基线对比）、"
            "当前在线版本，以及最近 200 条评分结果（含 Top 因子与人话解释）。\n\n"
            "`feature_importance` 可能是 dict 或 list 两种形态（取决于训练脚本的落库方式），"
            "调用方应兼容两种写法。"
        ),
        parameters=[],
        responses=ERROR_RESPONSES,
    )
    @cache_analytics(timeout=120)
    def get(self, request):
        try:
            data = get_model_overview()
        except Exception as exc:  # pragma: no cover
            return self.fail("模型概览", exc, request)
        return self.cached_ok(request, data, "模型概览")


class MetricDictionaryView(AnalyticsAPIView):
    """指标口径字典。"""

    @extend_schema(
        tags=["meta"],
        summary="指标口径字典",
        description=(
            "返回全部指标的口径定义（编码 / 名称 / 域 / 类型 / 业务定义 / 公式 / 分子分母 / "
            "单位 / 来源表 / 易误读提醒 / 版本），以及指标拆解树 `METRIC_TREE_SPEC`。\n\n"
            "本接口是「元数据接口」：任何分析接口返回的数字，都可以用它按 `code` 反查口径。"
            "建议前端把结果缓存到本地（口径很少变动），并用它渲染各页面的口径提示。\n\n"
            "支持按域或关键词过滤：`?domain=品类利润`、`?q=毛利率`。"
        ),
        parameters=[
            OpenApiParameter("domain", OpenApiTypes.STR, OpenApiParameter.QUERY,
                             description="按指标域过滤，例如 `品类利润`", required=False),
            OpenApiParameter("q", OpenApiTypes.STR, OpenApiParameter.QUERY,
                             description="按编码 / 名称 / 定义模糊搜索", required=False),
        ],
        responses=ERROR_RESPONSES,
    )
    @cache_analytics(timeout=3600)
    def get(self, request):
        try:
            from apps.analytics.metrics import METRIC_DEFINITIONS, METRIC_TREE_SPEC

            domain = request.GET.get("domain") or ""
            keyword = (request.GET.get("q") or "").strip().lower()
            items = list(METRIC_DEFINITIONS)
            if domain:
                items = [item for item in items if item.get("domain") == domain]
            if keyword:
                items = [
                    item for item in items
                    if keyword in str(item.get("code", "")).lower()
                    or keyword in str(item.get("name", "")).lower()
                    or keyword in str(item.get("definition", "")).lower()
                ]
            domains = []
            for item in METRIC_DEFINITIONS:
                name = item.get("domain")
                if name and name not in domains:
                    domains.append(name)
            data = {
                "items": items,
                "count": len(items),
                "domains": domains,
                "tree": METRIC_TREE_SPEC,
            }
        except Exception as exc:  # pragma: no cover
            return self.fail("指标字典", exc, request)
        return self.cached_ok(request, data, "指标字典")


# ---------------------------------------------------------------------------
# 十、订单明细（下钻用）
# ---------------------------------------------------------------------------
class OrderListView(AnalyticsAPIView):
    """订单明细分页（看板下钻的统一入口）。

    图表下钻（点击柱子 → 看这一批订单）全部走本接口：
    这样「柱子的高度」与「下钻出来的明细行」必然能对上，
    不会出现「点了某个渠道，明细里混进了别的渠道订单」。
    """

    @extend_schema(
        tags=["meta"],
        summary="订单明细分页",
        description=(
            "按筛选条件分页返回有效订单（已排除「已取消」）。\n\n"
            "支持的排序字段（`ordering`）：`order_no` / `order_date` / `final_amount` / "
            "`lead_days` / `customer`，加 `-` 前缀表示降序。\n"
            "`page_size` 默认 50，上限 {}。\n\n"
            "**本接口不返回客户电话与成本字段**：明细接口是下钻用的，"
            "成本数据应通过 `customer/<code>/360/` 或成本分析接口获取，"
            "避免在通用列表接口里泄漏内部经营数据。"
        ).format(MAX_PAGE_SIZE),
        parameters=FRAMEWORK_FILTER_PARAMETERS + [
            OpenApiParameter("ordering", OpenApiTypes.STR, OpenApiParameter.QUERY,
                             description="排序字段，前缀 `-` 为降序，默认 `-order_date`", required=False),
            OpenApiParameter("page", OpenApiTypes.INT, OpenApiParameter.QUERY,
                             description="页码，从 1 开始", required=False),
            OpenApiParameter("page_size", OpenApiTypes.INT, OpenApiParameter.QUERY,
                             description="每页条数，默认 50，上限 {}".format(MAX_PAGE_SIZE), required=False),
        ],
        responses=ERROR_RESPONSES,
    )
    def get(self, request):
        filters = self.build_filters(request)
        ordering = (request.GET.get("ordering") or "-order_date").strip()
        allowed = {
            "order_no", "-order_no", "order_date", "-order_date",
            "final_amount", "-final_amount", "lead_days", "-lead_days",
            "customer", "-customer",
        }
        if ordering not in allowed:
            ordering = "-order_date"
        try:
            page_size = int(request.GET.get("page_size") or 50)
        except (TypeError, ValueError):
            page_size = 50
        page_size = max(1, min(page_size, MAX_PAGE_SIZE))
        page_number = request.GET.get("page") or 1

        try:
            queryset = get_valid_orders(filters)
            queryset = queryset.order_by(
                "customer__customer_name" if ordering == "customer"
                else ("-customer__customer_name" if ordering == "-customer" else ordering)
            )
            paginator = Paginator(queryset, page_size)
            try:
                page = paginator.page(page_number)
            except PageNotAnInteger:
                page = paginator.page(1)
            except EmptyPage:
                page = paginator.page(paginator.num_pages or 1)
        except Exception as exc:  # pragma: no cover
            return self.fail("订单明细", exc, request)

        items = [_order_row(order) for order in page.object_list]
        return ok(
            {
                "items": items,
                "page": page.number,
                "page_size": page_size,
                "total": paginator.count,
                "total_pages": paginator.num_pages,
                "has_next": page.has_next(),
                "has_previous": page.has_previous(),
                "ordering": ordering,
            },
            request=request,
            metric_codes=["M_OV_001", "M_OV_002", "M_DS_001"],
        )


def _order_row(order) -> dict:
    """订单 → 下钻表格所需的最小字段集。

    刻意**不含**成本与毛利：下钻表格展示在浏览器里，
    成本数据的暴露面越小越好；需要时应走带权限的成本分析接口。
    """
    return {
        "order_no": order.order_no,
        "order_date": order.order_date.isoformat() if order.order_date else None,
        "customer_code": getattr(getattr(order, "customer", None), "customer_code", "") or "",
        "customer_name": getattr(getattr(order, "customer", None), "customer_name", "") or "散客",
        "customer_type": order.customer_type,
        "channel": order.channel,
        "order_type": order.order_type,
        "store_name": getattr(getattr(order, "store", None), "store_name", "") or "",
        "sales_rep_name": getattr(getattr(order, "sales_rep", None), "rep_name", "") or "",
        "dominant_category": order.dominant_category,
        "item_count": order.item_count,
        "final_amount": float(order.final_amount or 0),
        "order_status": order.order_status,
        "is_on_time": order.is_on_time,
        "lead_days": order.lead_days,
        "delay_days": order.delay_days,
    }


class BacktestView(AnalyticsAPIView):
    """模拟器的历史回测（演示「模型有回测，不是拍脑袋」）。"""

    metric_codes = ["M_OV_001", "M_OV_004", "M_OV_005"]

    @extend_schema(
        tags=["simulate"],
        summary="模拟器历史回测",
        description=(
            "用 `base_year` 的参数预测 `target_year` 并与真实结果对比，返回成交额 / 毛利额 / "
            "订单量 / 毛利率的预测值、实际值与误差率。\n\n"
            "回测不能解决「弹性系数从哪来」的问题，但它能回答一个更关键的问题："
            "**这套公式在真实数据上的误差有多大**，，给出误差率，用户才能判断结论是"
            "「可以参考」还是「只能当方向」。"
        ),
        parameters=[
            OpenApiParameter("base_year", OpenApiTypes.INT, OpenApiParameter.QUERY,
                             description="基年，默认 2024", required=False),
            OpenApiParameter("target_year", OpenApiTypes.INT, OpenApiParameter.QUERY,
                             description="目标年，默认 2025", required=False),
        ],
        responses=ERROR_RESPONSES,
    )
    @cache_analytics(timeout=1800)
    def get(self, request):
        try:
            base_year = int(request.GET.get("base_year") or 2024)
            target_year = int(request.GET.get("target_year") or 2025)
        except (TypeError, ValueError):
            base_year, target_year = 2024, 2025
        try:
            data = historical_backtest(base_year, target_year)
        except Exception as exc:  # pragma: no cover
            return self.fail("历史回测", exc, request)
        return self.cached_ok(request, data, "历史回测")


# 说明：指标口径元信息（``meta.metric_definitions``）统一由
# apps.api.utils._build_meta 注入，接口视图只需声明 ``metric_codes``，
# 不要在视图里自己拼 meta —— 口径的注入点只应有一处。
