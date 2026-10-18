# 数据字典（Data Dictionary）

> 业务场景：浙江嘉兴桐乡一家小型定制门厂（2023 年起步，3 位合伙人）。
> 覆盖时间：2023-01-01 ~ 2026-07-31。
> 说明：所有数据为模拟生成，字段口径按真实业务习惯设计。

---

## 1. products.csv —— 产品目录（48 行，粒度：SKU）

| 字段 | 类型 | 说明 | 示例 |
|---|---|---|---|
| product_id | string | 产品唯一编码 | P001 |
| product_name | string | 产品名称（风格+材质+品类） | 新中式紫铜门·静音 |
| category | string | 品类 | 铜门 / 不锈钢门 / 不锈钢铜门 / 铸铝门 / 甲级防盗门 / 防火门 |
| material | string | 材质 | 紫铜 / 黄铜 / 304不锈钢 / 铸铝 / 镀锌钢板 / 钢质防火 |
| surface | string | 表面工艺 | 氟碳漆 / 铜雕工艺 / 拉丝 / 烤漆 / 粉末喷涂 |
| style | string | 风格 | 新中式 / 欧式 / 轻奢 / 现代 / 简约 / 甲级 / 乙级 |
| feature | string | 附加配置 | 标准 / 静音 / 带可视猫眼 / 智能锁配套 |
| price_multiplier | float | 附加配置的价格系数 | 1.0 / 1.05 / 1.08 / 1.10 |
| base_price | int | 基础价（元/樘，不含尺寸与配置加成） | 8000 |
| std_width | int | 标准宽（mm） | 1100 |
| std_height | int | 标准高（mm） | 2200 |
| std_area | int | 标准面积（mm²） | 2420000 |
| lead_min / lead_max | int | 生产周期范围（天） | 45 / 70 |
| defect_rate | float | 该品类基准售后率（用于售后模拟） | 0.06 |

**口径**：`base_price × price_multiplier × 尺寸系数 × 非标系数 ≈ 实际单价`。铜门生产周期最长（45~70 天），免漆/防火类较短。

## 2. stores.csv —— 门店/渠道（3 行）

| 字段 | 类型 | 说明 | 示例 |
|---|---|---|---|
| store_id | string | 门店编码 | S01 |
| store_name | string | 门店名称 | 桐乡工厂展厅 |
| province / city | string | 所在地 | 浙江 / 嘉兴桐乡 |
| store_type | string | 类型 | 直营门店 / 线上 / 工程 |
| status | string | 状态 | 营业中 |

三个渠道：**S01 桐乡工厂展厅**（唯一实体展厅）、**S02 线上/抖音接单**、**S03 装企工程部**。

## 3. sales_reps.csv —— 业务员（6 人）

| 字段 | 类型 | 说明 |
|---|---|---|
| rep_id | string | 业务员编码（R001…） |
| rep_name | string | 姓名（虚构） |
| gender | string | 男 / 女 |
| store_id | string | 所属门店（关联 stores） |
| hire_date | date | 入职日期 |
| level | string | 初级 / 中级 / 高级 / 资深 |
| status | string | 在职 |

## 4. customers.csv —— 客户（1,500 行）

| 字段 | 类型 | 说明 |
|---|---|---|
| customer_id | string | 客户编码（C00001…） |
| customer_name | string | 姓名 / 公司名（虚构） |
| customer_type | string | 个人家装 / 装修公司 / 工程客户 |
| gender | string | 男 / 女（企业客户为 "—"） |
| age | int | 年龄（企业客户为空） |
| phone | string | 电话（虚构） |
| province / city | string | 所在地 |
| address | string | 地址（虚构） |
| channel | string | 首次来源渠道：抖音短视频 / 老客户转介 / 门店到访 / 线上渠道 / 展会营销 / 装修公司合作 |
| register_date | date | 登记/建档日期（首次下单前 20~400 天；未成交线索为随机） |
| member_level | string | 普通 / 银卡 / 金卡 / 钻石（按累计消费：≥1.2万银 / ≥3.5万金 / ≥8万钻石） |
| total_orders | int | 累计有效订单数 |
| total_spend | float | 累计有效消费金额（元） |
| first_order_date / last_order_date | date | 首单/最近一单日期 |
| is_lead | int | 是否未成交线索：1=仅登记未下单（约 18%） |

