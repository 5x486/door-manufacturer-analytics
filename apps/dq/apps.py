"""数据质量应用：校验规则、异常清单与质量评分。

分析结论的可信度取决于底层数据，此应用负责在导入与每日跑批时检查
空值率、重复单号、金额异常（负数/超大额）、时间穿越（交付早于下单）等问题，
并输出可追踪的异常清单，，没有这层保障，看板上的漂亮曲线可能只是脏数据。
"""

from django.apps import AppConfig


class DqConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.dq"
    verbose_name = "数据质量"
