"""销售业务应用：订单、收款、交付、售后、成本。

这是全系统的数据源头，所有分析指标都由这里的原始单据派生。
写入逻辑（下单、收款登记、交付推进）都收敛在本应用内，
分析类应用只做只读查询，避免同一份口径出现两个计算实现。
"""

from django.apps import AppConfig


class SalesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.sales"
    verbose_name = "销售业务"
