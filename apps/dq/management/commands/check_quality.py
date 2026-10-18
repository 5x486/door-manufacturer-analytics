# -*- coding: utf-8 -*-
"""
数据质量校验命令
================
    python manage.py check_quality                    # 跑全部规则
    python manage.py check_quality --category 金额     # 只跑某一类
    python manage.py check_quality --rules PK_ORDER_UNIQUE,FK_ITEM_ORDER

与旧版 scripts/check_data.py 的区别（这也是重构的收益）：
  · 结果落库（DQRun / DQResult）→ 能看历史趋势，能回答「上周还好这周为什么坏了」；
  · error 级失败会 exit 1 → 可以直接放进 CI / 定时任务，失败有信号；
  · 输出异常样本主键 → 出问题时能定位到具体是哪几行，不用重跑脚本找。
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.dq import checks as dq_checks


class Command(BaseCommand):
    help = "执行数据质量校验并落库（失败会以非 0 退出码结束，可用于 CI）"

    def add_arguments(self, parser):
        parser.add_argument("--category", default=None, help="只跑指定类别，如：主键 / 外键 / 金额 / 日期 / 业务")
        parser.add_argument("--rules", default="", help="只跑指定规则编码，逗号分隔")
        parser.add_argument("--trigger", default="manual",
                            choices=["manual", "import", "scheduled", "ci"])

    def handle(self, *args, **options):
        rule_codes = [s.strip() for s in options["rules"].split(",") if s.strip()] or None

        self.stdout.write(self.style.MIGRATE_HEADING("=" * 78))
        self.stdout.write(self.style.MIGRATE_HEADING("数据质量校验"))
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 78))

        try:
            run = dq_checks.run_all_checks(
                trigger=options["trigger"],
                category=options["category"],
                rule_codes=rule_codes,
            )
        except ValueError as exc:
            self.stdout.write(self.style.ERROR(f"  无法执行：{exc}"))
            raise SystemExit(2)

        results = getattr(run, "result_rows", None)
        if results is None:
            results = list(run.results.select_related("rule").all())
        for res in results:
            status = res.status
            style = {"pass": self.style.SUCCESS,
                     "warn": self.style.WARNING,
                     "fail": self.style.ERROR}.get(status, str)
            mark = {"pass": "通过", "warn": "警告", "fail": "失败"}.get(status, status)
            self.stdout.write(
                f"  [{style(mark)}] {res.rule.rule_code:<34s} "
                f"检查 {res.checked_rows:>7,} 行 / 异常 {res.issue_rows:>5,} 行  {res.message}"
            )
            if res.sample_keys:
                sample = ", ".join(str(k) for k in res.sample_keys[:5])
                self.stdout.write(f"        异常样本：{sample}")

        self.stdout.write(self.style.MIGRATE_HEADING("-" * 78))
        self.stdout.write(
            f"  规则 {run.total_checks} 条 · 通过 {run.passed} · 警告 {run.warned} · "
            f"失败 {run.failed} · 耗时 {run.duration_ms} ms"
        )
        self.stdout.write(f"  校验批次 ID = {run.pk}（可在「数据质量」页查看历史）")

        if not run.is_healthy:
            self.stdout.write(self.style.ERROR(
                "存在 error 级失败项，退出码 1（CI 会因此失败）。请先修数据再重跑。"))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("所有校验项通过 ✓"))
