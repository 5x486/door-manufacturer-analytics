"""生产环境配置：DEBUG 彻底关闭，安全头全开，日志落盘。

生产与开发的差别不只是 DEBUG 一个开关，还包含 cookie 安全属性、HTTPS 跳转、
静态文件托管方式和数据库长连接。集中在这里声明，部署时只需要保证
DJANGO_SETTINGS_MODULE=config.settings.production 且 .env 里填好密钥。
"""

from .base import *  # noqa: F401,F403

from .base import BASE_DIR, LOGGING, env, env_bool, env_list


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------
DEBUG = False

# 生产不允许默认值兜底：ALLOWED_HOSTS 必须由部署环境显式提供
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

# ---------------------------------------------------------------------------
# 安全
# ---------------------------------------------------------------------------
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
SECURE_HSTS_SECONDS = int(env("DJANGO_SECURE_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

# 经营数据看板不允许被第三方站点 iframe 嵌套，防点击劫持
X_FRAME_OPTIONS = "DENY"

# ---------------------------------------------------------------------------
# 数据库：长连接 600 秒，摊薄每次请求建连的开销；超时时间交给 MySQL 侧配置
# ---------------------------------------------------------------------------
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": env("MYSQL_DATABASE", "door_analysis"),
        "USER": env("MYSQL_USER", "door_analysis"),
        "PASSWORD": env("MYSQL_PASSWORD", ""),
        "HOST": env("MYSQL_HOST", "db"),
        "PORT": env("MYSQL_PORT", "3306"),
        "CONN_MAX_AGE": 600,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            "charset": "utf8mb4",
            "init_command": "SET sql_mode='STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION'",
        },
    }
}

# ---------------------------------------------------------------------------
# 静态文件：nginx 直出 /static/，whitenoise 作为兜底
# ---------------------------------------------------------------------------
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"
WHITENOISE_MAX_AGE = 60 * 60 * 24 * 30

# ---------------------------------------------------------------------------
# 日志：控制台给容器收集，文件留一份给排查历史问题（按天滚动）
# ---------------------------------------------------------------------------
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOGGING["handlers"]["file"] = {  # noqa: F405
    "class": "logging.handlers.TimedRotatingFileHandler",
    "filename": str(LOG_DIR / "door_analysis.log"),
    "when": "midnight",
    "backupCount": 30,
    "encoding": "utf-8",
    "formatter": "verbose",
}

# 错误单独一份文件，方便接入告警时直接盯这个文件
LOGGING["handlers"]["error_file"] = {  # noqa: F405
    "class": "logging.handlers.TimedRotatingFileHandler",
    "filename": str(LOG_DIR / "door_analysis.error.log"),
    "when": "midnight",
    "backupCount": 30,
    "encoding": "utf-8",
    "formatter": "verbose",
    "level": "ERROR",
}

LOGGING["root"]["handlers"] = ["console", "file"]  # noqa: F405
LOGGING["root"]["level"] = env("DJANGO_LOG_LEVEL", "INFO")  # noqa: F405
LOGGING["loggers"]["django"] = {  # noqa: F405
    "handlers": ["console", "file"],
    "level": "INFO",
    "propagate": False,
}
LOGGING["loggers"]["django.request"] = {  # noqa: F405
    "handlers": ["console", "error_file"],
    "level": "ERROR",
    "propagate": False,
}
LOGGING["loggers"]["apps"] = {  # noqa: F405
    "handlers": ["console", "file"],
    "level": "INFO",
    "propagate": False,
}

# 真实邮件发送：生产用 SMTP，凭据来自环境变量
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("EMAIL_HOST", "smtp.exmail.qq.com")
EMAIL_PORT = int(env("EMAIL_PORT", "465"))
EMAIL_HOST_USER = env("EMAIL_HOST_USER")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD")
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "noreply@example.com")

# 生产使用 Redis 缓存，多进程共享；地址缺失时回落到内存缓存不阻塞启动
REDIS_URL = env("REDIS_URL")
if REDIS_URL:
    CACHES = {  # noqa: F405
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
        }
    }
