"""数据接入层 ，， 刻意不建任何模型（ODS 表）。

口径与设计说明
--------------
1. **不落 ODS 表**：原始 CSV（``data/raw/*.csv``）与清洗宽表
   （``data/processed/*.csv``）是模拟数据的「事实来源」，文件本身就是最可靠的
   暂存层。若再建一套 ``ods_orders_raw`` 之类的中间表，只会带来三份同义数据
   （CSV / ODS / 业务表）互相漂移的风险，而本项目的数据量（订单 2401 行、
   明细 3087 行）也完全不需要落盘暂存来提升性能。

2. **导入路径**：``apps/datasource/loaders.py`` 提供按依赖顺序排列的装载函数
   （主数据 → 订单 → 明细/付款/售后），由 management command
   （``python manage.py import_dataset``）统一调用，直接写入
   masterdata / sales 的业务表。所有装载函数都是幂等的
   （以业务编码如 ``product_id`` → ``product_code``、``order_id`` → ``order_no``
   为自然键做 update_or_create），因此导入命令可以反复执行、可以断点重跑。

3. **补充模拟数据**：原始 CSV 里没有的派生数据（产品成本标准、订单成本明细、
   账期与账龄、交付阶段、员工与工时、销售目标、竞品对标）由
   ``apps/datasource/generators.py`` 用固定 seed（``GENERATOR_SEED``）生成，
   保证「换一台机器重跑得到完全一样的结果」。这类数据在真实企业里通常来自
   ERP / MES / 财务系统，本项目用生成器替代这些外部系统。

4. **本模块保持为空**：Django 需要一个 ``models.py`` 才能把应用纳入
   app registry（admin、migrations 都会 import 它），所以这里只放文档字符串。
   真正的字段定义全部在 ``apps.masterdata`` / ``apps.sales`` / ``apps.analytics``
   的 ``models.py`` 中，避免出现第二套同名业务表。
"""

# 本模块不含任何 Django 模型，请在 masterdata / sales 中定义业务表。
