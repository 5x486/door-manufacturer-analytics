"""经营分析应用：指标聚合、趋势、结构、同比环比与专题分析。

定位是「只读 + 计算」：负责把 sales 的明细单据聚合成分析口径的结果，
并保证同一指标在页面、导出、接口三处使用同一个实现，避免三份口径互相打架。
"""

from django.apps import AppConfig


class AnalyticsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.analytics"
    verbose_name = "经营分析"
