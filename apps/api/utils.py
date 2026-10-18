"""API 层通用工具：统一响应包、数据版本 token、指标口径元信息、结果缓存装饰器。

为什么要有「统一响应包」
------------------------
前端（ECharts 图表、抽屉下钻表格）如果每个接口的返回结构都不一样，
每个图表都要写一套解析代码，而且**错误处理只能靠猜**：接口出错时
是返回空数组、返回 null，还是返回一个字符串？统一成
``{code, message, data, meta}`` 之后，前端只需要写一次拦截器：

- ``code == 0``：取 ``data``；
- ``code != 0``：展示 ``message``（中文，可直接给用户看），并把 ``meta`` 记进日志。

``code`` 用「0 表示成功、非 0 表示失败」，比 HTTP 状态码更适合做
业务级错误（例如「筛选条件里的门店不存在」既不是 400 也不是 404）。
HTTP 状态码仍然保留语义：只有真正的协议级错误才动它。
"""

from __future__ import annotations

import functools
import logging
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse

logger = logging.getLogger(__name__)

__all__ = [
    "SUCCESS_CODE",
    "ok",
    "error_payload",
    "jsonable",
    "data_version_token",
    "metric_meta",
    "cache_analytics",
    "resolve_filters",
]

# 业务码约定：0 成功；1xxxx 参数类；2xxxx 业务规则类；5xxxx 系统类。
SUCCESS_CODE = 0
CODE_BAD_REQUEST = 10001
CODE_NOT_FOUND = 20001
CODE_SERVER_ERROR = 50000


