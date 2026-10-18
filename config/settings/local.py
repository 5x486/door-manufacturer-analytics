"""本地开发配置。

原则：**数据库连接只在一处定义**（base.py 从 .env 读 DB_* 变量），
本文件只覆盖「开发期需要不一样」的部分。
之所以这么改：此前 base.py 读 DB_PORT、local.py 读 MYSQL_PORT，
两个变量名并存，.env 只填了一个 → 开发环境静默连到 3306 空端口，
报错却是 (2002, Can't connect ... 10061)，排查成本极高。
配置文件里绝不允许多套变量名指向同一个含义。
"""

from .base import *  # noqa: F401,F403
from .base import BASE_DIR, env, env_bool, env_list  # noqa: F401

# ---------------------------------------------------------------------------
# 调试
# ---------------------------------------------------------------------------
DEBUG = env_bool("DJANGO_DEBUG", True)

# 开发环境默认放行本机；需要局域网/手机联调时在 .env 里追加 IP 即可
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost,0.0.0.0,[::1]")
INTERNAL_IPS = env_list("DJANGO_INTERNAL_IPS", "127.0.0.1,::1")
CSRF_TRUSTED_ORIGINS = env_list(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    "http://127.0.0.1:8000,http://localhost:8000",
)

# ---------------------------------------------------------------------------
# 数据库：沿用 base 的连接参数（单一来源），只调开发期特有的项
# ---------------------------------------------------------------------------
DATABASES["default"]["CONN_MAX_AGE"] = int(env("DB_CONN_MAX_AGE", "0"))
DATABASES["default"]["TEST"] = {          # noqa: F405
    "CHARSET": "utf8mb4",
    "COLLATION": "utf8mb4_unicode_ci",
}

# 开发期需要时打开 SQL 打印（默认关闭，避免刷屏）
if env_bool("DJANGO_SHOW_SQL", False):
    LOGGING["loggers"]["django.db.backends"] = {  # noqa: F405
        "handlers": ["console"],
        "level": "DEBUG",
        "propagate": False,
    }

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
LOGGING["root"]["level"] = env("DJANGO_LOG_LEVEL", "INFO")  # noqa: F405
LOGGING["loggers"]["django"]["level"] = "INFO"  # noqa: F405
LOGGING["loggers"]["apps"] = {  # noqa: F405
    "handlers": ["console"],
    "level": "DEBUG",
    "propagate": False,
}

# ---------------------------------------------------------------------------
# 开发期安全项：本地没有 HTTPS，强制跳转会导致全是 301
# ---------------------------------------------------------------------------
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

# 邮件走控制台，不发真实邮件
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# 开发期静态文件不压缩（改 CSS 后不用 collectstatic）
STORAGES["staticfiles"] = {  # noqa: F405
    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
}

PROJECT_ROOT = BASE_DIR


# ---------------------------------------------------------------------------
# 静态文件缓存：开发环境必须关掉
# ---------------------------------------------------------------------------
# 背景（真实踩坑）：开发时改了 CSS，浏览器却仍然用旧样式，表现是「代码改了界面没变」。
# 根因是静态响应没有任何 Cache-Control 头，浏览器按启发式规则（依 Last-Modified）
# 自行决定缓存很久；再加上 login 页曾漏带版本号参数，就变成了看不见的旧样式。
# 这里显式声明 no-cache（允许缓存但要每次校验），并让 WhiteNoise 不做长缓存。
WHITENOISE_MAX_AGE = 0

# 给所有静态响应补上 Cache-Control，覆盖浏览器的启发式缓存行为。
MIDDLEWARE.append("apps.core.middleware.DisableStaticCacheMiddleware")  # noqa: F405
