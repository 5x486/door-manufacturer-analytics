# 仪表盘使用说明

## 一、Streamlit 交互看板（推荐，开箱即用）

位置：`dashboard/app.py`。基于清洗后的 `data/processed` 宽表，提供侧边栏筛选 + 5 大分析视图 + 动态结论。

```bash
cd D:\python_study\pycharm_python\door_analysis_project
D:\python_study\pycharm_python\pytotal\.venv\Scripts\python.exe -m streamlit run dashboard/app.py
```

浏览器会自动打开 `http://localhost:8501`。

**功能**：
- 侧边栏按 年份 / 来源渠道 / 客户类型 / 品类 筛选，全看板联动
- 顶部 KPI：成交额、有效订单、客单价、成交客户、准时交付率
- 5 个 Tab：增长趋势 / 品类帕累托 / 渠道效能 / 客户价值(RFM) / 交付与售后
- 底部核心结论随筛选条件动态更新

**首次运行**：如提示依赖缺失，执行
`pip install streamlit plotly pandas numpy`

---

## 二、用 Tableau 复刻（如需投递外企/国企）

1. 打开 Tableau Desktop → 连接 → 文本文件，导入 `data/processed/orders_clean.csv`、`item_flat.csv`、`customer_rfm.csv`
2. 建议工作表布局（复用本项目的 5 条结论）：

| 工作表 | 行 | 列 | 标记 |
|---|---|---|---|
| 月度GMV | 订单日期(连续月) | 求和(成交额) | 折线 |
| 年度对比 | 订单年份 | 求和(成交额) | 柱状 |
| 品类帕累托 | 品类 | 求和(行金额) | 柱+参考线(80%) |
| 渠道GMV | 来源渠道 | 求和(成交额) | 横向条形 |
| RFM分群 | 客户分群 | 客户数 | 饼图/条形 |
| 交付周期 | 主力品类 | 中位数(实际交付周期) | 条形 |
| 准时率 | — | 平均值(准时标记) | 大数字 |

3. 仪表板组装以上工作表 + 筛选器（年份/渠道），配色统一即可。

## 三、用 Power BI 复刻

1. 获取数据 → 文本/CSV → 导入 `data/processed/` 三张表
2. 模型关系：`orders_clean` ↔ `item_flat` 按 `order_id`；`customer_rfm` 按 `customer_id`
3. 新建度量值：
   - `成交额 = SUM(orders_clean[成交额])`
   - `客单价 = [成交额] / COUNTROWS(orders_clean)`
   - `准时率 = DIVIDE(CALCULATE(COUNTROWS(orders_clean), orders_clean[准时交付]=1), COUNTROWS(orders_clean))`
4. 用切片器(年份/渠道)+矩阵/图表复刻上面 7 张表，导出到 `outputs/dashboard/`。

> 注意：导入时若中文列名出现乱码，请在导入对话框选择 **编码 = UTF-8**。
