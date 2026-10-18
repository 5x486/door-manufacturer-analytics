"""数据质量规则元数据。

为什么把规则元数据与规则实现分开
--------------------------------
``checks.py`` 里的函数是「怎么算」，本文件是「这条规则是什么、为什么要有它」。
分开的真实理由是：**规则要进数据库**（``DQCheck`` 表），供数据质量页面展示、
供业务同事阅读、供审计追溯。把中文说明硬编码在可执行函数里，
就只能靠程序员口头解释「这条规则为什么存在」，而数据质量的争议 90% 出在
「这条规则该不该算问题」上，不在「这条规则怎么算」上。

三个字段承担三种职责：
- ``description``：给业务看的自然语言解释（这条规则检查什么）；
- ``why``：**为什么需要它**，即不检查会出什么错，写「为什么」是为了让后人
  在调整阈值时知道自己在动什么，而不是看到告警就顺手放宽；
- ``threshold``：允许出现的异常行数上限（不是比例）。阈值一律用「行数」，
  因为行数对业务更直观（「有 3 张订单金额对不上」比「异常率 0.12%」更好沟通），
  且不会因为数据量翻倍而自动放宽标准。

严重级别
--------
- ``error``：破坏口径正确性（主键重复、外键悬空、金额对不上）。
  一旦失败，看板结论不可信，应阻断发布。
- ``warning``：业务合理性存疑（准时率越界、售后率偏高）。
  需要人工确认，但不一定阻断。
两者都超过阈值才分别判 ``fail`` / ``warn``，临界内一律 ``pass``。
"""

RULE_CATEGORIES = [
    "主键唯一",
    "外键完整",
    "金额对账",
    "日期合理",
    "业务合理",
]

SEVERITY_LEVELS = ["error", "warning"]

