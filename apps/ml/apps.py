"""数据挖掘应用：RFM 客户分群、流失预警、销量预测。

与 analytics 的分界线：analytics 输出「发生了什么」（描述性统计），
ml 输出「可能发生什么 / 应该关注谁」（预测与分群）。
模型训练结果落库后由 analytics 与 dashboard 消费，保证线上页面
不依赖在线推理，查询永远是可预期的毫秒级响应。
"""

from django.apps import AppConfig


class MlConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.ml"
    verbose_name = "数据挖掘"
