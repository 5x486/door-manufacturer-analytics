"""DRF 统一异常处理器。

为什么必须自己写一个
--------------------
DRF 默认返回 ``{"detail": "Invalid page."}`` 这种结构，它有三个问题：

1. **结构不统一**：校验错误返回 ``{"字段名": ["错误"]}``，权限错误返回
   ``{"detail": "..."}``，前端要为每种情况写一个分支；
2. **语言不合适**：默认文案是英文，直接弹给业务用户等于没有提示；
3. **丢失上下文**：出错的接口、数据版本、请求参数都不在响应里，
   线上排查只能靠翻日志。

本处理器把上述全部收进统一结构：

.. code-block:: json

    {
      "code": 50000,
      "message": "服务异常（DatabaseError），请稍后重试或联系管理员",
      "data": null,
      "meta": {"data_version": "dv-…", "generated_at": "…", "error_type": "DatabaseError"}
    }

开发环境（``settings.DEBUG``）额外在 ``meta.traceback`` 里放最后 25 行堆栈，，
分析系统出问题时，最常见的根因是「预聚合表还没构建」，
把堆栈直接给到开发者能省掉一轮「你截个图给我」的沟通。

注册方式（settings.py）：

.. code-block:: python

    REST_FRAMEWORK = {
        "EXCEPTION_HANDLER": "apps.api.exceptions.unified_exception_handler",
    }
"""

from __future__ import annotations

import logging

from rest_framework.views import exception_handler as drf_exception_handler

from apps.api.utils import error_payload

logger = logging.getLogger(__name__)

__all__ = ["unified_exception_handler"]


def unified_exception_handler(exc, context):
    """把 DRF 异常统一成 ``{code, message, data, meta}``。

    实现上先调用 DRF 自带的 :func:`drf_exception_handler`：
    它负责把 Django 异常翻译成 DRF 异常、并设置 ``WWW-Authenticate``
    之类的响应头。我们只在它的结果上「换一层壳」，
    而不是自己重新实现一遍异常映射，，那样会漏掉框架层面的细节。

    ``context`` 里通常带 ``view`` / ``request`` / ``args`` / ``kwargs``，
    这里只用 ``view`` 做定位；``request`` 全文不进响应（可能含敏感查询参数）。
    """
    response = drf_exception_handler(exc, context)
    view = (context or {}).get("view")
    view_name = getattr(view, "__class__", type(view)).__name__ if view else "-"

    # 只有 5xx 记 error，4xx 记 warning：客户端拿错参数不该淹没真正的服务端异常
    status_code = getattr(response, "status_code", 500) if response is not None else 500
    if status_code >= 500:
        logger.exception("接口异常 [%s]：%s", view_name, exc)
    else:
        logger.warning("接口请求被拒绝 [%s]：%s: %s", view_name, type(exc).__name__, exc)

    payload, http_status = error_payload(exc, context)

    if response is not None:
        # DRF 已识别该异常：沿用它的 HTTP 状态码（语义正确），只替换响应体
        payload["meta"]["view"] = view_name
        response.data = payload
        response.status_code = response.status_code or http_status
        return response

    # DRF 未识别的异常（例如 DatabaseError）：交给 Django 的 500 处理，
    # 但**先返回一个结构化的响应体**，避免前端收到 HTML 错误页而解析失败。
    try:
        from rest_framework.response import Response

        payload["meta"]["view"] = view_name
        return Response(payload, status=http_status)
    except Exception:  # pragma: no cover - 极端兜底
        return None
