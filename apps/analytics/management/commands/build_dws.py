# -*- coding: utf-8 -*-
"""
预聚合表重建命令
================
    python manage.py build_dws                 # 全量重建
    python manage.py build_dws --only business_monthly,rep_monthly

为什么要有预聚合表（面试会问）：
  看板每次筛选都去扫 fact_order 做 group by，数据量小时无所谓，
  真实企业 500 万行订单时会变成几秒的等待。预聚合把「常用维度交叉」
  提前算好，查询变成单表扫描 ，， 用存储换响应时间，是最常见的数仓做法。

重建策略：整表删除 + 批量插入（在事务内）。选它而不是增量更新，是因为
口径会调整；重建保证「库里 = 当前口径」，不会残留旧口径的行。
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.analytics import etl


class Command(BaseCommand):
    help = "重建 dws_* 预聚合表（经营月度 / 品类利润 / cohort / 复购间隔 / 交付阶段 / 账龄 / 人效 / 业务员）"

    # 命令名 → etl 函数名
    TASKS = {
        "business_monthly": "rebuild_business_monthly",
        "category_monthly": "rebuild_category_monthly",
        "customer_cohort": "rebuild_customer_cohort",
        "repurchase_interval": "rebuild_repurchase_interval",
        "delivery_stage": "rebuild_delivery_stage",
        "receivable_aging": "rebuild_receivable_aging",
        "workforce_monthly": "rebuild_workforce_monthly",
        "rep_monthly": "rebuild_rep_monthly",
    }

    def add_arguments(self, parser):
        parser.add_argument("--only", default="",
                            help="只重建指定任务，逗号分隔，例如 business_monthly,rep_monthly")
        parser.add_argument("--list", action="store_true", help="列出所有可重建任务")

    def handle(self, *args, **options):
        if options["list"]:
            for key in self.TASKS:
                self.stdout.write(f"  {key}")
            return

        only = [s.strip() for s in options["only"].split(",") if s.strip()]
        tasks = {k: v for k, v in self.TASKS.items() if not only or k in only}

        self.stdout.write(self.style.MIGRATE_HEADING("=" * 72))
        self.stdout.write(self.style.MIGRATE_HEADING("重建预聚合表（dws_*）"))
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 72))

        total_start = time.perf_counter()
        failed = []
        for key, func_name in tasks.items():
            func = getattr(etl, func_name, None)
            if func is None:
                self.stdout.write(self.style.WARNING(f"  {key:<22s} 跳过（etl.{func_name} 不存在）"))
                failed.append(key)
                continue
            start = time.perf_counter()
            try:
                with transaction.atomic():
                    result = func()
                cost = time.perf_counter() - start
                # etl 里的 rebuild_* 统一返回统计 dict（table / rows / version_token），
                # 但也允许只返回 int 行数 —— 这里两种都兼容，避免因返回形态变化就报错。
                if isinstance(result, dict):
                    detail = f"{result.get('rows', 0)} 行  token={result.get('version_token', '-')}"
                elif result is None:
                    detail = "-"
                else:
                    detail = f"{result} 行"
                self.stdout.write(self.style.SUCCESS(
                    f"  {key:<22s} OK  {detail:<28s} {cost:6.2f}s"))
            except Exception as exc:                       # 单表失败不影响其他表
                cost = time.perf_counter() - start
                self.stdout.write(self.style.ERROR(f"  {key:<22s} 失败 {cost:6.2f}s  {exc}"))
                failed.append(key)

        total = time.perf_counter() - total_start
        self.stdout.write(self.style.MIGRATE_HEADING("-" * 72))
        if failed:
            self.stdout.write(self.style.WARNING(
                f"完成，{len(failed)} 个任务失败：{', '.join(failed)}；总耗时 {total:.2f}s"))
        else:
            self.stdout.write(self.style.SUCCESS(f"全部完成，总耗时 {total:.2f}s"))
