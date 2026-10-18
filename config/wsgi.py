"""WSGI 入口：供 gunicorn / uWSGI 等同步部署方式加载。

生产用 gunicorn（见 Dockerfile 与 deploy/docker-compose.yml）：
    gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3
"""

import os

from django.core.wsgi import get_wsgi_application

# 默认本地配置，容器/服务器通过环境变量覆盖为 config.settings.production
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

application = get_wsgi_application()
