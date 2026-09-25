# 旧数据迁移：为升级前已存在的处罚单元补建原始证据封存包。
#
# 直接复用服务层 backfill_missing_packages（该函数只依赖 0003 已建好的
# EvidencePackage 表与既有业务表）：为每个还没有封存链的处罚单元生成
# 原始封存包（不可变清单 + 文件 SHA-256 摘要）。幂等，可重复执行；
# 文件缺失时如实记录 readable=false，不阻断迁移。
from django.db import migrations


def backfill_evidence_packages(apps, schema_editor):
    from assessment.services.sealing import backfill_missing_packages

    backfill_missing_packages(actor="migration-0004")


class Migration(migrations.Migration):

    dependencies = [
        ("assessment", "0003_evidence_package"),
    ]

    operations = [
        migrations.RunPython(backfill_evidence_packages, migrations.RunPython.noop),
    ]
