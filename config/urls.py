"""根 URL 配置：把各业务域的入口在这里汇总。

路由划分的用意：
- ``/``            经营总览看板及其全部专题页（dashboard）；
                   专题页（增长/品类利润/客户/留存/交付/回款/人效/模拟器/质检/模型/口径）
                   都挂在同一命名空间下，导航与权限只需维护一份；
- ``/api/v1/``     前后端分离接口，供 ECharts 取数，版本化便于将来并行发版；
- ``/api/docs/``   自动生成的 Swagger UI，接口契约即文档；
- ``/admin/``      数据维护后台，主数据由业务人员在这里维护。

说明：曾经规划过独立的 ``/analytics/`` 路由树，但页面全在 dashboard 命名空间下、
复用同一套筛选条与布局，拆成两棵树只会让导航与权限配置重复，故不保留空路由。
每个 include 都显式声明 namespace，模板里用 ``{% url 'dashboard:overview' %}`` 引用。
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

urlpatterns = [
    # 首页直接挂 dashboard，省掉一次跳转，也让根路径有真实内容
    path("", include("apps.dashboard.urls", namespace="dashboard")),

    # 登录、登出、改密码等由 Django 内置认证视图提供，
    # 模板放在 templates/registration/ 下即可，无需自己写视图
    path("accounts/", include("django.contrib.auth.urls")),

    path("admin/", admin.site.urls),

    # REST API：业务接口统一挂在 /api/v1/ 下，便于按版本演进
    path("api/v1/", include("apps.api.urls", namespace="api")),

    # OpenAPI schema 与在线文档
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path(
        "api/redoc/",
        SpectacularRedocView.as_view(url_name="schema"),
        name="redoc",
    ),
]

# 开发期由 Django 直接伺服用户上传的图片/附件；
# 生产环境交给 nginx（deploy/nginx.conf 里已配置 /static/ 与 /media/ 直出），
# 所以这里必须用 DEBUG 守卫，绝不能带上线。
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

# 后台标题汉化，属于运营同事日常接触的界面
admin.site.site_header = "杭博门业经营分析系统"
admin.site.site_title = "杭博门业"
admin.site.index_title = "数据管理后台"