# ---------------------------------------------------------------------------
# 序列化适配
# ---------------------------------------------------------------------------
def jsonable(value: Any) -> Any:
    """把任意查询结果转成可 JSON 序列化的结构。

    selectors 的约定是「金额返回 ``Decimal``」，而 ``json.dumps`` 默认不认识
    ``Decimal``。转换点必须**唯一**，否则会出现「有的接口把 Decimal 转成字符串、
    有的转成 float」，前端拿到 12.30 的字符串去画图就静默变成 0。
    这里统一走 ``Decimal → float``、``date/datetime → ISO 字符串``。
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# ---------------------------------------------------------------------------
# 统一响应
# ---------------------------------------------------------------------------
def data_version_token() -> str:
    """当前数据版本字符串（放进 ``meta.data_version``）。

    为什么接口必须带数据版本
    ------------------------
    分析接口的返回值会被截图、粘进汇报、做缓存。当两个人拿着两份不同的
    「2025 年成交额」争论时，唯一能快速定位问题的手段就是对齐数据版本。
    版本 token 由 ``DataVersion`` 表里各数据集的版本与更新时间聚合而成，
    任何一次数据重跑都会改变它。

    聚合失败（表还没建、字段名不一致）时返回 ``"unknown"`` 而不是抛异常，，
    版本信息是**元数据**，不能因为它取不到就让整个接口失败。
    """
    try:
        from apps.analytics.models import DataVersion

        # 字段以 apps.analytics.models.DataVersion 为准：table_name / layer /
        # version_token / row_count / max_event_date / built_at。
        # 早期版本按 dataset+version 聚合，字段名对不上会让版本号永远取不到
        # （表现为 meta.data_version 恒为 unknown，缓存也就失去了失效依据）。
        rows = DataVersion.objects.all().order_by("layer", "table_name")
        parts = []
        for row in rows:
            dataset = row.table_name or "?"
            version = row.version_token or ""
            stamp = row.built_at.strftime("%Y%m%d%H%M") if row.built_at else ""
            parts.append("{}@{}#{}#{}".format(dataset, version, stamp, row.row_count or 0))
        if not parts:
            return "empty"
        raw = "|".join(parts)
        # 用短摘要而不是原始串：meta 要小，且版本串可能很长
        digest = str(abs(hash(raw)) % (10 ** 12))
        return "dv-{}".format(digest)
    except Exception as exc:  # pragma: no cover - 依赖 DataVersion 表
        logger.warning("数据版本聚合失败：%s", exc)
        return "unknown"


def metric_meta(codes) -> list:
    """按指标编码取口径定义（放进 ``meta.metric_definitions``）。

    接口返回的数字应当自带口径：「准时率 91.2%」在不同分母下是 91.2% 还是 84.3%，
    只有把 definition / formula / caveat 一起返回，调用方才能自查。
    同时返回的 ``metric_codes`` 与页面 KPI 卡上的小问号用的是同一份常量。
    """
    if not codes:
        return []
    try:
        from apps.analytics.metrics import METRIC_MAP

        items = []
        for code in codes:
            item = METRIC_MAP.get(code)
            if not item:
                continue
            items.append(
                {
                    "code": item.get("code"),
                    "name": item.get("name"),
                    "domain": item.get("domain"),
                    "metric_type": item.get("metric_type"),
                    "definition": item.get("definition"),
                    "formula": item.get("formula"),
                    "unit": item.get("unit"),
                    "caveat": item.get("caveat"),
                    "version": item.get("version"),
                }
            )
        return items
    except Exception as exc:  # pragma: no cover - 口径常量异常不该影响取数
        logger.warning("指标口径读取失败：%s", exc)
        return []


def _build_meta(request=None, metric_codes=None, warnings=None, extra=None) -> dict:
    """组装 ``meta``：数据版本 + 口径 + 筛选条件 + 提示。"""
    meta = {
        "data_version": data_version_token(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    definitions = metric_meta(metric_codes)
    if definitions:
        meta["metric_definitions"] = definitions
    if warnings:
        meta["warnings"] = list(warnings)
    filters = resolve_filters(request) if request is not None else None
    if filters is not None:
        meta["filters"] = filters.to_dict()
        meta["filter_description"] = filters.describe()
        meta["cache_key"] = filters.cache_key()
    if extra:
        meta.update(jsonable(extra))
    return meta


def ok(data=None, request=None, metric_codes=None, warnings=None, code=SUCCESS_CODE, message="成功", meta=None):
    """构造统一成功响应。

    返回值优先用 DRF 的 ``Response``（若 DRF 可用）：这样分页、内容协商、
    浏览器可读渲染（Browsable API）都能正常工作；DRF 不可用时退回
    ``JsonResponse``，保证纯 Django 环境也能跑。

    参数
    ----
    data : 任意可序列化对象
        业务数据。``Decimal`` 会自动转 ``float``（见 :func:`jsonable`）。
    request : HttpRequest | None
        提供时会把筛选条件与口径写进 ``meta``。
    metric_codes : list[str] | None
        本次返回涉及的指标编码，用于把口径说明一起返回。
    warnings : list[str] | None
        取数过程中的降级提示（例如「预聚合表未构建，已回退实时计算」）。
    """
    payload = {
        "code": code,
        "message": message,
        "data": jsonable(data),
        "meta": jsonable(meta) if meta is not None else _build_meta(request, metric_codes, warnings),
    }
    try:
        from rest_framework.response import Response

        return Response(payload)
    except Exception:  # pragma: no cover - 纯 Django 环境
        return JsonResponse(payload, json_dumps_params={"ensure_ascii": False})


def error_payload(exc, context=None, include_traceback=None) -> tuple:
    """把异常翻译成 ``(payload, http_status)``。

    设计要点：

    1. **message 一定是中文且可直接展示**。DRF 的默认错误信息是英文的
       （``"Invalid page."``），直接弹给业务用户看等于没提示。
    2. **开发环境带 traceback**。线上不带：堆栈会泄漏文件路径与库版本。
       是否开发环境看 ``settings.DEBUG``，不看 ``ENVIRONMENT``，，
       后者各项目命名不一，靠不住。
    3. **字段级校验错误进 ``data.errors``**，前端可以逐字段标红。
    """
    from rest_framework import exceptions as drf_exceptions
    from rest_framework import status as drf_status

    if include_traceback is None:
        include_traceback = bool(getattr(settings, "DEBUG", False))

    http_status = drf_status.HTTP_500_INTERNAL_SERVER_ERROR
    code = CODE_SERVER_ERROR
    message = "服务异常，请稍后重试或联系管理员"
    errors = None

    if isinstance(exc, drf_exceptions.ValidationError):
        http_status = drf_status.HTTP_400_BAD_REQUEST
        code = CODE_BAD_REQUEST
        message = "参数校验未通过"
        errors = jsonable(getattr(exc, "detail", None))
    elif isinstance(exc, drf_exceptions.NotFound):
        http_status = drf_status.HTTP_404_NOT_FOUND
        code = CODE_NOT_FOUND
        message = "请求的资源不存在"
    elif isinstance(exc, drf_exceptions.PermissionDenied):
        http_status = drf_status.HTTP_403_FORBIDDEN
        code = CODE_BAD_REQUEST
        message = "当前账号没有访问该数据的权限"
    elif isinstance(exc, drf_exceptions.NotAuthenticated):
        http_status = drf_status.HTTP_401_UNAUTHORIZED
        code = CODE_BAD_REQUEST
        message = "登录状态已失效，请重新登录"
    elif isinstance(exc, drf_exceptions.Throttled):
        http_status = drf_status.HTTP_429_TOO_MANY_REQUESTS
        code = CODE_BAD_REQUEST
        message = "请求过于频繁，请稍后重试"
    elif isinstance(exc, drf_exceptions.APIException):
        http_status = getattr(exc, "status_code", http_status)
        code = CODE_BAD_REQUEST if http_status < 500 else CODE_SERVER_ERROR
        message = str(getattr(exc, "detail", None)) or "请求处理失败"
    else:
        # 非 DRF 异常：交给 Django 语义的兜底文案，绝不把原始异常串暴露给用户
        message = "服务异常（{}），请稍后重试或联系管理员".format(type(exc).__name__)

    meta = {
        "data_version": data_version_token(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "error_type": type(exc).__name__,
    }
    view = (context or {}).get("view")
    if view is not None:
        meta["view"] = getattr(view, "__class__", type(view)).__name__
    if include_traceback:
        import traceback

        meta["traceback"] = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ).splitlines()[-25:]

    payload = {
        "code": code,
        "message": message,
        "data": {"errors": errors} if errors is not None else None,
        "meta": meta,
    }
    return payload, http_status


# ---------------------------------------------------------------------------
# 筛选条件解析
# ---------------------------------------------------------------------------
def resolve_filters(request):
    """从 ``request`` 解析分析筛选条件；失败时退回「全量」而不是报错。

    接口与页面共用 :class:`AnalyticsFilters`：这样「接口返回的 2025 年成交额」
    与「页面筛选 2025 年后看到的成交额」必然是一致的。
    """
    from apps.analytics.services.filters import AnalyticsFilters

    try:
        return AnalyticsFilters.from_request(request)
    except Exception as exc:  # pragma: no cover
        logger.warning("接口筛选条件解析失败，已退回全量：%s", exc)
        return AnalyticsFilters()


# ---------------------------------------------------------------------------
# 缓存装饰器
# ---------------------------------------------------------------------------
def cache_analytics(timeout=None, cache_key_prefix=None):
    """分析类 GET 接口的结果缓存装饰器（用在 DRF ``APIView.get`` 上）。

    缓存 key 设计：``{view_name}:{filters.cache_key()}:{data_version_token()}``

    - ``filters.cache_key()`` 已经把筛选条件规范化成稳定摘要
      （列表排序去重、空值省略），所以 ``channels=a,b`` 与 ``channels=b,a`` 命中同一份缓存；
    - ``data_version_token()`` 保证**数据一重跑，旧缓存全部自动失效**，
      不需要额外写「清理缓存」的管理命令。这是缓存能做对的前提：
      分析结果缓存最容易出的事故不是「没命中」，而是「命中了旧数据还看不出来」。

    超时默认取 ``settings.ANALYTICS_CACHE_TTL``（未配置时 300 秒）。
    被装饰的视图返回值应当是 ``ok(...)`` 的返回值，，即 DRF ``Response``，
    因此缓存里存的是 ``payload`` dict（而不是 Response 对象，Response 不可序列化）。
    """
    effective_timeout = timeout
    if effective_timeout is None:
        effective_timeout = int(getattr(settings, "ANALYTICS_CACHE_TTL", 300) or 300)
    prefix = cache_key_prefix or "api"

    def decorator(view_method):
        @functools.wraps(view_method)
        def wrapper(self, request, *args, **kwargs):
            try:
                filters = resolve_filters(request)
                key = "{}:{}:{}:{}".format(
                    prefix,
                    getattr(self, "__class__", type(self)).__name__,
                    filters.cache_key(),
                    data_version_token(),
                )
            except Exception as exc:  # pragma: no cover - 缓存失败必须降级为直算
                logger.warning("缓存 key 构造失败，本次跳过缓存：%s", exc)
                return view_method(self, request, *args, **kwargs)

            try:
                cached = cache.get(key)
            except Exception as exc:  # pragma: no cover
                logger.warning("缓存读取失败，本次跳过缓存：%s", exc)
                cached = None

            if cached is not None:
                try:
                    from rest_framework.response import Response

                    payload = dict(cached)
                    meta = dict(payload.get("meta") or {})
                    meta["cache_hit"] = True
                    meta["cache_key"] = key
                    payload["meta"] = meta
                    return Response(payload)
                except Exception:  # pragma: no cover
                    return JsonResponse(cached, json_dumps_params={"ensure_ascii": False})

            response = view_method(self, request, *args, **kwargs)
            try:
                payload = getattr(response, "data", None)
                if isinstance(payload, dict) and payload.get("code") == SUCCESS_CODE:
                    meta = dict(payload.get("meta") or {})
                    meta["cache_hit"] = False
                    meta["cache_key"] = key
                    payload = dict(payload, meta=meta)
                    setattr(response, "data", payload)
                    cache.set(key, jsonable(payload), effective_timeout)
            except Exception as exc:  # pragma: no cover - 写缓存失败不影响返回
                logger.warning("写入缓存失败（不影响本次返回）：%s", exc)
            return response

        return wrapper

    return decorator


# 内部使用：把「视图名」注入 meta，便于排查是哪个接口产出的数字
def view_meta(view_name: str, **extra) -> dict:
    """构造附加 meta（视图名 + 自定义键值），供 :func:`ok` 的 ``meta`` 参数使用。"""
    payload = {"view": view_name}
    payload.update({key: value for key, value in extra.items() if value is not None})
    return payload
