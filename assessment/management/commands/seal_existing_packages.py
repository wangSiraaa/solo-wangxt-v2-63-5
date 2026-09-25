"""旧数据迁移命令：为还没有封存包的处罚单元补建原始证据封存包（幂等）。"""
from django.core.management.base import BaseCommand

from assessment.services.sealing import backfill_missing_packages


class Command(BaseCommand):
    help = "为既有处罚单元补建证据封存包（旧数据迁移；已有封存链的自动跳过，可反复执行）"

    def add_arguments(self, parser):
        parser.add_argument("--actor", default="seal-existing-command", help="记录为封存人，默认 seal-existing-command")

    def handle(self, *args, **options):
        result = backfill_missing_packages(actor=options["actor"])
        for package in result.created:
            self.stdout.write(f"  补建 {package.package_no} -> 处罚单 {package.penalty.penalty_no}")
        self.stdout.write(self.style.SUCCESS(
            f"完成：新建 {result.created_count} 个封存包，跳过已有封存链 {result.skipped_existing} 个"
        ))
