"""DRF 分页。

分页响应结构与 :func:`apps.api.utils.ok` 同构：

.. code-block:: json

    {
      "code": 0,
      "message": "成功",
      "data": {"items": [...], "page": 1, "page_size": 50, "total": 1287, "total_pages": 26},
      "meta": {...}
    }

为什么把 ``items`` 放进 ``data`` 而不是把列表直接作为 ``data``：
明细类接口未来一定会加「合计行」「小计分组」这类附加信息，
如果 ``data`` 本身是数组，加字段的那天就要做破坏性改动。

``page_size`` 允许前端通过 ``page_size`` 参数调整，但**上下限写死**：
下限 1（避免 0 导致除零）、上限 :data:`MAX_PAGE_SIZE`（避免有人
``?page_size=100000`` 把数据库拖死，，这在实际项目里发生过很多次）。
"""

from __future__ import annotations

from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from apps.api.utils import SUCCESS_CODE, jsonable

__all__ = ["StandardPagination", "MAX_PAGE_SIZE"]

MAX_PAGE_SIZE = 500


class StandardPagination(PageNumberPagination):
    """标准分页器：``?page=2&page_size=50``。"""

    page_size = 50
    page_size_query_param = "page_size"
    page_query_param = "page"
    max_page_size = MAX_PAGE_SIZE

    def get_paginated_response(self, data):
        """返回统一响应包（结构与 ``ok()`` 保持一致，前端只写一套解析）。"""
        total = self.page.paginator.count
        page_size = self.get_page_size(self.request) or self.page_size
        total_pages = self.page.paginator.num_pages
        return Response(
            {
                "code": SUCCESS_CODE,
                "message": "成功",
                "data": {
                    "items": jsonable(data),
                    "page": self.page.number,
                    "page_size": page_size,
                    "total": total,
                    "total_pages": total_pages,
                    "has_next": self.page.has_next(),
                    "has_previous": self.page.has_previous(),
                },
                "meta": {
                    "filters": _request_params(self.request),
                    "page_size_limit": {"min": 1, "max": MAX_PAGE_SIZE},
                },
            }
        )

    def get_paginated_response_schema(self, schema):
        """drf-spectacular 用的响应结构说明（与上面实际返回保持一致）。"""
        return {
            "type": "object",
            "properties": {
                "code": {"type": "integer", "example": 0},
                "message": {"type": "string", "example": "成功"},
                "data": {
                    "type": "object",
                    "properties": {
                        "items": schema,
                        "page": {"type": "integer", "example": 1},
                        "page_size": {"type": "integer", "example": 50},
                        "total": {"type": "integer", "example": 1287},
                        "total_pages": {"type": "integer", "example": 26},
                        "has_next": {"type": "boolean"},
                        "has_previous": {"type": "boolean"},
                    },
                },
                "meta": {"type": "object"},
            },
        }


def _request_params(request) -> dict:
    """把 GET 参数原样回显到 meta，便于排查「这个数字是哪次请求算出来的」。"""
    get = getattr(request, "GET", None)
    if get is None:
        return {}
    try:
        return {key: get.get(key) for key in get.keys()}
    except Exception:  # pragma: no cover
        return {}
