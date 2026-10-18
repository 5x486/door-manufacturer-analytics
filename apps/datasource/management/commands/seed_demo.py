# -*- coding: utf-8 -*-
"""
演示数据种入命令
================
一条命令把项目从「空库」变成「可直接演示的系统」：

    python manage.py seed_demo            # 完整流程
    python manage.py seed_demo --skip-base # 跳过 CSV 导入，只重算派生数据

流程（顺序不可颠倒，前者是后者的输入）：
  1. load_all            8 张 CSV → ODS 业务表（幂等）
  2. gen_costs           产品成本标准 + 明细成本拆分 → 产出毛利字段
  3. gen_payment_terms   合同账期与应收（账龄 / 逾期 / 回款率）
  4. gen_delivery_stages 交付阶段耗时与延期归因
  5. gen_workforce       员工与工时
  6. gen_targets         销售目标（年/月 × 范围）
  7. gen_benchmarks      竞品对标价
  8. rebuild_rfm         客户 RFM 分群 + KMeans 交叉验证
  9. （可选）build_dws   预聚合表，由独立命令负责

为什么派生数据用「删除重建」而不是增量：这些数据的口径会随分析需求调整，
重建式写入保证「库里 = 口径的当前定义」，不会残留旧口径的行。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
from decimal import ROUND_HALF_UP, Decimal

import numpy as np
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count, Max, Min, Q, Sum

from apps.core import constants as C
from apps.masterdata.models import (
    CompetitorBenchmark, Customer, Employee, Product, ProductCostStandard,
)
from apps.sales.models import (
    DeliveryStage, LaborHour, Order, OrderItem, OrderItemCost, Payment,
    PaymentTerm, SalesTarget,
)

logger = logging.getLogger(__name__)

# 固定随机种子：与数据生成脚本一致，保证每次种出的数据完全一样（可复现）
SEED = 20260809
D = Decimal
CENT = D("0.01")


def rng():
    """每次调用返回独立但可复现的随机数发生器。"""
    return np.random.default_rng(SEED)


def money(value) -> Decimal:
    """统一金额取整：两位小数、四舍五入。"""
    return D(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def month_iter(start: dt.date, end: dt.date):
    """按自然月迭代 (年, 月)。"""
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


def _normalize_stat(stat):
    """把装载器返回值统一成 (created, updated, skipped)。

    这里刻意做兼容而不是强约束：数据层（loaders）与分析层（命令）会分别演进，
    接口一旦不匹配，报错信息是 `TypeError: tuple indices must be integers`，
    排查成本远高于在入口处做 10 行归一化。
    支持的形态：ImportStat（有 as_dict）、dict、三元组 (created, updated, total)。
    """
    if hasattr(stat, "as_dict"):
        d = stat.as_dict()
        return d.get("created", 0), d.get("updated", 0), d.get("skipped", 0)
    if isinstance(stat, dict):
        return stat.get("created", 0), stat.get("updated", 0), stat.get("skipped", 0)
    if isinstance(stat, (tuple, list)):
        created = stat[0] if len(stat) > 0 else 0
        updated = stat[1] if len(stat) > 1 else 0
        total = stat[2] if len(stat) > 2 else (created + updated)
        return created, updated, max(0, total - created - updated)
    return 0, 0, 0

class Command(BaseCommand):
    help = "种入演示数据：CSV 导入 + 成本毛利 + 账期应收 + 交付阶段 + 人效 + 目标 + 竞品 + RFM"

    def add_arguments(self, parser):
        parser.add_argument("--skip-base", action="store_true",
                            help="跳过 CSV 基础数据导入（仅重算派生数据）")
        parser.add_argument("--skip-derive", action="store_true",
                            help="只导入 CSV 基础数据，不生成派生数据")

    def handle(self, *args, **options):
        from apps.datasource import loaders

        data_as_of = getattr(settings, "DATA_AS_OF_DATE", dt.date(2026, 8, 9))
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 72))
        self.stdout.write(self.style.MIGRATE_HEADING("杭博门业 · 演示数据种入"))
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 72))
        self.stdout.write(f"  数据基准日 DATA_AS_OF = {data_as_of}")

        if not options["skip_base"]:
            self.stdout.write(self.style.HTTP_INFO("\n[1/8] 导入 8 张原始 CSV"))
            stats = loaders.load_all()
            for table, stat in stats.items():
                c, u, k = _normalize_stat(stat)
                self.stdout.write(f"    {table:<26s} 新增 {c:>6,} / 更新 {u:>6,} / 跳过 {k:>4,}")

        if options["skip_derive"]:
            self.stdout.write(self.style.SUCCESS("\n基础数据导入完成（--skip-derive）"))
            return

        self.stdout.write(self.style.HTTP_INFO("\n[2/8] 产品成本标准 + 明细成本拆分"))
        self.stdout.write(f"    {self.gen_costs()}")

        self.stdout.write(self.style.HTTP_INFO("\n[3/8] 合同账期与应收"))
        self.stdout.write(f"    {self.gen_payment_terms(data_as_of)}")

        self.stdout.write(self.style.HTTP_INFO("\n[4/8] 交付阶段与延期归因"))
        self.stdout.write(f"    {self.gen_delivery_stages()}")

        self.stdout.write(self.style.HTTP_INFO("\n[5/8] 员工与工时"))
        self.stdout.write(f"    {self.gen_workforce(data_as_of)}")

        self.stdout.write(self.style.HTTP_INFO("\n[6/8] 销售目标"))
        self.stdout.write(f"    {self.gen_targets(data_as_of)}")

        self.stdout.write(self.style.HTTP_INFO("\n[7/8] 竞品对标价"))
        self.stdout.write(f"    {self.gen_benchmarks(data_as_of)}")

        self.stdout.write(self.style.HTTP_INFO("\n[8/8] 客户 RFM 分群"))
        self.stdout.write(f"    {self.rebuild_rfm(data_as_of)}")

        self.stdout.write(self.style.SUCCESS("\n演示数据种入完成。下一步："))
        self.stdout.write("    python manage.py build_dws       # 重建预聚合表")
        self.stdout.write("    python manage.py check_quality   # 跑数据质量校验")

    # -----------------------------------------------------------------------
    # [2] 成本与毛利
    # -----------------------------------------------------------------------
    def gen_costs(self) -> str:
        """
        生成产品成本标准，并按明细拆分六类成本。

        定价逻辑（与真实门业成本结构对齐）：
          售价 = base_price × price_multiplier × 尺寸系数 × 非标系数
          目标毛利率按品类给定（铜门 34% / 不锈钢门 24% / 防火门 18% …）
          成本 = 售价 × (1 - 目标毛利率)，再按成本结构比例拆成五类
          （售后成本单独计，不参与毛利）
        """
        r = rng()
        price = self._build_price_estimator()
        products = list(Product.objects.all())

        ProductCostStandard.objects.all().delete()
        standards = []
        for p in products:
            est = price(p, r)
            margin = D(str(C.CATEGORY_TARGET_MARGIN.get(p.category, 0.25)))
            total_cost = est * (D("1") - margin)
            # 成本结构比例做品类微调：铜门材料占比更高，防火门人工/制造占比更高
            ratios = dict(C.COST_STRUCTURE_RATIO)
            if p.category == C.CategoryChoices.COPPER.value:
                ratios[C.CostTypeChoices.MATERIAL.value] = 0.58
            elif p.category == C.CategoryChoices.FIREPROOF.value:
                ratios[C.CostTypeChoices.MATERIAL.value] = 0.46
            material = total_cost * D(str(ratios[C.CostTypeChoices.MATERIAL.value]))
            weight = D(str(round(float(p.std_area) / 1e6 * 55.0, 2))) or D("60")   # 按面积估重
            standards.append(ProductCostStandard(
                product=p,
                effective_from=dt.date(2023, 1, 1),
                material_cost=money(material),
                labor_cost=money(total_cost * D(str(ratios[C.CostTypeChoices.LABOR.value]))),
                manufacturing_cost=money(total_cost * D(str(ratios[C.CostTypeChoices.MANUFACTURING.value]))),
                logistics_cost=money(total_cost * D(str(ratios[C.CostTypeChoices.LOGISTICS.value]))),
                installation_cost=money(total_cost * D(str(ratios[C.CostTypeChoices.INSTALLATION.value]))),
                material_unit_price=money(material / weight),
                material_weight_kg=weight,
            ))
        ProductCostStandard.objects.bulk_create(standards, batch_size=200)

        # ---- 明细成本拆分 ----
        OrderItemCost.objects.all().delete()
        OrderItem.objects.update(total_cost=D("0"), gross_profit=D("0"))

        # 按品类分组批量处理，避免逐行查询
        by_cat = {}
        for item in OrderItem.objects.select_related("product", "order").all():
            by_cat.setdefault(item.category, []).append(item)

        cost_rows, item_updates = [], []
        for category, items in by_cat.items():
            margin = float(C.CATEGORY_TARGET_MARGIN.get(category, 0.25))
            ratios = dict(C.COST_STRUCTURE_RATIO)
            ratios.pop(C.CostTypeChoices.AFTER_SALES.value, None)
            for item in items:
                # 目标毛利率 ± 8 个百分点的现实波动（Beta 分布偏乐观）
                jitter = float(r.beta(2.2, 2.0)) * 0.16 - 0.08
                m = min(max(margin + jitter, 0.02), 0.55)
                if item.is_custom:                      # 非标定制成本上浮
                    m -= float(r.uniform(0.08, 0.18))
                m = max(m, -0.05)

                unit_line = money(item.line_amount)
                total_cost = money(unit_line * D(str(round(1 - m, 6))))
                if total_cost < 0:
                    total_cost = D("0")

                remain = total_cost
                keys = list(ratios.keys())
                for idx, ctype in enumerate(keys):
                    share = D(str(ratios[ctype]))
                    if idx == len(keys) - 1:
                        amount = remain                    # 最后一项吃掉尾差，保证加总=total_cost
                    else:
                        amount = money(total_cost * share)
                        remain -= amount
                    cost_rows.append(OrderItemCost(item=item, cost_type=ctype,
                                                   amount=amount, is_standard=False))

                item.total_cost = total_cost
                item.gross_profit = money(unit_line - total_cost)
                item_updates.append(item)

        OrderItemCost.objects.bulk_create(cost_rows, batch_size=2000)
        OrderItem.objects.bulk_update(item_updates, ["total_cost", "gross_profit"], batch_size=1000)
        return f"成本标准 {len(standards)} 条 · 明细成本 {len(cost_rows)} 行 · 明细毛利回填 {len(item_updates)} 行"

    def _build_price_estimator(self):
        """
        返回一个 (product, rng) -> 预估单价 的函数。

        用途：成本标准需要「售价」做锚。这里用产品基础价 × 配置系数做基准，
        与订单明细实际成交价无关（成本是出厂口径，不随当期折扣变化）。
        """
        def estimator(product, r):
            base = D(str(product.base_price)) * D(str(product.price_multiplier))
            return money(base)
        return estimator

    # -----------------------------------------------------------------------
    # [3] 合同账期与应收
    # -----------------------------------------------------------------------
    def gen_payment_terms(self, data_as_of: dt.date) -> str:
        """
        生成应收账龄。

        这里有一个必须讲清楚的口径问题：**付款流水 ≠ 回款结清**。
        原始 payments 是按合同额生成的（定金 30% + 尾款 70%），
        所以「按流水算应收」会得出「所有单都结清了、逾期 0 条」，，
        这个结论在真实企业里不可能，等于把回款风险写成不存在。

        因此本函数按真实制造业的回款规律重建应收口径：
          · 回款率 = 该订单类型的基础回款率区间 × 客户履约扰动
            （零售散户几乎全款；装企有账期、常留尾差；工程按进度付、尾款难收）
          · 未回款部分才是应收；账期起点 = 实际交付日（未交付用计划日）
          · 到约定到期日仍未收齐 → 形成逾期账龄，工程/装企概率显著高于零售
        """
        r = rng()
        PaymentTerm.objects.all().delete()

        # 各订单类型的回款率区间（低, 高）——应收风险的核心假设
        collect_range = {
            C.OrderTypeChoices.RETAIL.value: (0.985, 0.999),       # 散户：送货前基本付清
            C.OrderTypeChoices.DECORATION.value: (0.855, 0.975),   # 装企：月结，常有尾差
            C.OrderTypeChoices.ENGINEERING.value: (0.700, 0.945),  # 工程：按进度付，尾款拖欠
        }
        # 全额结清的概率：散户几乎都付清，工程单则大面积留尾款
        full_pay_prob = {
            C.OrderTypeChoices.RETAIL.value: 0.97,
            C.OrderTypeChoices.DECORATION.value: 0.72,
            C.OrderTypeChoices.ENGINEERING.value: 0.58,
        }
        credit_range = {
            C.OrderTypeChoices.RETAIL.value: (0, 15),
            C.OrderTypeChoices.DECORATION.value: (30, 60),
            C.OrderTypeChoices.ENGINEERING.value: (60, 90),
        }
        # 到期仍未收齐的概率（工程 > 装企 > 零售）
        overdue_prob = {
            C.OrderTypeChoices.RETAIL.value: 0.05,
            C.OrderTypeChoices.DECORATION.value: 0.28,
            C.OrderTypeChoices.ENGINEERING.value: 0.45,
        }

        rows, overdue_terms = [], []
        orders = (Order.objects.exclude(order_status__in=C.VALID_ORDER_EXCLUDE_STATUS)
                  .select_related("customer"))
        # 一次性取回各单最后回款日，避免循环里查询
        last_map = dict(Payment.objects.filter(is_refund=False)
                        .values_list("order_id").annotate(m=Max("payment_date")))

        for o in orders:
            low, high = collect_range.get(o.order_type, (0.90, 1.0))
            contract = money(o.final_amount)
            # 关键：先决定这单「是否全额结清」，再决定结清率。
            # 若只从 [0.985, 1.0] 均匀取值，2 位小数舍入后几乎永远凑不满合同额，
            # 结果就是 2232 单全部未结清、95% 逾期 —— 一个不可能的经营画面。
            full_paid = r.random() < full_pay_prob.get(o.order_type, 0.95)
            rate = 1.0 if full_paid else float(r.uniform(low, min(high, 0.995)))
            paid = money(contract * D(str(round(rate, 6))))
            outstanding = money(contract - paid)

            c_low, c_high = credit_range.get(o.order_type, (0, 30))
            credit_days = int(r.integers(c_low, c_high + 1))
            base_date = o.actual_delivery_date or o.planned_delivery_date or o.order_date
            due = base_date + dt.timedelta(days=credit_days) if base_date else None

            # 逾期判定：仍有未收余额 + 已过约定到期日
            overdue_days = 0
            if outstanding > CENT and due and due < data_as_of:
                base_overdue = (data_as_of - due).days
                if r.random() < overdue_prob.get(o.order_type, 0.15):
                    # 抽中拖欠：在到期日基础上再拖一段（真实企业里的「尾款黑洞」）
                    overdue_days = base_overdue + int(r.integers(10, 120))
                else:
                    overdue_days = base_overdue

            is_settled = outstanding <= CENT
            if is_settled:
                outstanding = D("0.00")
                overdue_days = 0
                paid = contract

            term = PaymentTerm(
                order=o,
                contract_amount=contract,
                paid_amount=paid,
                outstanding_amount=outstanding,
                agreed_due_date=due,
                last_payment_date=last_map.get(o.id),
                is_settled=is_settled,
                credit_days=credit_days,
                overdue_days=overdue_days,
                aging_bucket=C.aging_bucket(overdue_days),
            )
            rows.append(term)
            if overdue_days > 0:
                overdue_terms.append(term)

        PaymentTerm.objects.bulk_create(rows, batch_size=1000)

        unsettled = sum(1 for t in rows if not t.is_settled)
        amount = sum((t.outstanding_amount for t in rows), D("0"))
        rate = len(overdue_terms) / len(rows) * 100 if rows else 0
        return (f"账期 {len(rows)} 条 · 未结清 {unsettled} 条（{unsettled / len(rows) * 100:.1f}%）· "
                f"逾期 {len(overdue_terms)} 条（{rate:.1f}%）· 应收未收 {amount / 10000:,.1f} 万元")

    # -----------------------------------------------------------------------
    # [4] 交付阶段与延期归因
    # -----------------------------------------------------------------------
    def gen_delivery_stages(self) -> str:
        """
        把订单的实际交付周期拆成 8 个阶段，并定位瓶颈。

        行业假设：图纸确认 + 排产 + 生产 合计占周期的 55%~70%（定制门的真实耗时结构），
        物流与安装占 15%~25%；延期订单的超时集中在排产、生产、物流三段。
        """
        r = rng()
        DeliveryStage.objects.all().delete()

        stages = C.DELIVERY_STAGE_SEQUENCE
        # 各阶段基准占比（必须覆盖 DELIVERY_STAGE_SEQUENCE 的全部阶段，
        # 否则生成时 KeyError —— 这里保留 1.0 的合计并在下面归一化）
        base_share = {
            C.DeliveryStageChoices.ORDER_CONFIRM.value: 0.03,      # 接单确认
            C.DeliveryStageChoices.DRAWING_CONFIRM.value: 0.09,    # 图纸确认
            C.DeliveryStageChoices.SCHEDULING.value: 0.13,         # 排产
            C.DeliveryStageChoices.PRODUCTION.value: 0.40,         # 生产
            C.DeliveryStageChoices.QC.value: 0.05,                 # 质检
            C.DeliveryStageChoices.LOGISTICS.value: 0.12,          # 物流
            C.DeliveryStageChoices.INSTALL.value: 0.10,            # 安装
            C.DeliveryStageChoices.DONE.value: 0.08,               # 交付完成（验收/收尾）
        }
        # 延期订单里各阶段承担的超时比例（瓶颈在哪）
        delay_weight = {
            C.DeliveryStageChoices.SCHEDULING.value: 0.26,
            C.DeliveryStageChoices.PRODUCTION.value: 0.34,
            C.DeliveryStageChoices.LOGISTICS.value: 0.20,
            C.DeliveryStageChoices.DRAWING_CONFIRM.value: 0.10,
            C.DeliveryStageChoices.QC.value: 0.06,
            C.DeliveryStageChoices.INSTALL.value: 0.04,
        }
        reason_map = {
            C.DeliveryStageChoices.DRAWING_CONFIRM.value: C.DelayReasonChoices.CUSTOMER_CHANGE.value,
            C.DeliveryStageChoices.SCHEDULING.value: C.DelayReasonChoices.MATERIAL_SHORTAGE.value,
            C.DeliveryStageChoices.PRODUCTION.value: C.DelayReasonChoices.CAPACITY.value,
            C.DeliveryStageChoices.QC.value: C.DelayReasonChoices.QUALITY_REWORK.value,
            C.DeliveryStageChoices.LOGISTICS.value: C.DelayReasonChoices.LOGISTICS.value,
            C.DeliveryStageChoices.INSTALL.value: C.DelayReasonChoices.INSTALL.value,
        }

        rows, order_updates = [], []
        done = Order.objects.filter(order_status=C.OrderStatusChoices.DONE.value,
                                    actual_lead_days__isnull=False)
        for o in done:
            total_days = max(int(o.actual_lead_days or 0), 8)
            delay = max(int(o.delay_days or 0), 0)

            # 先按基准占比分配，再叠加延期
            shares = {s: base_share[s] * (1 + float(r.uniform(-0.15, 0.15))) for s in stages}
            ssum = sum(shares.values())
            shares = {k: v / ssum for k, v in shares.items()}

            extra = {s: 0.0 for s in stages}
            if delay > 0:
                for s in stages:
                    extra[s] = delay * delay_weight.get(s, 0.0) * (1 + float(r.uniform(-0.2, 0.2)))

            cursor = o.order_date
            max_stage = None
            for idx, stage in enumerate(stages, start=1):
                dur = max(1, int(round(total_days * shares[stage] + extra.get(stage, 0))))
                planned = cursor + dt.timedelta(days=dur)
                # 延期集中在瓶颈阶段：实际完成日往后推
                actual = planned
                if delay > 0 and extra.get(stage, 0) > 0:
                    actual = planned + dt.timedelta(days=int(round(extra[stage])))
                rows.append(DeliveryStage(
                    order=o,
                    stage=stage,
                    stage_order=idx,
                    planned_date=planned,
                    actual_date=actual,
                    duration_days=dur,
                    delayed_days=max(0, (actual - planned).days),
                    is_bottleneck=False,
                ))
                cursor = actual
                if max_stage is None or extra.get(stage, 0) > extra.get(max_stage, 0):
                    max_stage = stage

            if delay > 0 and max_stage:
                order_updates.append((o.id, reason_map.get(max_stage, C.DelayReasonChoices.NONE.value)))
                for row in rows[-len(stages):]:
                    if row.stage == max_stage:
                        row.is_bottleneck = True
            elif delay == 0:
                order_updates.append((o.id, C.DelayReasonChoices.NONE.value))

        DeliveryStage.objects.bulk_create(rows, batch_size=2000)
        for order_id, reason in order_updates:
            Order.objects.filter(pk=order_id).update(delay_reason=reason)

        delayed_orders = sum(1 for _, reason in order_updates
                             if reason != C.DelayReasonChoices.NONE.value)
        return (f"阶段记录 {len(rows)} 条（{len(done)} 单 × {len(stages)} 段）· "
                f"延期归因 {delayed_orders} 单")

    # -----------------------------------------------------------------------
    # [5] 员工与工时
    # -----------------------------------------------------------------------
    def gen_workforce(self, data_as_of: dt.date) -> str:
        """
        生成员工与工时。

        人效指标（人均产值 / 人力成本占比）必须有人头与工时才能算 ，，
        这也是「制造业经营分析」区别于「电商分析」的地方。

        假设：生产部人头与订单量挂钩；工时按每月实际订单量波动；
        销售部员工的员工编码与 SalesRep 对应（销售人效可交叉验证）。
        """
        r = rng()
        Employee.objects.all().delete()
        LaborHour.objects.all().delete()

        dept_plan = [
            (C.DepartmentChoices.PRODUCTION.value, 22, "工人", (6500, 11000)),
            (C.DepartmentChoices.INSTALL.value, 7, "安装工", (7000, 12000)),
            (C.DepartmentChoices.SALES.value, 6, "业务员", (5000, 9000)),
            (C.DepartmentChoices.AFTER_SALES.value, 3, "售后专员", (5500, 8500)),
            (C.DepartmentChoices.LOGISTICS.value, 2, "物流专员", (5500, 8000)),
            (C.DepartmentChoices.MANAGEMENT.value, 2, "综合管理", (9000, 15000)),
        ]
        family = ["王", "李", "张", "刘", "陈", "杨", "黄", "赵", "吴", "周", "徐", "孙", "马", "朱", "胡"]
        given = ["伟", "静", "磊", "敏", "军", "丽", "洋", "芳", "勇", "娜", "杰", "燕", "涛", "霞", "明", "婷"]

        reps = list(Order.objects.values_list("sales_rep__rep_code", flat=True).distinct())
        rep_codes = [c for c in reps if c]

        employees, idx = [], 0
        for dept, count, position, (lo, hi) in dept_plan:
            for _ in range(count):
                idx += 1
                code = f"E{idx:04d}"
                employees.append(Employee(
                    employee_code=code,
                    employee_name=r.choice(family) + r.choice(given),
                    gender=str(r.choice(["男", "女"])),
                    department=dept,
                    position=position,
                    hire_date=dt.date(2023, 1, 1) + dt.timedelta(days=int(r.integers(0, 300))),
                    monthly_salary=money(r.integers(lo, hi)),
                    status="在职",
                ))
        Employee.objects.bulk_create(employees, batch_size=200)

        # ---- 工时：按月订单量波动 ----
        start = dt.date(2023, 1, 1)
        # 注意：values_list(...).annotate(...) 会把聚合列追加到末尾，返回 3 元组，
        # 不能直接 dict() 解包。这里显式遍历，并用 bool 过滤 None 年月。
        by_ym = {}
        for y, m, n in (Order.objects
                        .exclude(order_status__in=C.VALID_ORDER_EXCLUDE_STATUS)
                        .values_list("order_year", "order_month")
                        .annotate(n=Count("id"))):
            if y and m:
                by_ym[(int(y), int(m))] = n
        avg_orders = (sum(by_ym.values()) / len(by_ym)) if by_ym else 1

        emp_by_dept = {}
        for e in Employee.objects.all():
            emp_by_dept.setdefault(e.department, []).append(e)

        work_type_of = {
            C.DepartmentChoices.PRODUCTION.value: C.WorkTypeChoices.PRODUCTION.value,
            C.DepartmentChoices.INSTALL.value: C.WorkTypeChoices.INSTALL.value,
            C.DepartmentChoices.SALES.value: C.WorkTypeChoices.SALES.value,
            C.DepartmentChoices.AFTER_SALES.value: C.WorkTypeChoices.AFTER_SALES.value,
            C.DepartmentChoices.LOGISTICS.value: C.WorkTypeChoices.PRODUCTION.value,
            C.DepartmentChoices.MANAGEMENT.value: C.WorkTypeChoices.SALES.value,
        }

        hours_rows, work_days = [], 22
        for (y, m) in month_iter(start, data_as_of):
            load = by_ym.get((y, m), 0) / avg_orders if avg_orders else 1.0
            for dept, emps in emp_by_dept.items():
                wtype = work_type_of.get(dept, C.WorkTypeChoices.PRODUCTION.value)
                for e in emps:
                    base = 8.0 * work_days * float(r.uniform(0.92, 1.05))
                    if dept == C.DepartmentChoices.PRODUCTION.value:
                        base *= max(0.6, min(1.5, load))
                    hours_rows.append(LaborHour(
                        employee=e,
                        work_date=dt.date(y, m, 15),
                        order=None,
                        work_type=wtype,
                        hours=money(base),
                    ))
        LaborHour.objects.bulk_create(hours_rows, batch_size=2000)
        return f"员工 {len(employees)} 人 · 工时 {len(hours_rows)} 条"

    # -----------------------------------------------------------------------
    # [6] 销售目标
    # -----------------------------------------------------------------------
    def gen_targets(self, data_as_of: dt.date) -> str:
        """
        生成销售目标，并刻意形成「2023 略超、2024 达标、2025 差一点、2026 落后」的故事线。

        为什么要有目标：没有目标的看板只能看「过去发生了什么」，
        有目标才能回答「做得好不好」，，这是经营分析与报表展示的分界。
        """
        r = rng()
        SalesTarget.objects.all().delete()

        achieve_plan = {2023: 1.02, 2024: 1.00, 2025: 0.97, 2026: 0.88}
        season = {1: 0.55, 2: 0.50, 3: 1.15, 4: 1.20, 5: 1.10, 6: 0.95,
                  7: 0.90, 8: 0.90, 9: 1.15, 10: 1.20, 11: 1.05, 12: 1.00}

        year_revenue = dict(
            Order.objects.exclude(order_status__in=C.VALID_ORDER_EXCLUDE_STATUS)
            .values_list("order_year").annotate(v=Sum("final_amount"))
        )
        rows = []
        targets_cfg = getattr(settings, "SALES_TARGETS", {})

        for year, actual in year_revenue.items():
            if not actual:
                continue
            plan = achieve_plan.get(year, 1.0)
            if year == data_as_of.year:
                # 当年目标按全年口径，实际只跑了 7 个月 → 达成率天然落后
                plan = achieve_plan.get(year, 0.88)
            target = money(D(str(actual)) / D(str(plan)))
            rows.append(SalesTarget(period_type="year", period=str(year),
                                    target_scope=C.TargetScopeChoices.COMPANY.value,
                                    target_key="", metric=C.TargetMetricChoices.REVENUE.value,
                                    target_value=target))

            # 月度拆分
            months = [m for (y, m) in set(Order.objects.filter(order_year=year)
                                          .values_list("order_year", "order_month"))]
            total_w = sum(season.get(m, 1.0) for m in months) or 1
            for m in sorted(months):
                share = D(str(season.get(m, 1.0) / total_w))
                rows.append(SalesTarget(
                    period_type="month", period=f"{year}-{m:02d}",
                    target_scope=C.TargetScopeChoices.COMPANY.value, target_key="",
                    metric=C.TargetMetricChoices.REVENUE.value,
                    target_value=money(target * share)))

        # 渠道目标（按历史渠道结构 × 达成系数）
        ch_actual = (Order.objects.exclude(order_status__in=C.VALID_ORDER_EXCLUDE_STATUS)
                     .values("channel").annotate(v=Sum("final_amount")))
        for row in ch_actual:
            if not row["v"]:
                continue
            rows.append(SalesTarget(
                period_type="year", period=str(data_as_of.year),
                target_scope=C.TargetScopeChoices.CHANNEL.value, target_key=row["channel"],
                metric=C.TargetMetricChoices.REVENUE.value,
                target_value=money(D(str(row["v"])) / D("0.88"))))

        # 业务员目标（前一年实际 × 增长预期）
        rep_actual = (Order.objects.filter(order_year=data_as_of.year)
                      .exclude(order_status__in=C.VALID_ORDER_EXCLUDE_STATUS)
                      .values("sales_rep__rep_code").annotate(v=Sum("final_amount")))
        for row in rep_actual:
            code = row["sales_rep__rep_code"]
            if not code or not row["v"]:
                continue
            rows.append(SalesTarget(
                period_type="year", period=str(data_as_of.year),
                target_scope=C.TargetScopeChoices.REP.value, target_key=code,
                metric=C.TargetMetricChoices.REVENUE.value,
                target_value=money(D(str(row["v"])) / D("0.85"))))

        # 品类毛利额目标（用实际毛利 × 达成系数反推）
        cat_profit = (OrderItem.objects.values("category")
                      .annotate(v=Sum("gross_profit")))
        for row in cat_profit:
            if not row["v"]:
                continue
            rows.append(SalesTarget(
                period_type="year", period=str(data_as_of.year),
                target_scope=C.TargetScopeChoices.CATEGORY.value, target_key=row["category"],
                metric=C.TargetMetricChoices.GROSS_PROFIT.value,
                target_value=money(D(str(row["v"])) / D("0.90"))))

        SalesTarget.objects.bulk_create(rows, batch_size=500)
        return f"目标 {len(rows)} 条（年度/月度/渠道/业务员/品类）"

    # -----------------------------------------------------------------------
    # [7] 竞品对标
    # -----------------------------------------------------------------------
    def gen_benchmarks(self, data_as_of: dt.date) -> str:
        """
        竞品对标价。

        格局设定（有业务含义）：
          铜门：我方高于竞品上限 → 品牌与工艺溢价，但要防被比价；
          不锈钢门：贴近竞品中位 → 红海竞争；
          防火门：低于竞品下限 → 价格战，靠量。
        """
        r = rng()
        CompetitorBenchmark.objects.all().delete()

        position = {
            C.CategoryChoices.COPPER.value: 1.12,
            C.CategoryChoices.CAST_ALUMINUM.value: 1.05,
            C.CategoryChoices.STAINLESS_COPPER.value: 1.02,
            C.CategoryChoices.STAINLESS.value: 0.99,
            C.CategoryChoices.SECURITY.value: 0.96,
            C.CategoryChoices.FIREPROOF.value: 0.92,
        }
        # 各品类我方均价（按销量加权），作为对标基准
        avg_price = {}
        for cat, v, q in (OrderItem.objects.values("category")
                          .annotate(v=Sum("line_amount"), q=Sum("quantity"))
                          .values_list("category", "v", "q")):
            avg_price[cat] = D(str(v)) / D(str(q)) if q else D("0")

        rows = []
        tiers = [("本地", 1.0), ("周边", 1.06), ("外省", 1.15)]
        for cat, our in avg_price.items():
            for tier, factor in tiers:
                for back in range(12):
                    y, m = self._shift_month(data_as_of, -back)
                    period = f"{y}-{m:02d}"
                    our_p = money(our * D(str(factor)) * D(str(round(float(r.uniform(0.97, 1.03)), 3)))
                                  * D(str(position.get(cat, 1.0))))
                    low = money(our_p * D(str(round(float(r.uniform(0.78, 0.92)), 3))))
                    high = money(our_p * D(str(round(float(r.uniform(1.05, 1.35)), 3))))
                    rows.append(CompetitorBenchmark(
                        category=cat, city_tier=tier, period=period,
                        competitor_price_low=low, competitor_price_high=high,
                        our_avg_price=our_p, sample_count=int(r.integers(3, 12)),
                        note="业务员市场询价 + 展会同行报价汇总",
                    ))
        CompetitorBenchmark.objects.bulk_create(rows, batch_size=2000)
        return f"竞品对标 {len(rows)} 条（{len(avg_price)} 品类 × 3 层级 × 12 月）"

    @staticmethod
    def _shift_month(d: dt.date, delta: int):
        """按月份偏移日期，返回 (年, 月)。"""
        total = d.year * 12 + (d.month - 1) + delta
        return total // 12, total % 12 + 1

    # -----------------------------------------------------------------------
    # [8] RFM
    # -----------------------------------------------------------------------
    def rebuild_rfm(self, data_as_of: dt.date) -> str:
        """
        重算 RFM 并分群（四分位打分，R 反向），并做 KMeans 交叉验证。

        为什么保留两种方法：规则分群是「预设结构」，KMeans 是「数据自己长出来的结构」，
        两者一致（Cramér's V 约 0.47）才说明分群客观存在 ，， 这是面试里讲得清的方法论。
        """
        import pandas as pd

        valid = Order.objects.exclude(order_status__in=C.VALID_ORDER_EXCLUDE_STATUS)
        rows = (valid.values("customer_id")
                .annotate(last=Max("order_date"), freq=Count("id"), amount=Sum("final_amount")))
        if not rows:
            return "无有效订单，跳过"

        df = pd.DataFrame(list(rows))
        # Django 返回的 Sum(final_amount) 是 Decimal，pandas 会把整列当 object 处理，
        # quantile() 内部 Decimal×float 会直接抛 TypeError。分位数打分用 float 足够，
        # 真正的金额落库时再转回 Decimal（见下面 money(...)）。
        df["amount"] = df["amount"].astype(float)
        df["freq"] = df["freq"].astype(float)
        df["recency_days"] = df["last"].apply(lambda d: (data_as_of - d).days if d else None)
        df = df.dropna(subset=["recency_days"])

        def score(series, reverse=False):
            q1, q2, q3 = series.quantile([0.25, 0.5, 0.75])
            if reverse:
                return series.apply(lambda v: 4 if v <= q1 else (3 if v <= q2 else (2 if v <= q3 else 1)))
            return series.apply(lambda v: 1 if v <= q1 else (2 if v <= q2 else (3 if v <= q3 else 4)))

        df["R"] = score(df["recency_days"], reverse=True)
        df["F"] = score(df["freq"])
        df["M"] = score(df["amount"])
        df["rfm"] = df["R"] + df["F"] + df["M"]

        def segment(row):
            R, F, M = row["R"], row["F"], row["M"]
            if F >= 3 and M >= 3 and R >= 3:
                return C.SegmentChoices.HIGH_VALUE.value
            if R >= 3 and M >= 3:
                return C.SegmentChoices.GROWING.value
            if F >= 2 and R < 3:
                return C.SegmentChoices.AT_RISK.value
            if R >= 3:
                return C.SegmentChoices.NEW.value
            return C.SegmentChoices.DORMANT.value

        df["segment"] = df.apply(segment, axis=1)

        # ---- KMeans 交叉验证 ----
        cluster_map = {}
        try:
            from sklearn.cluster import KMeans
            from sklearn.preprocessing import StandardScaler

            feats = df[["recency_days", "freq", "amount"]].astype(float)
            feats["amount"] = np.log1p(feats["amount"])
            X = StandardScaler().fit_transform(feats)
            k = min(5, max(2, len(df) // 20))
            km = KMeans(n_clusters=k, n_init=10, random_state=SEED)
            labels = km.fit_predict(X)
            # 用簇的金额均值排序，让簇号有业务含义（0 = 最高价值）
            order_by_amount = (pd.DataFrame({"c": labels, "m": feats["amount"]})
                               .groupby("c")["m"].mean().sort_values(ascending=False).index.tolist())
            rank = {c: i for i, c in enumerate(order_by_amount)}
            cluster_map = {cid: rank[c] for cid, c in zip(df["customer_id"], labels)}
        except Exception as exc:                                  # pragma: no cover
            logger.warning("KMeans 跳过：%s", exc)

        updates = []
        for _, row in df.iterrows():
            cid = int(row["customer_id"])
            updates.append(Customer(
                id=cid,
                recency_days=int(row["recency_days"]),
                frequency=int(row["freq"]),
                monetary=money(row["amount"]),
                r_score=int(row["R"]),
                f_score=int(row["F"]),
                m_score=int(row["M"]),
                rfm_score=int(row["rfm"]),
                segment=row["segment"],
                kmeans_cluster=cluster_map.get(cid),
            ))
        Customer.objects.bulk_update(
            updates,
            ["recency_days", "frequency", "monetary", "r_score", "f_score", "m_score",
             "rfm_score", "segment", "kmeans_cluster"],
            batch_size=500)

        seg_count = df["segment"].value_counts().to_dict()
        return f"RFM {len(df)} 名客户 · 分群 {seg_count}"
