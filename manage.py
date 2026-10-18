#!/usr/bin/env python
"""杭博门业经营分析系统 ，， Django 命令行入口。

这里集中做两件事：
1. 在导入 Django 之前读取项目根目录的 ``.env``，让数据库密码、密钥等敏感项
   只存在于本机环境而不进入版本库；延迟到 settings 里读取会太晚，因为
   ``django.setup()`` 触发 settings 加载时就要求这些变量已经就位。
2. 把默认配置模块指向 ``config.settings.local``，而生产/测试环境通过
   ``DJANGO_SETTINGS_MODULE`` 环境变量覆盖，无需改动本文件。
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def main() -> None:
    """执行管理命令，失败时以非零状态码退出。"""
    base_dir = Path(__file__).resolve().parent
    load_dotenv(base_dir / ".env")

    # Windows 控制台默认 GBK，管理命令输出里的中文/符号（如 ¥）会触发
    # UnicodeEncodeError 直接中断命令。这里显式切到 UTF-8：
    # 输出乱码只是难看，编码异常中断命令却会让人以为「代码有 bug」。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):   # 被重定向到不支持 reconfigure 的对象
            pass

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "无法导入 Django，请确认已激活虚拟环境并执行 pip install -r requirements.txt"
        ) from exc

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
