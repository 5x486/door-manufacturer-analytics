# -*- coding: utf-8 -*-
"""页面路由（dashboard）。

命名规范：``<模块>:<页面>``，例如 ``dashboard:overview``、``dashboard:profit``。
路由名同时被导航高亮（context_processors.nav_modules）与面包屑复用。

设计取舍：页面路由与 API 路由（``/api/v1/``）分开。
页面返回 HTML、走 ``@login_required``；API 返回统一响应包、可被前端下钻复用。
两者共用同一套 selectors，口径不会分叉。
"""
from django.urls import path

from apps.dashboard import views

app_name = "dashboard"

urlpatterns = [
    path("", views.overview, name="overview"),                       # 经营总览
    path("growth/", views.growth, name="growth"),                    # 增长与渠道
    path("profit/", views.profit, name="profit"),                    # 品类与利润
    path("customers/", views.customers, name="customers"),           # 客户价值
    path("customers/<str:customer_code>/", views.customer_detail, name="customer_detail"),  # 客户 360
    path("retention/", views.retention, name="retention"),           # 留存与预警
    path("delivery/", views.delivery, name="delivery"),              # 交付与售后
    path("cashflow/", views.cashflow, name="cashflow"),              # 回款与现金流
    path("workforce/", views.workforce, name="workforce"),           # 人效与门店
    path("simulator/", views.simulator, name="simulator"),           # 决策模拟
    path("dq/", views.data_quality, name="data_quality"),            # 数据质量
    path("model/", views.model_online, name="model_online"),         # 模型在线
    path("metrics/", views.metric_dictionary, name="metric_dictionary"),  # 指标字典
    path("orders/", views.order_list, name="order_list"),            # 明细数据
    path("help/", views.help_page, name="help"),                     # 使用说明
]