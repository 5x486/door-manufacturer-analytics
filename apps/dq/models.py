# -*- coding: utf-8 -*-
"""
数据质量模型（dq）
==================
把原先 print 一通的 check_data.py，变成「可查询、可对比、可进 CI」的对象。

关键改进：
  1. 每次校验落库 → 能看到「上周还好，这周主键重复了」；
  2. 有 severity → error 阻断（exit 1，CI 失败），warning 只提示；
  3. 有 actual/sample → 出问题时能直接看到是哪几行坏了，不用重跑脚本。
"""
from django.db import models

from apps.core import constants as C
from apps.core.base import BaseModel


class DQCheck(BaseModel):
    """
    校验规则定义（规则本身，不是结果）。

    rule_code 全局唯一，CI 与页面都按它引用。
    """

    rule_code = models.CharField("规则编码", max_length=32, unique=True,
                                 help_text="如 PK_ORDER_UNIQUE / FK_ORDER_CUSTOMER")
    rule_name = models.CharField("规则名称", max_length=64)
    category = models.CharField("规则类别", max_length=16, db_index=True,
                                help_text="主键 / 外键 / 金额 / 日期 / 业务 / 完整性")
    target_table = models.CharField("目标表", max_length=64)

    description = models.TextField("规则说明", blank=True)
    sql_text = models.TextField("校验逻辑说明", blank=True,
                                help_text="用自然语言+SQL 说明这条规则怎么查异常")
    severity = models.CharField("严重级别", max_length=8, choices=C.DQSeverityChoices.choices,
                                default=C.DQSeverityChoices.ERROR)
    threshold = models.DecimalField("告警阈值", max_digits=10, decimal_places=4, default=0,
                                    help_text="异常行数超过该值即判失败")

    is_active = models.BooleanField("是否启用", default=True, db_index=True)
    order = models.PositiveSmallIntegerField("执行顺序", default=0)

    class Meta:
        db_table = "dq_check"
        verbose_name = "数据质量规则"
        verbose_name_plural = "数据质量规则"
        ordering = ["category", "order", "rule_code"]

    def __str__(self):
        return f"{self.rule_code} {self.rule_name}"


class DQRun(BaseModel):
    """一次校验执行（整批），用来做趋势对比与 CI 判定。"""

    run_at = models.DateTimeField("执行时间", auto_now_add=True, db_index=True)
    trigger = models.CharField("触发方式", max_length=16, default="manual",
                               help_text="manual / command / ci / scheduled")
    data_as_of = models.DateField("数据基准日", null=True, blank=True)

    total_checks = models.PositiveIntegerField("规则总数", default=0)
    passed = models.PositiveIntegerField("通过数", default=0)
    warned = models.PositiveIntegerField("警告数", default=0)
    failed = models.PositiveIntegerField("失败数", default=0)
    duration_ms = models.PositiveIntegerField("耗时（毫秒）", default=0)
    is_healthy = models.BooleanField("整体是否健康", default=True, db_index=True)

    class Meta:
        db_table = "dq_run"
        verbose_name = "校验批次"
        verbose_name_plural = "校验批次"
        ordering = ["-run_at"]

    def __str__(self):
        return f"{self.run_at:%Y-%m-%d %H:%M} 通过 {self.passed}/{self.total_checks}"

    @property
    def pass_rate(self):
        if not self.total_checks:
            return None
        return self.passed / self.total_checks


class DQResult(BaseModel):
    """单条规则的执行结果（含异常样本，便于定位）。"""

    run = models.ForeignKey(DQRun, on_delete=models.CASCADE, related_name="results",
                            verbose_name="校验批次")
    # 字段名不能叫 check：Django 的 Model.check() 是框架内置方法，
    # 同名外键会让 django 系统检查直接报 models.E020（系统跑不起来）。
    # 这里用 rule，同时用 db_column 保留原列名 check_id —— 语义修正不影响已有表结构。
    rule = models.ForeignKey(DQCheck, on_delete=models.CASCADE, related_name="results",
                             db_column="check_id", verbose_name="规则")

    status = models.CharField("状态", max_length=8, choices=C.DQStatusChoices.choices,
                              db_index=True)
    checked_rows = models.BigIntegerField("检查行数", default=0)
    issue_rows = models.BigIntegerField("异常行数", default=0)
    issue_ratio = models.DecimalField("异常占比", max_digits=8, decimal_places=6, default=0)

    # 512 而非 255：校验消息里常带「阈值 / 实际值 / 差值」三段说明，
    # 255 会在 MySQL 严格模式下直接抛 Data too long 导致整批校验中断。
    message = models.CharField("结果说明", max_length=512, blank=True)
    sample_keys = models.JSONField("异常样本主键", default=list, blank=True)
    detail = models.JSONField("明细", default=dict, blank=True)

    class Meta:
        db_table = "dq_result"
        verbose_name = "校验结果"
        verbose_name_plural = "校验结果"
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["run", "status"], name="idx_dqres_run_status"),
        ]

    def __str__(self):
        return f"{self.rule.rule_code} → {self.status}"
