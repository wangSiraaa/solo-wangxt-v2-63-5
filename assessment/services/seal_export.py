"""
封存包导出：生成可离线校验的 tar 包。

中断可续作：
* 每个导出任务有独立暂存目录，证据文件按 SealedFile 顺序逐个复制（幂等覆盖），
  每写成功一个推进 cursor / done_files；
* 构建中断（进程被杀 / 异常）后用同一 (package, client_token) 重试，
  跳过已完成文件，仅续传剩余文件；
* 全部文件就绪后才写 manifest、校验器并打包，bundle 以原子 rename 发布，
  completed 状态不可重入——因此“导出中断重试”永远只产出同一个 bundle。
"""
import io
import os
import shutil
import tarfile

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from assessment.exceptions import DomainError
from assessment.models import SealExportJob, SealPackage
from assessment.services.seal_manifest import (
    canonical_json_bytes,
    file_sha256,
)

OFFLINE_VERIFIER = '''#!/usr/bin/env python3
"""
封存包离线校验器（纯标准库，无 Django / 无数据库依赖）。

用法：
    python verify_offline.py [解压目录]   # 默认当前目录
退出码：0=校验通过；1=校验失败；2=缺少清单。
"""
import hashlib
import json
import os
import sys


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(root):
    manifest_path = os.path.join(root, "manifest.json")
    if not os.path.isfile(manifest_path):
        print("FAIL: 缺少 manifest.json")
        return 2
    raw = open(manifest_path, "rb").read()
    actual = hashlib.sha256(raw).hexdigest()
    digest_path = os.path.join(root, "manifest.json.sha256")
    expected = ""
    if os.path.isfile(digest_path):
        expected = open(digest_path, encoding="utf-8").read().strip().split()[0].lower()
    manifest_ok = bool(expected) and actual == expected
    print(f"manifest digest: {actual}")
    print(f"manifest digest expected: {expected or '(none)'}")
    print(f"manifest ok: {manifest_ok}")
    manifest = json.loads(raw.decode("utf-8"))
    ok = manifest_ok
    files = (manifest.get("content") or {}).get("files") or []
    checked = missing = mismatch = 0
    listed = set()
    for item in files:
        rel = item["rel_path"]
        listed.add(rel)
        on_disk = os.path.join(root, rel)
        if item.get("missing_at_seal"):
            continue
        if not os.path.isfile(on_disk):
            print(f"MISSING: {rel}")
            missing += 1
            ok = False
            continue
        got = sha256_file(on_disk)
        if item.get("sha256") and got != item["sha256"].lower():
            print(f"MISMATCH: {rel} expected={item['sha256']} actual={got}")
            mismatch += 1
            ok = False
        else:
            checked += 1
    # 包外多余文件（工具/清单/.MISSING 占位除外）
    allowed = {"manifest.json", "manifest.json.sha256", "verify_offline.py", "README.txt"}
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if rel in allowed or rel in listed or rel.endswith(".MISSING"):
                continue
            print(f"EXTRA: {rel}")
            ok = False
    print(f"checked={checked} missing={missing} mismatch={mismatch}")
    restored = manifest.get("content", {}).get("penalty", {})
    print(f"penalty_no={restored.get('penalty_no')} points={restored.get('points')} "
          f"locked_version_no={restored.get('locked_version_no')}")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else os.getcwd()))
'''

BUNDLE_README = """街道环卫考核 —— 证据封存离线包
================================

* manifest.json         封存清单（canonical JSON）
* manifest.json.sha256  清单摘要（封存时计算）
* files/photos/         全部关联证据照片（含整改照片）
* verify_offline.py     纯标准库离线校验器

离线校验：
    python3 verify_offline.py .
退出码 0 表示清单与全部证据文件完整一致。
"""


class ExportError(DomainError):
    status_code = 409
    default_detail = "封存包导出状态不允许该操作"


def _export_root() -> str:
    root = os.path.join(str(settings.MEDIA_ROOT), "seal_exports")
    os.makedirs(root, exist_ok=True)
    return root


def _stage_dir(job: SealExportJob) -> str:
    path = os.path.join(_export_root(), f"job-{job.id}")
    return path


def _reset_stage_dir(job: SealExportJob) -> str:
    """导出（重新）构建前清空暂存目录，避免历史残留文件被打进 bundle。"""
    path = _stage_dir(job)
    if os.path.isdir(path):
        shutil.rmtree(path)
    os.makedirs(os.path.join(path, "files", "photos"), exist_ok=True)
    return path


def _bundle_path(job: SealExportJob) -> str:
    return os.path.join(_export_root(), f"{job.package.package_no}-job{job.id}.tar")


@transaction.atomic
def create_export_job(package: SealPackage, *, client_token: str, actor: str = "") -> SealExportJob:
    """登记导出任务（幂等：同 package+token 返回同一任务）。"""
    if not package.manifest_digest or package.status == SealPackage.Status.PENDING:
        raise ExportError("封存包尚未完成封存，不能导出")

    job = SealExportJob.objects.filter(package=package, client_token=client_token).first()
    if job is not None:
        return job

    total = package.files.count()
    try:
        job = SealExportJob.objects.create(
            package=package,
            client_token=client_token,
            status=SealExportJob.Status.PENDING,
            total_files=total,
            created_by=actor,
        )
    except Exception:
        # 并发创建兜底
        return SealExportJob.objects.get(package=package, client_token=client_token)
    return job


