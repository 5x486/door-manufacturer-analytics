# -*- coding: utf-8 -*-
"""
杭博门业经营分析系统 · 项目配置包
====================================

配置分层（12-factor 风格）：
    base.py        公共配置（所有环境共享）
    local.py       本地开发（DEBUG、localhost、宽松日志）
    test.py        测试（SQLite 内存库、快速哈希）
    production.py  生产（安全项、whitenoise、持久连接）

通过环境变量 DJANGO_SETTINGS_MODULE 选择，默认 config.settings.local。
"""
