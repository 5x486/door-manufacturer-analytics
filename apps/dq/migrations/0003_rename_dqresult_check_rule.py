# -*- coding: utf-8 -*-
"""把 dq_result.check 重命名为 rule。

背景：``check`` 与 Django 的 ``Model.check()`` 内置方法同名，系统检查直接报
``models.E020``，项目连启动都过不去。改名后语义更准（这条结果属于哪条规则），
同时用 ``db_column="check_id"`` 保留原列名，因此**不产生任何表结构变更**：
本条迁移只同步 Django 的迁移状态，不改动数据库中的列。
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("dq", "0002_alter_dqresult_message"),
    ]

    operations = [
        migrations.AlterField(
            model_name="dqresult",
            name="check",
            field=__import__("django.db.models", fromlist=["ForeignKey"]).ForeignKey(
                db_column="check_id",
                on_delete=__import__("django.db.models", fromlist=["CASCADE"]).CASCADE,
                related_name="results",
                to="dq.dqcheck",
                verbose_name="规则",
            ),
            preserve_default=False,
        ),
        migrations.RenameField(
            model_name="dqresult",
            old_name="check",
            new_name="rule",
        ),
    ]