def run_export_job(job_id: int, *, fail_after: int | None = None) -> SealExportJob:
    """
    推进导出任务（可重复调用续作）。

    不套外层事务：状态翻转与每个文件的进度都独立提交，
    这样中断（含 fail_after 测试钩子）后 cursor/done_files 不回滚，重试可续作。

    fail_after: 测试钩子——复制 N 个文件后抛出异常模拟中断。
    """
    with transaction.atomic():
        job = SealExportJob.objects.select_for_update().get(pk=job_id)
        package_id = job.package_id
        if job.status == SealExportJob.Status.COMPLETED:
            return job
        if job.status == SealExportJob.Status.PENDING:
            # 首次构建：清空同名残留暂存目录（测试复用 ID 时尤其重要）
            stage = _reset_stage_dir(job)
            job.status = SealExportJob.Status.BUILDING
            job.save(update_fields=["status", "updated_at"])
        else:
            # failed/building 续作：保留已复制文件与 cursor
            stage = _stage_dir(job)
            os.makedirs(os.path.join(stage, "files", "photos"), exist_ok=True)

    package = SealPackage.objects.get(pk=package_id)
    return _copy_loop(job_id=job_id, package=package, stage=stage, fail_after=fail_after)


def _persist_progress(job_id: int, *, done_files: int, cursor: str, status: str, error: str = ""):
    """逐文件独立提交进度——保证中断（含测试钩子）后 cursor/done 不回滚。"""
    with transaction.atomic():
        SealExportJob.objects.filter(pk=job_id).update(
            done_files=done_files, cursor=cursor, status=status, error=error[:512],
        )


def _copy_loop(*, job_id: int, package: SealPackage, stage: str, fail_after: int | None) -> SealExportJob:
    sealed_files = list(package.files.order_by("rel_path"))
    copied_this_run = 0

    try:
        for index, sf in enumerate(sealed_files, start=1):
            target = os.path.join(stage, sf.rel_path)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            # 续作：文件已完整落盘则跳过（missing 占位文件也重建，幂等）
            if os.path.isfile(target):
                # 对齐历史进度（例如 cursor 已越过但计数未刷新）
                _persist_progress(
                    job_id, done_files=index, cursor=sf.rel_path,
                    status=SealExportJob.Status.BUILDING,
                )
                continue
            if sf.missing_at_seal or sf.photo_id is None:
                # 封存时缺失的文件：写占位说明，保证离线校验逻辑可复现
                with open(target + ".MISSING", "w", encoding="utf-8") as fh:
                    fh.write(f"文件在封存时即缺失: {sf.rel_path}\n")
            else:
                photo = sf.photo
                if not photo.image.storage.exists(photo.image.name):
                    with open(target + ".MISSING", "w", encoding="utf-8") as fh:
                        fh.write(f"文件在导出时缺失: {sf.rel_path}\n")
                else:
                    with photo.image.storage.open(photo.image.name, "rb") as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
            copied_this_run += 1
            _persist_progress(
                job_id,
                done_files=index,
                cursor=sf.rel_path,
                status=SealExportJob.Status.BUILDING,
            )
            if fail_after is not None and copied_this_run >= fail_after:
                raise _ExportInterrupted(f"模拟导出中断：本次已写入 {copied_this_run} 个文件")

        # 文件齐了 → 写清单与校验器
        manifest_bytes = canonical_json_bytes(package.manifest)
        with open(os.path.join(stage, "manifest.json"), "wb") as fh:
            fh.write(manifest_bytes)
        with open(os.path.join(stage, "manifest.json.sha256"), "w", encoding="utf-8") as fh:
            fh.write(f"{package.manifest_digest}  manifest.json\n")
        with open(os.path.join(stage, "verify_offline.py"), "w", encoding="utf-8") as fh:
            fh.write(OFFLINE_VERIFIER)
        os.chmod(os.path.join(stage, "verify_offline.py"), 0o755)
        with open(os.path.join(stage, "README.txt"), "w", encoding="utf-8") as fh:
            fh.write(BUNDLE_README)

        final_tar = _bundle_path(SealExportJob.objects.get(pk=job_id))
        tmp_tar = final_tar + ".part"
        # 清单中的 rel_path 相对包根，因此 tar 内直接平铺（不套 package_no 目录）
        with tarfile.open(tmp_tar, "w") as tar:
            for entry in sorted(os.listdir(stage)):
                tar.add(os.path.join(stage, entry), arcname=entry)
        digest = file_sha256(tmp_tar)
        os.replace(tmp_tar, final_tar)  # 原子发布

        with transaction.atomic():
            job = SealExportJob.objects.select_for_update().get(pk=job_id)
            job.bundle_path = final_tar
            job.bundle_digest = digest
            job.size_bytes = os.path.getsize(final_tar)
            job.status = SealExportJob.Status.COMPLETED
            job.error = ""
            job.completed_at = timezone.now()
            job.save()
        return SealExportJob.objects.get(pk=job_id)
    except _ExportInterrupted as exc:
        # 测试中断钩子：进度已逐文件提交，仅补记 error，状态保持 building 等待续作
        SealExportJob.objects.filter(pk=job_id).update(error=str(exc)[:512])
        raise
    except Exception as exc:  # 真实故障：保留已复制文件，标记 failed 可重试
        current = SealExportJob.objects.get(pk=job_id)
        _persist_progress(
            job_id,
            done_files=current.done_files,
            cursor=current.cursor,
            status=SealExportJob.Status.FAILED,
            error=str(exc),
        )
        raise


class _ExportInterrupted(Exception):
    """测试用中断信号（非系统故障）。"""


def read_bundle_bytes(job: SealExportJob) -> bytes:
    if job.status != SealExportJob.Status.COMPLETED or not job.bundle_path:
        raise ExportError("导出任务尚未完成")
    with open(job.bundle_path, "rb") as fh:
        return fh.read()
