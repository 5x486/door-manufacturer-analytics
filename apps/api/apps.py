"""API 应用配置。"""

from django.apps import AppConfig


class ApiConfig(AppConfig):
    """对外接口应用（v1）。

    本应用不定义模型、不产生迁移：它是 selectors 之上的「适配层」，，
    把取数结果翻译成统一响应包（``{code, message, data, meta}``），
    并负责参数校验、缓存与 OpenAPI 文档。分析口径一律不在这里实现。
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.api"
    verbose_name = "数据接口"
