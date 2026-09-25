"""
历史数据封存迁移命令：为封存功能上线前已存在的处罚单元补建 legacy 封存包。

特性：
* 幂等可续跑——已有任意封存包的处罚跳过，中断后重跑只补缺口；
* 不会产生第二个活动包；
* --limit 支持小批量迁移（长事务风险控制）。
"""
from django.core.management.base import BaseCommand

from assessment.services.sealing import migrate_legacy_seals


class Command(BaseCommand):
    help = "为尚无封存包的历史处罚单元批量补建 legacy 证据封存包（幂等可续跑）"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None, help="本次最多迁移条数")
        parser.add_argument("--actor", default="legacy-migration", help="迁移操作人标识")

    def handle(self, *args, **options):
        result = migrate_legacy_seals(limit=options["limit"], actor=options["actor"])
        self.stdout.write(
            self.style.SUCCESS(
                f"历史封存迁移完成：新建 {result['migrated']} 个，跳过 {result['skipped']} 个"
            )
        )
