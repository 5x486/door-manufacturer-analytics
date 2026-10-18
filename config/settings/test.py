"""测试环境配置：SQLite 内存库 + 弱哈希，目标是让测试套件秒级跑完。

为什么不用 MySQL 跑测试：
1. 建立/销毁测试库的开销在几百个用例上是分钟级的浪费；
2. 内存库每个测试进程独享，天然并行、天然隔离，不会污染本地开发数据。
代价是 SQLite 与 MySQL 在少数函数行为上有差异，因此与 SQL 方言强相关的
用例请用 ``@pytest.mark.integration`` 标记，并在 CI 里切回 MySQL 跑一遍。
"""

from .base import *  # noqa: F401,F403

from .base import BASE_DIR, LOGGING, env_list


DEBUG = False

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "testserver,localhost,127.0.0.1")

# ---------------------------------------------------------------------------
# 数据库：内存 SQLite，进程退出即销毁，测试之间互不影响
# ---------------------------------------------------------------------------
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        "TEST": {
            "NAME": ":memory:",
        },
    }
}

# 测试数据量小、又反复建用户，MD5 能显著缩短认证相关用例的耗时。
# 这只在测试环境生效，生产仍使用 Django 默认的 PBKDF2。
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

# ---------------------------------------------------------------------------
# 静态与媒体：测试不写真实文件系统
# ---------------------------------------------------------------------------
# 测试环境必须使用「不压缩、不要 manifest」的静态存储。
# base.py 里配的是 CompressedManifestStaticFilesStorage，它要求 staticfiles.json
# 存在（由 collectstatic 生成）；测试环境没有这个文件，模板里每个 {% static %}
# 都会抛 "Missing staticfiles manifest entry"，导致所有页面测试全部失败 ——
# 而失败原因看起来像「页面坏了」，实际是测试配置问题，非常容易误判。
STORAGES["staticfiles"] = {  # noqa: F405
    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
}
MEDIA_ROOT = BASE_DIR / "artifacts" / "test_media"

# 邮件不出网
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# ---------------------------------------------------------------------------
# 日志：测试只关心失败信息，级别调高避免刷屏，否则 pytest 输出里全是噪声
# ---------------------------------------------------------------------------
LOGGING["root"]["level"] = "WARNING"  # noqa: F405
LOGGING["loggers"]["django"] = {  # noqa: F405
    "handlers": ["console"],
    "level": "WARNING",
    "propagate": False,
}
LOGGING["loggers"]["apps"] = {  # noqa: F405
    "handlers": ["console"],
    "level": "WARNING",
    "propagate": False,
}

# 测试库里的加密校验一律关闭，避免用例因为密码太简单而失败
AUTH_PASSWORD_VALIDATORS = []

# 保持与生产一致的迁移路径（不跳过迁移直接建表），
# 这样迁移文件写错时测试会立刻失败，而不是上线才炸。
MIGRATION_MODULES = {}
