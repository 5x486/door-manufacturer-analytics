"""数据接入应用：把外部 CSV 明细灌入业务表。

为什么需要这样一个「只有 loader、没有表」的应用：
1. 导入逻辑（列名映射、NaN 清洗、Decimal 转换、主外键解析）属于数据工程，
   与 masterdata/sales 的业务规则是两件事，混在 models 或 service 里会让
   两边都难以单独测试；
2. 数据集会换版（新增列、调整枚举），改动集中在本应用内即可，
   业务表结构不动；
3. 导入动作用 management command 触发、可重复执行（幂等），
   因此本应用天然是「没有界面、只有入口」的批处理角色。
"""

from django.apps import AppConfig


class DatasourceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.datasource"
    verbose_name = "数据接入"
