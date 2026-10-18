"""主数据应用：客户、产品、门店、员工、渠道等维表。

主数据的特点是「改动少、被引用多」，因此集中在一个应用里统一维护编码与
命名规范（例如客户编号、产品型号），业务表只存外键。
维表一旦口径分叉，后面所有分析都会失准，这是它单独成应用的核心理由。
"""

from django.apps import AppConfig


class MasterdataConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.masterdata"
    verbose_name = "主数据管理"
