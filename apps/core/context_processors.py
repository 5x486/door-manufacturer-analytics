"""全局模板上下文。

为什么用 context processor 而不是让每个视图自己传
------------------------------------------------
1. **项目名、数据截止日、免责声明**出现在几乎每个页面上。如果让 20 个视图
   各自准备这些变量，一定会有人漏掉，而漏掉「数据截止日」的后果特别严重，，
   用户会把模拟数据当成实时经营数据来做决策。
2. **导航菜单**依赖 URL 反向解析。放在这里统一维护，
   新增一个分析模块只需要改这一处，不会出现「菜单里有、路由里没有」的 500。
3. ``current_module``（当前模块高亮）必须由路由推断而不是由视图声明：
   一个分析页可能被多个入口 URL 命中（例如 ``/analytics/category/`` 与
   ``/analytics/category/detail/``），靠路由推断才不会漏高亮。

设计约束
--------
- **任何情况下不得抛异常**。context processor 抛异常会让整个页面 500，
  而这里装的都是装饰性内容。因此所有 URL 解析都走
  :func:`_safe_reverse`：解析失败就退回 ``"#"``，页面照常渲染。
- 导航列表在进程内缓存，但**反向解析结果不缓存**：路由表可能在开发期变化
  （新增模块、重命名路由），缓存 URL 会造成「改了路由菜单却还是旧链接」。
"""

from __future__ import annotations

import logging
from datetime import date

from django.conf import settings
from django.urls import NoReverseMatch, reverse

logger = logging.getLogger(__name__)

__all__ = [
    "COMPANY_PROFILE",
    "DATA_DISCLAIMER",
    "NAV_MODULES",
    "global_context",
    "get_nav_modules",
]

# 公司概况：模板页脚与「关于本系统」使用。
# 数字是模拟经营的设定值，不是真实工商信息——页面上会与 DATA_DISCLAIMER 一起展示。
COMPANY_PROFILE = {
    "name": "浙江杭博门业有限公司",
    "short_name": "杭博门业",
    "brand": "杭博",
    "province": "浙江省",
    "city": "嘉兴市桐乡市",
    "founded_year": 2023,
    "business": "定制铜门、不锈钢门、铸铝门、甲级防盗门、防火门的研发、生产与安装",
    "scale": "3 家门店 · 6 名业务员 · 42 名员工",
    "category_count": 6,
    "positioning": "深耕桐乡及周边县市的定制门零售与工程配套",
}

# 数据免责声明：必须在每个页面可见。
# 理由：这批数据全部是按固定 seed 生成的模拟数据，一旦被误当成真实经营数据
# 引用（写进汇报、拿去做决策），后果比算错一个指标严重得多。
DATA_DISCLAIMER = (
    "本系统展示的全部数据均为按固定随机种子生成的模拟数据，"
    "仅用于演示经营分析方法与系统功能，不代表任何真实企业的经营情况，"
    "不可用于投资、经营决策或对外披露。"
)

