# -*- coding: utf-8 -*-
"""
抽象基类
========
全项目模型统一继承，避免每个模型重复写时间戳与通用方法。

设计取舍：
  - 业务表（订单/客户等）源数据来自 CSV 导入，故主键用自增 BigAutoField，
    同时保留业务编码字段（order_no 等）并加唯一索引 ，， 业务编码对外，自增主键对内。
  - 不用软删除：经营数据「取消」有明确业务字段（order_status='已取消'），
    再加一层 deleted_at 会让口径混乱。
"""
from django.db import models


class BaseModel(models.Model):
    """所有模型的基类：创建/更新时间。"""

    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        abstract = True


class BusinessModel(BaseModel):
    """
    业务数据表基类：额外挂一个 extra 字段。

    extra 用于存放「实验性/低频」属性，避免为每个小需求加一次迁移；
    凡是进入指标口径的字段一律显式建模，不许塞进 extra。
    """

    extra = models.JSONField("扩展属性", default=dict, blank=True)

    class Meta:
        abstract = True


class ReadOnlyModel(models.Model):
    """
    预聚合表（dws_*）基类。

    这些表由 ETL 命令整表重建，Django 侧只读：
    模型上覆盖 save/delete 抛错，防止业务代码误写导致「预聚合与明细不一致」。
    """

    built_at = models.DateTimeField("构建时间", auto_now=True)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        raise NotImplementedError(
            f"{self.__class__.__name__} 是预聚合表，只读；"
            f"请通过 `manage.py build_dws` 重建，不要直接写。"
        )

    def delete(self, *args, **kwargs):
        raise NotImplementedError(
            f"{self.__class__.__name__} 是预聚合表，只读；请通过 `manage.py build_dws` 重建。"
        )
