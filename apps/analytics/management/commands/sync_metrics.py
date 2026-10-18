# -*- coding: utf-8 -*-
"""
指标口径同步命令
================
    python manage.py sync_metrics

把 `apps/analytics/metrics.py` 里的 METRIC_DEFINITIONS（代码即口径）
同步进 `dim_metric_definition` 表，让「口径说明书」变成可查询、可在页面上点开的东西。

为什么口径要进数据库而不是只放在 Markdown 里：
  · Markdown 里写的口径，前端页面拿不到 → 指标卡旁边没法显示「这个数怎么算的」；
  · 口径改了没人知道改了哪一版 → 表里有 version 字段，可追溯；
  · 面试时面试官问「你的成交额口径」，答案应该是一条可以点开看的记录，而不是一段回忆。
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.analytics.metrics import METRIC_DEFINITIONS, METRIC_TREE_SPEC
from apps.analytics.models import MetricDefinition, MetricTree


class Command(BaseCommand):
    help = "同步指标口径字典与指标拆解树（数据源：apps/analytics/metrics.py）"

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 72))
        self.stdout.write(self.style.MIGRATE_HEADING("同步指标口径"))
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 72))

        created = updated = 0
        with transaction.atomic():
            for spec in METRIC_DEFINITIONS:
                code = spec.get("code") or spec.get("metric_code")
                if not code:
                    continue
                defaults = {
                    "name": spec.get("name", ""),
                    "domain": spec.get("domain", ""),
                    "metric_type": spec.get("metric_type", "derived"),
                    "definition": spec.get("definition", ""),
                    "formula": spec.get("formula", ""),
                    "numerator_desc": spec.get("numerator_desc", "") or "",
                    "denominator_desc": spec.get("denominator_desc", "") or "",
                    "unit": spec.get("unit", "") or "",
                    "source_tables": spec.get("source_tables", "") or "",
                    "caveat": spec.get("caveat", "") or "",
                    "version": spec.get("version", "v1"),
                    "owner": spec.get("owner", "数据分析"),
                    "order": spec.get("order", 0) or 0,
                }
                _, is_new = MetricDefinition.objects.update_or_create(code=code, defaults=defaults)
                created += 1 if is_new else 0
                updated += 0 if is_new else 1

            self.stdout.write(f"  指标口径：新增 {created} 条 / 更新 {updated} 条")

            # 指标树：先清后建（结构变化频繁，重建最省心）
            MetricTree.objects.all().delete()
            code_map = {m.code: m for m in MetricDefinition.objects.all()}
            tree_rows, skipped = [], 0

            # METRIC_TREE_SPEC 的键名可能是 parent_code/child_code 或 parent/child
            def pick(spec, *keys):
                for k in keys:
                    if spec.get(k):
                        return spec[k]
                return None

            parent_map = {}
            for spec in METRIC_TREE_SPEC:
                p_code = pick(spec, "parent_code", "parent", "from_code", "source")
                c_code = pick(spec, "child_code", "child", "to_code", "target")
                if not p_code or not c_code or p_code not in code_map or c_code not in code_map:
                    skipped += 1
                    continue
                if p_code not in parent_map:
                    parent_map[p_code] = MetricTree.objects.create(
                        parent=None,
                        metric=code_map[p_code],
                        relation_type="add",
                        note=f"{code_map[p_code].name} 为一级指标",
                    )
                tree_rows.append(MetricTree(
                    parent=parent_map[p_code],
                    metric=code_map[c_code],
                    relation_type=spec.get("relation_type", "multiply"),
                    weight=spec.get("weight", 1) or 1,
                    note=spec.get("note", "") or "",
                ))
            MetricTree.objects.bulk_create(tree_rows, batch_size=200)
            self.stdout.write(f"  指标拆解：{len(tree_rows)} 条关系"
                              + (f"（跳过 {skipped} 条无法匹配的配置）" if skipped else ""))

        self.stdout.write(self.style.SUCCESS(
            f"完成。共 {MetricDefinition.objects.count()} 条口径，"
            f"{MetricTree.objects.count()} 条拆解关系。"))
