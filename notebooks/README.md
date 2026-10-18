# notebooks/

数据分析 notebook（已预执行，包含真实输出与图表）。

| 文件 | 内容 |
|---|---|
| `01_数据清洗.ipynb` | 读取 8 张原始表 → 缺失/异常核查 → 派生指标 → RFM 分群 → 输出 processed 宽表 |
| `02_销售与渠道分析.ipynb` | 增长趋势 / 季节性 / 品类帕累托 / 渠道效能 / 客户价值 / 交付售后 → 5 条业务建议 |

**运行方式**：在项目根目录用 Jupyter / PyCharm 打开即可（已内嵌输出，也可直接看）。
如需重新执行：`D:\python_study\pycharm_python\pytotal\.venv\Scripts\python.exe scripts/build_notebooks.py`（会覆盖生成并嵌入最新输出）。

**依赖**：pandas、numpy、matplotlib、nbformat、nbclient、ipykernel。
