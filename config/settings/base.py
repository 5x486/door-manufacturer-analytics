# -*- coding: utf-8 -*-
"""
公共配置（base）
================
只放「所有环境都一样」的东西。环境差异一律进 local / test / production。

设计原则：
  1. 所有可能随部署变化的值都从 .env 读，代码里不留硬编码；
  2. 业务口径（如数据基准日）集中在此，杜绝脚本里到处硬编码日期；
  3. 新增配置项时，同步更新 .env.example，保证「新人 clone 下来就能跑」。
"""
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
import os

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent.parent
PROJECT_ROOT = BASE_DIR

# 读取 .env（不存在也不报错，便于 CI 里完全用环境变量注入）
load_dotenv(BASE_DIR / ".env")


def env(key, default=None, cast=None):
    """读环境变量并可选做类型转换。空字符串按「未设置」处理。"""
    value = os.getenv(key)
    if value is None or value == "":
        value = default
    if value is None:
        return None
    if cast is bool:
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if cast is int:
        return int(value)
    if cast is float:
        return float(value)
    return value


def env_list(key, default=""):
    """读逗号分隔的列表型环境变量。"""
    raw = env(key, default)
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def env_bool(key, default=False):
    """布尔型环境变量（别名，语义更清晰；供各环境配置直接 import）。"""
    return env(key, default, bool)


# ---------------------------------------------------------------------------
# 安全与基础
# ---------------------------------------------------------------------------
SECRET_KEY = env(
    "DJANGO_SECRET_KEY",
    "django-insecure-dev-only-key-please-override-in-production-0123456789",
)
DEBUG = env("DJANGO_DEBUG", False, bool)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost")

# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",          # 模板里做千分位与人类可读数字
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "drf_spectacular",
]

LOCAL_APPS = [
    "apps.core",
    "apps.accounts",
    "apps.masterdata",
    "apps.sales",
    "apps.datasource",      # 数据接入层：CSV 导入器 + 补充数据生成器 + 管理命令
    "apps.analytics",
    "apps.dq",
    "apps.ml",
    "apps.api",
    "apps.dashboard",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# ---------------------------------------------------------------------------
# 中间件
# ---------------------------------------------------------------------------
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",       # 中文界面
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------------
# 模板
# ---------------------------------------------------------------------------
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.global_context",
            ],
        },
    },
]

# ---------------------------------------------------------------------------
# 数据库（默认 MySQL，测试环境可覆盖为 SQLite）
# ---------------------------------------------------------------------------
DB_ENGINE = env("DB_ENGINE", "mysql")

if DB_ENGINE == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / env("DB_NAME", "db.sqlite3"),
            "ATOMIC_REQUESTS": False,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": env("DB_NAME", "door_analysis"),
            "USER": env("DB_USER", "root"),
            "PASSWORD": env("DB_PASSWORD", ""),
            "HOST": env("DB_HOST", "127.0.0.1"),
            "PORT": env("DB_PORT", "3307"),
            # 表与连接统一 utf8mb4，避免 emoji / 生僻字写入失败
            "OPTIONS": {
                "charset": "utf8mb4",
                "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
            },
            "CONN_MAX_AGE": 0,           # local 覆盖为 60；production 覆盖为 600
            "ATOMIC_REQUESTS": False,
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------
# 项目启动即换成自定义用户模型（Django 官方建议：中途再换代价极大）
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

# ---------------------------------------------------------------------------
# 国际化
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = False          # 业务数据均为「日期」语义，统一用 naive datetime，避免时区漂移

# ---------------------------------------------------------------------------
# 静态与媒体
# ---------------------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# ---------------------------------------------------------------------------
# DRF
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    # 默认要求登录：经营数据含客户联系方式，不能匿名读
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": env("PAGE_SIZE", 50, int),
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.OrderingFilter",
        "rest_framework.filters.SearchFilter",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "EXCEPTION_HANDLER": "apps.api.exceptions.unified_exception_handler",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "杭博门业经营分析系统 API",
    "DESCRIPTION": (
        "定制门业企业经营分析 API。\n\n"
        "**统一响应包**：`{code, message, data, meta}`，其中 `meta.data_version` 标识数据版本、"
        "`meta.metric_definitions` 给出指标口径编码 ，， 前端 KPI 卡可直接展示「这个数怎么算的」。\n\n"
        "**统一筛选参数**：`period` / `date_from` / `date_to` / `grain` / `channel` / "
        "`product_category` / `customer_type` / `order_type`。"
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "TAGS": [
        {"name": "kpi", "description": "核心指标卡与目标达成"},
        {"name": "growth", "description": "增长与渠道分析"},
        {"name": "profit", "description": "成本、毛利与定价"},
        {"name": "customer", "description": "客户价值、留存与 360"},
        {"name": "delivery", "description": "交付履约与售后"},
        {"name": "cashflow", "description": "回款与现金流"},
        {"name": "workforce", "description": "人效与门店"},
        {"name": "simulate", "description": "What-if 决策模拟"},
        {"name": "dq", "description": "数据质量"},
        {"name": "ml", "description": "模型在线评分"},
        {"name": "meta", "description": "指标口径与元数据"},
    ],
}

# ---------------------------------------------------------------------------
# 缓存（默认本地内存；配置 REDIS_URL 自动切 Redis）
# ---------------------------------------------------------------------------
REDIS_URL = env("REDIS_URL", "")
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "hbms-local",
        }
    }

# ---------------------------------------------------------------------------
# 业务口径与目标（全项目唯一来源）
# ---------------------------------------------------------------------------
# 数据基准日：所有「距今多少天」「账龄」类指标都以它为准，
# 取代原先三个脚本里各自硬编码的 TODAY。
DATA_AS_OF = env("DATA_AS_OF", "2026-08-09")
DATA_AS_OF_DATE = date.fromisoformat(str(DATA_AS_OF))

# 年度营收目标（元）——用于「目标 vs 实际」达成率
SALES_TARGETS = {
    2023: env("SALES_TARGET_2023", 8_000_000, float),
    2024: env("SALES_TARGET_2024", 16_000_000, float),
    2025: env("SALES_TARGET_2025", 24_000_000, float),
    2026: env("SALES_TARGET_2026", 28_000_000, float),
}

# 分析接口结果缓存时长（秒）
ANALYTICS_CACHE_TTL = env("ANALYTICS_CACHE_TTL", 600, int)

# 项目元信息（页脚与 API meta 用）
PROJECT_NAME = "杭博门业经营分析系统"
PROJECT_SHORT_NAME = "杭博门业"
COMPANY_PROFILE = "浙江嘉兴桐乡 · 小型定制门厂 · 2023 年起步"
DATA_DISCLAIMER = "全部业务数据为程序随机模拟生成，不含任何真实个人或企业信息"

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name}: {message}",
            "style": "{",
        },
        "simple": {"format": "{levelname} {message}", "style": "{"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django.db.backends": {"level": "WARNING", "propagate": True},
        "apps": {"level": "INFO", "propagate": True},
    },
}

# ---------------------------------------------------------------------------
# 消息框架（给结果页用足样式）
# ---------------------------------------------------------------------------
from django.contrib.messages import constants as message_constants  # noqa: E402

MESSAGE_TAGS = {
    message_constants.DEBUG: "debug",
    message_constants.INFO: "info",
    message_constants.SUCCESS: "success",
    message_constants.WARNING: "warning",
    message_constants.ERROR: "danger",
}
