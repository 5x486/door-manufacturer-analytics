# -*- coding: utf-8 -*-
"""
页面冒烟测试
============
断言「每个页面都能渲染出 200」，而不是断言页面里的数字。
理由：视图里的取数链路很长（selectors → 预聚合表 → 多表关联），
任何一环的字段名写错都会让整页 500，而这类错误在开发期很难靠肉眼看出来。
"""
import re
from pathlib import Path

import pytest
from django.conf import settings

PAGES = [
    ("/", "经营总览"),
    ("/growth/", "增长与渠道"),
    ("/profit/", "品类与利润"),
    ("/customers/", "客户价值"),
    ("/retention/", "留存与预警"),
    ("/delivery/", "交付与售后"),
    ("/cashflow/", "回款与现金流"),
    ("/workforce/", "人效与门店"),
    ("/simulator/", "决策模拟"),
    ("/dq/", "数据质量"),
    ("/model/", "模型在线"),
    ("/metrics/", "指标字典"),
    ("/orders/", "明细数据"),
]

# 模板注释（{# ... #} 与 {% comment %}...{% endcomment %}）是开发者笔记，
# 不会渲染到页面上，检查前先剔除，避免对注释里的示例符号误报。
_COMMENT_PATTERNS = (
    re.compile(r"\{#.*?#\}", re.S),
    re.compile(r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", re.S),
)

# emoji 主要落在几个 Unicode 区段：杂项符号与绘文字、装饰符号、麻将与扑克牌。
_EMOJI_PATTERN = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F0FF]"
)

# 破折号：U+2014 EM DASH 与 U+2013 EN DASH。文案里用句号或冒号代替。
_EM_DASHES = ("\u2014", "\u2013")


def _template_texts():
    """逐个读入模板，剔除注释后产出 (文件名, 正文)。"""
    tpl_dir = Path(settings.BASE_DIR) / "templates"
    for path in sorted(tpl_dir.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for pattern in _COMMENT_PATTERNS:
            text = pattern.sub("", text)
        yield path.name, text


@pytest.mark.django_db
class TestPagesRender:
    @pytest.mark.parametrize("url,name", PAGES)
    def test_page_returns_200(self, authenticated_client, url, name):
        resp = authenticated_client.get(url)
        assert resp.status_code == 200, f"{name}（{url}）返回 {resp.status_code}"
        # 页面必须有实质内容，不能是一个空白骨架
        assert len(resp.content) > 500

    def test_anonymous_redirected_to_login(self, client, db):
        """未登录访问任何页面都应跳登录页。"""
        for url, _ in PAGES:
            resp = client.get(url)
            assert resp.status_code == 302, f"{url} 未登录时返回 {resp.status_code}"
            assert "/accounts/login/" in resp["Location"]

    def test_customer_360_renders(self, authenticated_client, customer):
        resp = authenticated_client.get(f"/customers/{customer.customer_code}/")
        assert resp.status_code == 200

    def test_unknown_customer_does_not_crash(self, authenticated_client):
        """不存在的客户编码必须优雅处理，不能 500。"""
        resp = authenticated_client.get("/customers/C00000/")
        assert resp.status_code in (200, 404)


@pytest.mark.django_db
class TestNoEmojiNoEmDash:
    """品味与可访问性回归测试。

    这两条容易被后续改动破坏（有人顺手贴一个 emoji 图标、或者用破折号写文案），
    而它们正是「看起来专业」的关键细节，所以用测试守住。
    """

    def test_no_emoji_in_templates(self):
        offenders = [name for name, text in _template_texts() if _EMOJI_PATTERN.search(text)]
        assert not offenders, f"模板里出现了 emoji 图标：{offenders}"

    def test_no_em_dash_in_templates(self):
        offenders = [
            name for name, text in _template_texts() if any(d in text for d in _EM_DASHES)
        ]
        assert not offenders, f"模板里出现了破折号（改为句号或冒号）：{offenders}"


def test_no_multiline_inline_comments():
    """模板里不允许出现跨行的 {# #} 注释。

    Django 的 {# #} 只在**单行**内有效；一旦注释跨行，它不会被解析，
    而是被当作普通文本原样输出到页面上，表现为「页面顶部多出一段乱码」。
    这个坑在本项目里踩过两次（注释里含 #} 会提前闭合、注释跨行不被解析），
    所以用测试守住，避免以后有人好心加注释反而把页面搞坏。
    """
    offenders = []
    for name, text in _template_texts():
        # _template_texts 会剔掉合格注释，所以这里直接扫原始文件
        pass
    from pathlib import Path

    from django.conf import settings as _s

    for path in Path(_s.BASE_DIR).joinpath("templates").rglob("*.html"):
        raw = path.read_text(encoding="utf-8")
        for m in re.finditer(r"\{#(.*?)#\}", raw, re.S):
            if "\n" in m.group(1):
                offenders.append(path.name)
                break
    assert not offenders, f"这些模板里有跨行 {{# #}} 注释（Django 不解析，会变成页面文字）：{offenders}"
