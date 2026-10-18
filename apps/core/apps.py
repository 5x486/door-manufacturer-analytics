"""核心公共应用。

承载全项目共享的枚举常量（constants）与纯函数工具（utils）。
它不定义业务实体，只提供「口径」，，各业务应用都依赖它，
因此它必须保持零业务依赖，避免形成环状引用。
"""

from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    verbose_name = "核心公共"