# 导航模块：名称 + URL 名 + 图标。
# url_name 采用 ``命名空间:路由名`` 形式，与 config/urls.py 的 include(namespace=...) 对应。
NAV_MODULES = [
    {
        "key": "help",
        "name": "使用说明",
        "url_name": "dashboard:help",
        "description": "每个栏目怎么用、先看什么、常见疑问",
        "group": "上手",
    },
    {
        "key": "overview",
        "name": "经营总览",
        "url_name": "dashboard:overview",
        "description": "成交额、毛利、目标达成与同比环比的第一屏",
        "group": "经营",
    },
    {
        "key": "growth",
        "name": "增长渠道",
        "url_name": "dashboard:growth",
        "description": "渠道成交结构、获客成本与渠道 ROI",
        "group": "经营",
    },
    {
        "key": "profit",
        "name": "品类利润",
        "url_name": "dashboard:profit",
        "description": "六大品类的收入、成本结构与毛利对比",
        "group": "经营",
    },
    {
        "key": "customers",
        "name": "客户价值",
        "url_name": "dashboard:customers",
        "description": "RFM 分群、客户贡献集中度与客户 360",
        "group": "客户",
    },
    {
        "key": "retention",
        "name": "留存预警",
        "url_name": "dashboard:retention",
        "description": "队列留存曲线与流失预警名单",
        "group": "客户",
    },
    {
        "key": "delivery",
        "name": "交付售后",
        "url_name": "dashboard:delivery",
        "description": "准时率、交付阶段瓶颈与售后成本",
        "group": "运营",
    },
    {
        "key": "cashflow",
        "name": "回款现金",
        "url_name": "dashboard:cashflow",
        "description": "应收账龄、DSO 与未来 13 周回款预测",
        "group": "运营",
    },
    {
        "key": "workforce",
        "name": "人效门店",
        "url_name": "dashboard:workforce",
        "description": "人均产值、人力成本占比与门店对比",
        "group": "运营",
    },
    {
        "key": "simulator",
        "name": "决策模拟",
        "url_name": "dashboard:simulator",
        "description": "调折扣、调品类结构，看毛利会怎么变",
        "group": "运营",
    },
    {
        "key": "dq",
        "name": "数据质量",
        "url_name": "dashboard:data_quality",
        "description": "25 条校验规则的通过情况与异常样本",
        "group": "运营",
    },
    {
        "key": "model",
        "name": "模型在线",
        "url_name": "dashboard:model_online",
        "description": "流失预警与销量预测模型的版本与评估指标",
        "group": "运营",
    },
    {
        "key": "metrics",
        "name": "指标字典",
        "url_name": "dashboard:metric_dictionary",
        "description": "56 条指标的口径、公式与易误读提醒",
        "group": "运营",
    },
    {
        "key": "orders",
        "name": "数据明细",
        "url_name": "dashboard:order_list",
        "description": "订单、明细、付款、售后的原始明细与导出",
        "group": "运营",
    },
]

# key → 模块，供视图按 key 取当前模块信息
NAV_MODULE_MAP = {module["key"]: module for module in NAV_MODULES}


def _safe_reverse(url_name: str, fallback: str = "#") -> str:
    """安全反向解析 URL。

    解析失败返回 ``fallback`` 而不是抛异常：项目是分阶段搭建的，
    「菜单项已加、对应路由还没写」是常态，不能因此让所有页面 500。
    同时用 ``logger.debug`` 留痕，便于开发期排查真实的路由名写错。
    """
    try:
        return reverse(url_name)
    except NoReverseMatch:
        logger.debug("导航模块路由未注册，暂用占位链接：%s", url_name)
        return fallback
    except Exception as exc:  # pragma: no cover - 兜底，绝不让页面挂掉
        logger.warning("反向解析 %s 失败：%s", url_name, exc)
        return fallback


def get_nav_modules(resolve_urls: bool = True) -> list:
    """返回导航模块列表（在 :data:`NAV_MODULES` 基础上补 ``url`` 字段）。

    每次调用都重新解析 URL，但列表本身是浅拷贝，，
    调用方拿到的是新对象，可以安全地在模板里改（例如加 ``active`` 标记）。
    """
    modules = []
    for module in NAV_MODULES:
        item = dict(module)
        item["url"] = _safe_reverse(module["url_name"]) if resolve_urls else "#"
        item["route_ready"] = item["url"] != "#"
        modules.append(item)
    return modules


def _resolve_current_module(request, modules: list) -> dict:
    """按 ``request.resolver_match`` 推断当前所在模块。

    推断顺序（从精确到宽松）：
    1. ``namespace:url_name`` 与模块的 ``url_name`` 完全一致 → 命中；
    2. namespace 一致且模块 key 出现在 url_name 里（例如
       ``analytics:customer_detail`` 命中 ``analytics:customer``）；
    3. 首页（``/``）视为「经营总览」。

    全部未命中时返回一个「未归属」占位 dict，
    模板里用 ``current_module.key`` 判空即可，不必到处写 ``if current_module``。
    """
    resolver_match = getattr(request, "resolver_match", None)
    fallback = {"key": "", "name": "", "icon": "", "url_name": "", "url": "#", "is_fallback": True}
    if resolver_match is None:
        # 首页由 dashboard 承接；没有 resolver_match 时（例如中间件早期阶段）
        # 直接返回占位，不必猜。
        return fallback

    namespace = getattr(resolver_match, "namespace", "") or ""
    url_name = getattr(resolver_match, "url_name", "") or ""
    full_name = "{}:{}".format(namespace, url_name) if namespace else url_name

    for module in modules:
        if module["url_name"] == full_name:
            item = dict(module)
            item["is_fallback"] = False
            return item

    if namespace:
        for module in modules:
            module_namespace = module["url_name"].split(":")[0]
            if module_namespace != namespace:
                continue
            # 模块 key 是 url_name 的前缀约定（analytics:growth_detail → growth）
            module_suffix = module["url_name"].split(":")[-1]
            if module_suffix and module_suffix in url_name:
                item = dict(module)
                item["is_fallback"] = False
                return item

    return fallback


