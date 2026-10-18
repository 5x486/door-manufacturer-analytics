# -*- coding: utf-8 -*-
"""
自定义用户模型（accounts）
==========================
为什么不在项目一开始就用默认 User：Django 的官方建议是「项目启动时就换成自定义用户模型」，
中途再换代价极大。这里继承 AbstractUser，额外挂三个业务字段：

  role            角色：老板 / 运营 / 业务员 / 只读访客
  sales_rep       业务员关联：业务员登录后只看自己的单（数据权限）
  display_name    中文姓名，界面上比 username 好看

权限设计（对应真实企业的最小可用方案）：
  BOSS      看全部，可导出
  OPERATOR  看全部经营数据，不可改系统配置
  SALES     只看自己名下订单与客户（行级过滤在 selectors 层实现）
  GUEST     只读演示账号，给面试官用，看不到客户联系方式
"""
from django.contrib.auth.models import AbstractUser
from django.db import models

from apps.core import constants as C


class UserRole(models.TextChoices):
    BOSS = "boss", "老板"
    OPERATOR = "operator", "运营"
    SALES = "sales", "业务员"
    GUEST = "guest", "只读访客"


# 角色 -> 是否可见客户联系方式（电话/地址）
ROLE_CAN_SEE_CONTACT = {"boss", "operator", "sales"}
# 角色 -> 是否可访问全部数据（否则按 sales_rep 过滤）
ROLE_CAN_SEE_ALL = {"boss", "operator", "guest"}


class User(AbstractUser):
    """项目统一用户模型。"""

    display_name = models.CharField("姓名", max_length=32, blank=True)
    role = models.CharField("角色", max_length=16, choices=UserRole.choices,
                            default=UserRole.OPERATOR, db_index=True)

    # 关联业务员：用于「业务员只能看自己数据」的行级权限
    sales_rep = models.ForeignKey(
        "masterdata.SalesRep", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="users", verbose_name="关联业务员",
        help_text="角色为「业务员」时填写，用于数据行级过滤",
    )

    last_login_ip = models.GenericIPAddressField("最后登录 IP", null=True, blank=True)

    class Meta:
        db_table = "sys_user"
        verbose_name = "系统用户"
        verbose_name_plural = "系统用户"

    def __str__(self):
        return self.display_name or self.username

    @property
    def can_see_all_data(self):
        """是否能看全部数据（否则只能看自己名下）。"""
        if self.is_superuser:
            return True
        return self.role in ROLE_CAN_SEE_ALL

    @property
    def can_see_customer_contact(self):
        """是否能看到客户电话/地址（脱敏红线）。"""
        if self.is_superuser:
            return True
        return self.role in ROLE_CAN_SEE_CONTACT

    @property
    def can_export(self):
        """是否可以导出数据。"""
        if self.is_superuser:
            return True
        return self.role in {"boss", "operator"}

    def mask_phone(self, phone):
        """按角色脱敏电话号码；无权限时返回掩码。"""
        if not phone:
            return ""
        if self.can_see_customer_contact:
            return phone
        return phone[:3] + "****" + phone[-2:] if len(phone) >= 7 else "***"
