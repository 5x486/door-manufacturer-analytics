"""经营看板应用配置。"""

from django.apps import AppConfig


class DashboardConfig(AppConfig):
    """看板应用。

    看板只做「展示层编排」：解析筛选、调用 selectors、组装图表 option、渲染模板。
    它**不定义模型**（没有 models.py），因此不产生任何数据库迁移，，
    这个约束是有意的：展示层一旦开始自己存数据，口径就会分叉。
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.dashboard"
    verbose_name = "经营看板"