def _static_version() -> str:
    """静态资源版本号：取前端三个关键文件的修改时间戳。

    为什么需要它：浏览器会把 design-system.css / site.js 缓存住，
    每次改完样式都要用户手动 Ctrl+F5，实际使用中没人会记得这么做，
    结果就是「代码改了但界面没变」，还容易被误判成代码没生效。
    把 mtime 作为查询参数拼在 URL 上，文件一变 URL 就变，浏览器自然重新拉取。
    """
    from pathlib import Path

    stamps = []
    # 动效层（motion-layer.css / motion.js）也必须计入版本号：
    # 它们是「不在原有交付件里」的新文件，如果只盯设计系统与 site.js，
    # 改完动效后浏览器仍会拿旧缓存，表现就是「改了但没变化」。
    for rel in (
        "static/css/design-system.css",
        "static/css/slider.css",
        "static/css/motion-layer.css",
        "static/js/site.js",
        "static/js/motion.js",
    ):
        path = Path(settings.BASE_DIR) / rel
        try:
            stamps.append(str(int(path.stat().st_mtime)))
        except OSError:
            stamps.append("0")
    return "-".join(stamps)


def global_context(request) -> dict:
    """注入全局模板上下文。

    返回的键在模板里直接可用：

    - ``project_name``：项目名（来自 ``settings.PROJECT_NAME``，缺失时退回默认值）；
    - ``company_profile``：公司概况 dict；
    - ``data_as_of``：数据截止日（``date`` 对象 + ISO 字符串两种形式都提供，
      避免模板里到处写 ``|date:"Y-m-d"`` 时踩时区格式的坑）；
    - ``data_disclaimer``：模拟数据免责声明；
    - ``nav_modules`` / ``nav_groups``：导航模块与按分组聚合的导航；
    - ``current_module``：当前模块（用于高亮与页面标题）。
    """
    project_name = getattr(settings, "PROJECT_NAME", None) or "杭博门业经营分析系统"

    # 数据截止日：优先 settings.DATA_AS_OF_DATE，其次解析 DATA_AS_OF 字符串，
    # 最后退回项目约定值。三种来源都要覆盖，因为不同环境下只配了其中一种。
    as_of = getattr(settings, "DATA_AS_OF_DATE", None)
    if not isinstance(as_of, date):
        raw = getattr(settings, "DATA_AS_OF", None)
        as_of = None
        if isinstance(raw, str) and raw.strip():
            try:
                as_of = date.fromisoformat(raw.strip())
            except ValueError:
                as_of = None
        if as_of is None:
            as_of = date(2026, 8, 9)

    modules = get_nav_modules(resolve_urls=True)
    current_module = _resolve_current_module(request, modules)

    # 按 group 聚合，模板里可以直接渲染「核心 / 客户与留存 / 履约与现金 / 效率与治理」四组
    groups: dict = {}
    for module in modules:
        groups.setdefault(module["group"], []).append(module)
    nav_groups = [{"group": group, "modules": items} for group, items in groups.items()]

    return {
        "static_version": _static_version(),
        "project_name": project_name,
        "company_profile": COMPANY_PROFILE,
        "company_name": COMPANY_PROFILE["name"],
        "data_as_of": as_of,
        "data_as_of_iso": as_of.isoformat(),
        "data_disclaimer": DATA_DISCLAIMER,
        "nav_modules": modules,
        "nav_groups": nav_groups,
        "current_module": current_module,
        "is_readonly_demo": True,
    }
