"""API 应用（v1）。

本包只承载「对外接口」这一层关注点：

- ``utils``：统一响应包、数据版本 token、指标口径元信息、分析结果缓存装饰器；
- ``exceptions``：DRF 异常 → 统一错误响应包；
- ``pagination``：分页响应包（与 ``utils.ok`` 同构）；
- ``serializers``：明细资源的序列化（中文 label）；
- ``views`` / ``urls``：接口实现与路由。

取值一律走 :mod:`apps.analytics.selectors`，与页面视图共用同一批函数，，
这是「页面与接口数字必然一致」的唯一保证。
"""
