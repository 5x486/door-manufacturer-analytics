"""API 路由（v1）。

为什么用显式 ``path`` 而不是 DRF Router
--------------------------------------
``DefaultRouter`` 的语义是「资源 + CRUD 动作」：它会为每个视图集生成
``GET /resource/``、``POST /resource/``、``GET /resource/{pk}/``、
``PUT/PATCH/DELETE /resource/{pk}/`` 一整套路由。

而本项目的接口**几乎全是「分析查询」而不是「资源管理」**：

- ``/api/v1/kpi/`` 不是「KPI 资源」，它是「按条件算一组指标」；
  它没有 pk，不支持 POST / PUT / DELETE，把它注册成 ViewSet 会凭空
  生成一堆返回 405 的路由，把 OpenAPI 文档污染成噪音；
- ``/api/v1/customer/{code}/360/`` 是一个**视图**（一个客户的 360° 汇总），
  不是一个可增删改的资源；
- ``/api/v1/simulate/`` 是一个动词（推演），REST 资源建模里只能用
  ``POST /simulations/`` 这种别扭的写法来表达。

所以这里选择显式 ``path``：路由即接口清单，一眼能看出「系统对外提供了什么」，
而不用去追 Router 的注册顺序与 ``@action`` 装饰器。

什么时候该换成 Router
---------------------
当接口出现真正符合资源语义的场景时（例如后续要做「自定义看板配置」的
增删改查、或「导出任务」的 RESTful 管理），再为那一组接口单独建一个
``router.register("dashboards", DashboardViewSet)``，与这里的 path 并存即可，，
两种写法并不互斥，关键是为每类接口选**语义匹配**的那一种。
"""

from django.urls import path

from apps.api import views

app_name = "api"

urlpatterns = [
    # ---- 经营总览 / 增长 -------------------------------------------------
    path("kpi/", views.KpiView.as_view(), name="kpi"),
    path("trend/", views.TrendView.as_view(), name="trend"),
    path("channel/", views.ChannelView.as_view(), name="channel"),
    # ---- 品类与利润 -----------------------------------------------------
    path("category/", views.CategoryView.as_view(), name="category"),
    path("profit/sku/", views.ProfitSkuView.as_view(), name="profit_sku"),
    path("profit/quadrant/", views.ProfitQuadrantView.as_view(), name="profit_quadrant"),
    # ---- 客户价值与留存 -------------------------------------------------
    path("customer/segments/", views.CustomerSegmentsView.as_view(), name="customer_segments"),
    path("customer/cohort/", views.CustomerCohortView.as_view(), name="customer_cohort"),
    path("customer/churn/", views.CustomerChurnView.as_view(), name="customer_churn"),
    path("customer/<str:customer_code>/360/", views.Customer360View.as_view(), name="customer_360"),
    # ---- 交付与售后 -----------------------------------------------------
    path("delivery/", views.DeliveryView.as_view(), name="delivery"),
    path("delivery/bottleneck/", views.DeliveryBottleneckView.as_view(), name="delivery_bottleneck"),
    path("after-sales/", views.AfterSalesView.as_view(), name="after_sales"),
    # ---- 回款与现金流 ---------------------------------------------------
    path("cashflow/", views.CashflowView.as_view(), name="cashflow"),
    path("cashflow/forecast/", views.CashflowForecastView.as_view(), name="cashflow_forecast"),
    # ---- 人效与门店 -----------------------------------------------------
    path("workforce/", views.WorkforceView.as_view(), name="workforce"),
    # ---- 决策模拟（含回测）---------------------------------------------
    path("simulate/", views.SimulateView.as_view(), name="simulate"),
    path("simulate/backtest/", views.BacktestView.as_view(), name="simulate_backtest"),
    # ---- 治理与元数据 ---------------------------------------------------
    path("dq/", views.DqView.as_view(), name="dq"),
    path("model/", views.ModelView.as_view(), name="model"),
    path("metrics/", views.MetricDictionaryView.as_view(), name="metrics"),
    # ---- 明细下钻 -------------------------------------------------------
    path("orders/", views.OrderListView.as_view(), name="orders"),
]

# ---------------------------------------------------------------------------
# 可选写法（等价实现，本项目未采用）
# ---------------------------------------------------------------------------
# from rest_framework.routers import DefaultRouter
#
# router = DefaultRouter()
# router.register("kpi", views.KpiViewSet, basename="kpi")
# router.register("metrics-view", views.MetricViewSet, basename="metrics")
# urlpatterns += router.urls
#
# 采用 Router 的**唯一收益**是少写几行 path，且能自动生成列表/详情路由；
# 代价是：
#   1. 生成的 ``/kpi/{pk}/`` 这类路由没有业务含义，只会让调用方困惑；
#   2. 每个 ViewSet 必须实现 ``queryset`` 或 ``get_queryset``，而分析接口
#      的「数据源」是一个筛选条件 + 一个 selector 函数，硬套 queryset
#      反而要写一堆 ``return Order.objects.none()`` 的占位代码；
#   3. 路由清单从「文件里能直接读完」变成「要靠 Router 推导」，
#      对交接与代码评审都是负收益。