# 每条规则的元数据。
# 字段说明：
#   rule_code   稳定编码（写入 dq_check 表，改名不改码，便于历史结果对比）
#   rule_name   规则名（中文，页面展示）
#   category    规则分类（见 RULE_CATEGORIES）
#   target_table 主要检查对象（业务表名，便于按表排查）
#   severity    error / warning
#   threshold   允许异常行数上限
#   description 检查什么
#   why         为什么需要这条规则（不查会出什么错）
#   sql_text    规则的「可读伪 SQL」，用于 DQCheck 表与页面展示，
#               实际执行走 ORM（更安全、可移植），这里存的是口径说明，
#               让懂 SQL 的人能直接看懂规则想干什么。
RULES = [
    # ---------------- 主键唯一 ----------------
    {
        "rule_code": "PK_ORDERS_NO",
        "rule_name": "订单号唯一",
        "category": "主键唯一",
        "target_table": "sales_order",
        "severity": "error",
        "threshold": 0,
        "description": "order_no 不得重复，同一订单号只能有一条订单记录。",
        "why": "订单号是订单与明细、付款、售后的唯一连接点。重复订单号会让"
               "「一笔订单的金额」在关联时被放大成两倍，直接污染成交额与毛利。",
        "sql_text": "SELECT order_no, COUNT(*) FROM sales_order GROUP BY order_no HAVING COUNT(*) > 1;",
    },
    {
        "rule_code": "PK_ITEMS_KEY",
        "rule_name": "订单明细唯一",
        "category": "主键唯一",
        "target_table": "sales_orderitem",
        "severity": "error",
        "threshold": 0,
        "description": "同一订单内同一产品（item_no 业务键）不得重复成行。",
        "why": "明细重复会把成本与收入同时放大，且不会触发任何报错，"
               "这类「静默翻倍」是导入任务最容易留下的事故。",
        "sql_text": "SELECT item_no, COUNT(*) FROM sales_orderitem GROUP BY item_no HAVING COUNT(*) > 1;",
    },
    {
        "rule_code": "PK_PAYMENTS_NO",
        "rule_name": "付款流水号唯一",
        "category": "主键唯一",
        "target_table": "sales_payment",
        "severity": "error",
        "threshold": 0,
        "description": "payment_no 不得重复。",
        "why": "付款流水重复会让回款额虚增，进而让「回款率」超过 100%、"
               "账龄分布整体失真，是财务口径最不能接受的错误。",
        "sql_text": "SELECT payment_no, COUNT(*) FROM sales_payment GROUP BY payment_no HAVING COUNT(*) > 1;",
    },
    {
        "rule_code": "PK_CUSTOMERS_CODE",
        "rule_name": "客户编码唯一",
        "category": "主键唯一",
        "target_table": "masterdata_customer",
        "severity": "error",
        "threshold": 0,
        "description": "customer_code 不得重复。",
        "why": "客户重复会造成「同一个客户被算成两个」，"
               "直接击穿复购率、留存率与客户贡献集中度这三个核心指标。",
        "sql_text": "SELECT customer_code, COUNT(*) FROM masterdata_customer GROUP BY customer_code HAVING COUNT(*) > 1;",
    },
    {
        "rule_code": "PK_PRODUCTS_CODE",
        "rule_name": "产品编码唯一",
        "category": "主键唯一",
        "target_table": "masterdata_product",
        "severity": "error",
        "threshold": 0,
        "description": "product_code 不得重复。",
        "why": "产品重复会导致同一 SKU 的成本标准与毛利出现两套值，"
               "「SKU 毛利率」排名将不可信。",
        "sql_text": "SELECT product_code, COUNT(*) FROM masterdata_product GROUP BY product_code HAVING COUNT(*) > 1;",
    },
    # ---------------- 外键完整 ----------------
    {
        "rule_code": "FK_ORDER_CUSTOMER",
        "rule_name": "订单客户外键完整",
        "category": "外键完整",
        "target_table": "sales_order",
        "severity": "error",
        "threshold": 0,
        "description": "每张订单都必须关联到有效客户。",
        "why": "订单缺客户会同时丢失客户维度与渠道维度："
               "该订单无法进入客户价值、留存、区域分布中的任何一张报表，"
               "却仍会计入成交额，造成「总额对得上、明细对不上」的诡异现象。",
        "sql_text": "SELECT o.order_no FROM sales_order o LEFT JOIN masterdata_customer c "
                    "ON o.customer_id = c.id WHERE c.id IS NULL;",
    },
    {
        "rule_code": "FK_ITEM_ORDER",
        "rule_name": "明细订单外键完整",
        "category": "外键完整",
        "target_table": "sales_orderitem",
        "severity": "error",
        "threshold": 0,
        "description": "每条订单明细都必须关联到有效订单。",
        "why": "悬空明细不会出现在任何订单口径里，但会计入产品成本与品类销量，"
               "让「品类销量」与「品类收入」来自不同样本集。",
        "sql_text": "SELECT i.item_no FROM sales_orderitem i LEFT JOIN sales_order o "
                    "ON i.order_id = o.id WHERE o.id IS NULL;",
    },
    {
        "rule_code": "FK_ITEM_PRODUCT",
        "rule_name": "明细产品外键完整",
        "category": "外键完整",
        "target_table": "sales_orderitem",
        "severity": "error",
        "threshold": 0,
        "description": "每条订单明细都必须关联到有效产品。",
        "why": "明细缺产品则无法获得成本标准与品类归属，"
               "会让毛利分析出现「无成本行」，把毛利率拉高到不真实的水平。",
        "sql_text": "SELECT i.item_no FROM sales_orderitem i LEFT JOIN masterdata_product p "
                    "ON i.product_id = p.id WHERE p.id IS NULL;",
    },
    {
        "rule_code": "FK_PAYMENT_ORDER",
        "rule_name": "付款订单外键完整",
        "category": "外键完整",
        "target_table": "sales_payment",
        "severity": "error",
        "threshold": 0,
        "description": "每笔付款都必须关联到有效订单。",
        "why": "无主付款会导致回款额与订单不匹配、账期表的 paid_amount 对不上，"
               "最终表现为「回款率忽高忽低」而查不出原因。",
        "sql_text": "SELECT p.payment_no FROM sales_payment p LEFT JOIN sales_order o "
                    "ON p.order_id = o.id WHERE o.id IS NULL;",
    },
    {
        "rule_code": "FK_AFTER_ORDER",
        "rule_name": "售后订单外键完整",
        "category": "外键完整",
        "target_table": "sales_aftersales",
        "severity": "error",
        "threshold": 0,
        "description": "每条售后工单都必须关联到有效订单。",
        "why": "售后无法归到订单，就无法判断责任品类与责任渠道，"
               "「售后率」「售后成本率」都算不出来，质量改进也就无从下手。",
        "sql_text": "SELECT a.after_no FROM sales_aftersales a LEFT JOIN sales_order o "
                    "ON a.order_id = o.id WHERE o.id IS NULL;",
    },
    # ---------------- 金额对账 ----------------
    {
        "rule_code": "AMT_FINAL_FORMULA",
        "rule_name": "订单终价公式一致",
        "category": "金额对账",
        "target_table": "sales_order",
        "severity": "error",
        "threshold": 0,
        "description": "final_amount ≈ total_amount × (1 - discount) + freight，容差 0.01 元。",
        "why": "终价是成交额唯一口径，也是所有金额指标的基数。若终价与"
               "总额/让利/运费三者不自洽，那么「折扣侵蚀率」「让利金额」就只是"
               "一段无法解释的数字，业务会立刻质疑整张报表。容差留到 2 行是因为"
               "真实业务里偶有手工抹零，这类差异可解释、不必阻断。",
        "sql_text": "SELECT order_no, final_amount, total_amount, discount, freight "
                    "FROM sales_order WHERE ABS(final_amount - (total_amount * (1 - discount) + freight)) > 0.01;",
    },
    {
        "rule_code": "AMT_ITEM_SUM",
        "rule_name": "明细合计与订单总额一致",
        "category": "金额对账",
        "target_table": "sales_orderitem",
        "severity": "error",
        "threshold": 0,
        "description": "订单明细 line_amount 合计应等于订单 total_amount（容差 0.01）。",
        "why": "这是「自下而上」与「自上而下」两条金额路径的对账点。"
               "一旦不成立，说明明细漏导或订单金额被改过，"
               "此时用明细算的品类收入与用订单算的成交额必然打架。",
        "sql_text": "SELECT o.order_no, o.total_amount, SUM(i.line_amount) FROM sales_order o "
                    "JOIN sales_orderitem i ON i.order_id = o.id GROUP BY o.order_no, o.total_amount "
                    "HAVING ABS(o.total_amount - SUM(i.line_amount)) > 0.01;",
    },
    {
        "rule_code": "AMT_COST_POSITIVE",
        "rule_name": "成本金额非负",
        "category": "金额对账",
        "target_table": "sales_orderitemcost",
        "severity": "error",
        "threshold": 0,
        "description": "六类成本明细的金额必须 ≥ 0，且订单行成本不得为负。",
        "why": "负成本等价于「凭空产生利润」。现实中只可能来自冲销凭证或"
               "生成器参数越界，若进了毛利分析，会造出一个毛利率奇高的假爆款。",
        "sql_text": "SELECT id, cost_type, amount FROM sales_orderitemcost WHERE amount < 0;",
    },
    {
        "rule_code": "AMT_PAYMENT_POSITIVE",
        "rule_name": "付款金额规范",
        "category": "金额对账",
        "target_table": "sales_payment",
        "severity": "warning",
        "threshold": 0,
        "description": "非退款付款必须为正数；退款必须为负或零并按退款标记登记。",
        "why": "付款方向搞反会让回款曲线出现负值尖峰，"
               "也会让 13 周现金流预测把「退款」误当成「回款」。",
        "sql_text": "SELECT payment_no, stage, amount, is_refund FROM sales_payment "
                    "WHERE (is_refund = 0 AND amount <= 0) OR (is_refund = 1 AND amount > 0);",
    },
    # ---------------- 日期合理 ----------------
    {
        "rule_code": "DATE_DELIVERY_AFTER_ORDER",
        "rule_name": "交付不早于下单",
        "category": "日期合理",
        "target_table": "sales_order",
        "severity": "error",
        "threshold": 0,
        "description": "actual_delivery_date 必须 ≥ order_date。",
        "why": "「时间穿越」会让交付周期变成负数，平均交付周期、履约速度"
               "全都被拉低，而且这类错误在图上表现为「周期突然变优」，极具误导性。",
        "sql_text": "SELECT order_no, order_date, actual_delivery_date FROM sales_order "
                    "WHERE actual_delivery_date IS NOT NULL AND actual_delivery_date < order_date;",
    },
    {
        "rule_code": "DATE_PAYMENT_AFTER_ORDER",
        "rule_name": "付款日不早于下单日",
        "category": "日期合理",
        "target_table": "sales_payment",
        "severity": "error",
        "threshold": 0,
        "description": "payment_date 必须 ≥ 所属订单的 order_date。",
        "why": "收款早于下单在门业里只可能是「订金预收」，但本项目的数据口径"
               "不包含这种情形；出现即说明日期列被写错，会让回款账龄与"
               "现金流预测的落点整体前移。",
        "sql_text": "SELECT p.payment_no, p.payment_date, o.order_date FROM sales_payment p "
                    "JOIN sales_order o ON o.id = p.order_id WHERE p.payment_date < o.order_date;",
    },
    {
        "rule_code": "DATE_NO_FUTURE",
        "rule_name": "无超出数据截止日的未来日期",
        "category": "日期合理",
        "target_table": "sales_order",
        "severity": "warning",
        "threshold": 0,
        "description": "订单日期、实际交付日、付款日均不得晚于 settings.DATA_AS_OF。",
        "why": "数据截止日是全系统「截至今天」的锚点。若存在截止日之后的记录，"
               "同比环比、账龄、目标达成率都会掺入未来数据，"
               "而这类污染往往只在某个页面上体现，极难排查。",
        "sql_text": "SELECT order_no, order_date, actual_delivery_date FROM sales_order "
                    "WHERE order_date > :data_as_of OR actual_delivery_date > :data_as_of;",
    },
    {
        "rule_code": "DATE_STAGE_SEQUENCE",
        "rule_name": "交付阶段日期不倒挂",
        "category": "日期合理",
        "target_table": "sales_deliverystage",
        "severity": "warning",
        "threshold": 5,
        "description": "同一订单各阶段的实际日期应随 stage_order 递增。",
        "why": "阶段倒挂意味着工序顺序在数据上被破坏，"
               "「瓶颈阶段」的判定会落到错误的工序上，让生产改进方向跑偏。"
               "阈值放宽到 5 行，是因为跨月排产偶有同日完成多道工序的情况。",
        "sql_text": "SELECT s1.order_id, s1.stage, s2.stage FROM sales_deliverystage s1 "
                    "JOIN sales_deliverystage s2 ON s1.order_id = s2.order_id "
                    "AND s2.stage_order = s1.stage_order + 1 WHERE s2.actual_date < s1.actual_date;",
    },
    # ---------------- 业务合理 ----------------
    {
        "rule_code": "BIZ_CANCELLED_NO_DELIVERY",
        "rule_name": "取消订单无实际交付",
        "category": "业务合理",
        "target_table": "sales_order",
        "severity": "error",
        "threshold": 0,
        "description": "order_status = 已取消 的订单不得有 actual_delivery_date 与准时标记。",
        "why": "有效口径是「排除已取消」，若取消单带着交付数据，"
               "一旦某处漏写过滤条件，就会把没发生的生意算进成交额，"
               "而且数字看起来完全正常。",
        "sql_text": "SELECT order_no, order_status, actual_delivery_date, is_on_time FROM sales_order "
                    "WHERE order_status = '已取消' AND (actual_delivery_date IS NOT NULL OR is_on_time <> -1);",
    },
    {
        "rule_code": "BIZ_VALID_ORDER_HAS_ITEM",
        "rule_name": "有效订单必须有明细",
        "category": "业务合理",
        "target_table": "sales_order",
        "severity": "error",
        "threshold": 0,
        "description": "未取消的订单必须至少有一条订单明细。",
        "why": "「零明细订单」在成交额里有金额、在品类与成本分析里却完全不存在，"
               "会同时造成「收入与成本不匹配」和「品类收入合计数对不上总成交额」。",
        "sql_text": "SELECT o.order_no FROM sales_order o LEFT JOIN sales_orderitem i "
                    "ON i.order_id = o.id WHERE o.order_status <> '已取消' "
                    "GROUP BY o.order_no HAVING COUNT(i.id) = 0;",
    },
    {
        "rule_code": "BIZ_ON_TIME_RATE",
        "rule_name": "准时交付率在合理区间",
        "category": "业务合理",
        "target_table": "sales_order",
        "severity": "warning",
        "threshold": 0,
        "description": "准时交付率应落在 0% ~ 100% 之间（样本充足时不低于 60%）。",
        "why": "准时率越界说明 is_on_time 标记被写坏；"
               "而低于 60% 虽然不是数据错误，却是经营上的红灯，"
               "这条规则的告警本身就是给管理层看的信号。",
        "sql_text": "SELECT COUNT(*) total, SUM(is_on_time = 1) on_time FROM sales_order "
                    "WHERE is_on_time IN (0, 1);",
    },
    {
        "rule_code": "BIZ_GROSS_MARGIN_RANGE",
        "rule_name": "毛利率在业务合理区间",
        "category": "业务合理",
        "target_table": "sales_orderitem",
        "severity": "warning",
        "threshold": 5,
        "description": "单行毛利率应落在 -20% ~ 60% 之间。",
        "why": "低于 -20% 意味着亏本卖（接单让利极端化），高于 60% 意味着"
               "成本漏记或售价录入错误。两者都会让「品类利润」结论失真，"
               "阈值给到 5 行是为了容忍个别清库存的亏损单。",
        "sql_text": "SELECT id, line_amount, total_cost FROM sales_orderitem "
                    "WHERE line_amount > 0 AND (1 - total_cost / line_amount) NOT BETWEEN -0.20 AND 0.60;",
    },
    {
        "rule_code": "BIZ_AFTER_SALES_RATE",
        "rule_name": "售后工单率阈值",
        "category": "业务合理",
        "target_table": "sales_aftersales",
        "severity": "warning",
        "threshold": 0,
        "description": "售后工单数 ÷ 有效订单数 < 15%。",
        "why": "定制门行业售后率通常在 5%~12%。突破 15% 说明要么质量出问题，"
               "要么工单系统把咨询也当成工单录入，两种情况都需要人工介入确认。",
        "sql_text": "SELECT (SELECT COUNT(*) FROM sales_aftersales) / "
                    "(SELECT COUNT(*) FROM sales_order WHERE order_status <> '已取消');",
    },
    {
        "rule_code": "BIZ_AFTER_SALES_CATEGORY",
        "rule_name": "售后品类值域合法",
        "category": "业务合理",
        "target_table": "sales_aftersales",
        "severity": "warning",
        "threshold": 0,
        "description": "售后记录的 category 必须落在 CATEGORY_LIST 之内。",
        "why": "上游 CSV 的 product_id 列实际存的是品类名。这条规则专门捕捉"
               "这种「列名与语义不一致」的情况，一旦上游改了取值口径，"
               "这里会立刻亮灯，而不是让错误静默进入品类售后率。",
        "sql_text": "SELECT after_no, category FROM sales_aftersales WHERE category NOT IN (:category_list);",
    },
    {
        "rule_code": "BIZ_QUANTITY_POSITIVE",
        "rule_name": "明细数量为正",
        "category": "业务合理",
        "target_table": "sales_orderitem",
        "severity": "error",
        "threshold": 0,
        "description": "订单明细 quantity 必须 ≥ 1。",
        "why": "数量为 0 或负数的明细会让「销量」出现反直觉的下降，"
               "同时让面积/单价类指标（单位面积造价）产生除零或负值。",
        "sql_text": "SELECT item_no, quantity FROM sales_orderitem WHERE quantity IS NULL OR quantity < 1;",
    },
]

# 快捷索引：rule_code → 元数据
RULE_MAP = {rule["rule_code"]: rule for rule in RULES}

# 需要写入 DQCheck 表的字段（与 apps.dq.models.DQCheck 对应）
DQ_CHECK_FIELDS = (
    "rule_code",
    "rule_name",
    "category",
    "target_table",
    "description",
    "sql_text",
    "severity",
    "threshold",
    "is_active",
    "order",
)


def iter_rules(category: str | None = None) -> list:
    """按分类取规则元数据；``category=None`` 表示全部。

    供 ``checks.CHECKS`` 组装与 ``sync_dq_checks`` 命令共用，
    保证「数据库里的规则」与「代码里跑的规则」永远是同一份。
    """
    if category is None:
        return list(RULES)
    return [rule for rule in RULES if rule["category"] == category]
