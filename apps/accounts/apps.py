"""账号与权限应用。

存放自定义用户模型、角色（老板/店长/销售/财务）与登录行为。
独立成应用是因为 AUTH_USER_MODEL 一旦建库就很难更换，
把它与业务数据解耦，未来接入企业微信/钉钉登录时改动面最小。
"""

from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    verbose_name = "账号权限"
