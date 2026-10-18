# -*- coding: utf-8 -*-
"""项目中间件。

DisableStaticCacheMiddleware
============================
开发环境下给静态资源响应补上 ``Cache-Control: no-cache``。

为什么需要它：Django 开发服务器与 WhiteNoise 默认都不发 Cache-Control 头，
浏览器于是按启发式缓存规则（依据 Last-Modified 推算一个过期时间）把 CSS/JS
长期缓存住。结果是「改了样式、刷新了页面，界面还是旧的」，而且很难归因到缓存，
通常会被误判成「代码没生效」。no-cache 的含义是「可以缓存，但每次使用前必须
向服务器校验」，配合 ETag/Last-Modified 既不会拖慢加载，也不会看到过期内容。

生产环境不启用这个中间件：那里静态文件由 nginx 直出，并使用带内容指纹的文件名，
本来就该长缓存。
"""
from __future__ import annotations

from django.conf import settings


class DisableStaticCacheMiddleware:
    """仅对 DEBUG 模式下的 /static/ 与 /media/ 响应禁用强缓存。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not getattr(settings, "DEBUG", False):
            return response
        path = request.path or ""
        static_url = getattr(settings, "STATIC_URL", "/static/") or "/static/"
        media_url = getattr(settings, "MEDIA_URL", "/media/") or "/media/"
        if path.startswith(static_url) or path.startswith(media_url):
            response["Cache-Control"] = "no-cache, must-revalidate, max-age=0"
            response["Pragma"] = "no-cache"
            response["Expires"] = "0"
        return response