**口径**：`is_lead=1` 的客户是渠道转化分析的分母；`total_spend` 只统计非取消订单。

## 5. orders.csv —— 订单主表（2,401 行）

| 字段 | 类型 | 说明 |
|---|---|---|
| order_id | string | 订单号（DD + 日期 + 序号） |
| customer_id | string | 客户编码 |
| customer_type | string | 个人家装 / 装修公司 / 工程客户 |
| channel | string | 订单来源渠道（同客户获客渠道） |
| store_id | string | 成交门店 |
| sales_rep_id | string | 负责业务员 |
| order_type | string | 零售 / 装企合作 / 工程 |
| delivery_type | string | 包安装 / 自提 |
| order_date | date | 下单日期 |
| order_status | string | 已完成 / 生产中 / 待发货 / 已取消 |
| planned_delivery_date | date | 计划交付日期（下单 + 生产周期） |
| actual_delivery_date | date | 实际交付日期（已取消/在产为空） |
| is_on_time | int | 是否按时交付（1=是 / 0=否 / 空=未交付） |
| total_amount | float | 商品金额合计（折扣前） |
| discount | float | 折扣率（零售 0~3%，装企 4~8%，工程 6~12%） |
| freight | float | 运费（多数为 0） |
| final_amount | float | **成交金额 = total_amount × (1-discount) + freight** |
| lead_days | int | 生产周期（天，按订单金额最大品类取值） |
| dominant_category | string | 金额最大品类 |
| item_count | int | 商品行数 |
| order_year / order_month / quarter / weekday | int | 下单年月/季度/星期（便于按时间切片） |

## 6. order_items.csv —— 订单明细（3,087 行）

| 字段 | 类型 | 说明 |
|---|---|---|
| item_id | string | 明细编码 |
| order_id | string | 关联订单 |
| product_id | string | 关联产品 |
| product_name | string | 冗余产品名 |
| category | string | 品类 |
| quantity | int | 数量（樘：零售 1~3，装企 2~5，工程 8~18） |
| width_mm / height_mm / thickness_mm | int | 定制尺寸（mm） |
| is_custom | int | 是否非标定制（1=是，约 3 成） |
| unit_price | float | 成交单价（元/樘） |
| line_amount | float | 行金额 = unit_price × quantity |
| area_m2 | float | 面积（㎡，= 宽×高/1e6，可用于面积定价分析） |

**口径**：`unit_price = base_price × price_multiplier × (宽×高/标准面积) × 非标加成 × 随机波动`，四舍五入到 10 元。

## 7. payments.csv —— 付款记录（5,982 行）

| 字段 | 类型 | 说明 |
|---|---|---|
| payment_id | string | 付款编码 |
| order_id | string | 关联订单 |
| payment_date | date | 付款日期 |
| stage | string | 定金（30%）/ 中期款（35%，大单或非零售才有）/ 尾款 |
| amount | float | 金额 |
| payment_method | string | 微信支付 / 支付宝 / POS刷卡 / 银行转账 / 对公转账 |
| is_refund | int | 是否退款（已取消订单的定金退回） |

**口径**：定金在下单日支付；尾款在交付日支付；大额/工程单加一笔中期款。

## 8. after_sales.csv —— 售后/退换（189 行）

| 字段 | 类型 | 说明 |
|---|---|---|
| after_id | string | 售后编码 |
| order_id | string | 关联订单 |
| customer_id | string | 关联客户 |
| product_id | string | 涉及品类（退款/咨询可能为空） |
| report_date | date | 上报日期 |
| type | string | 安装问题 / 尺寸误差 / 色差 / 五金损坏 / 门体变形 / 物流破损 / 售后咨询 / 退款 |
| cost | float | 公司承担成本（元，退款为退款额） |
| status | string | 已处理 / 处理中 |
| handling_days | int | 处理时长（天） |

**口径**：售后率约 7%左右，铜门/铸铝类色差与变形略高，工程标准件尺寸误差略高。

---

## 附录：常用指标口径

- **GMV / 成交额**：`final_amount`，统计时剔除 `order_status='已取消'`
- **客单价**：`final_amount` 均值（按订单）
- **转化率**：有订单客户数 / `is_lead=0` 的注册客户数（按渠道拆）
- **复购率**：`total_orders>=2` 的客户占比（按客户）
- **准时交付率**：`is_on_time=1` 的已交付订单占比
- **售后率**：售后订单数 / 有效订单数
