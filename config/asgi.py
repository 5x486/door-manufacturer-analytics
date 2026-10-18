"""ASGI 入口：为将来的异步视图、WebSocket 推送（如看板数据实时刷新）预留。

当前业务以同步 ORM 查询为主，部署仍走 WSGI；此处保持标准实现，
以便后续用 uvicorn / daphne 直接接管而无需改动项目结构。
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

application = get_asgi_application()
