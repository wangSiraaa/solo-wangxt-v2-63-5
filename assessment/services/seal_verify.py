"""
封存包校验：在线校验、离线（导出包）校验与离线还原。

铁律：校验失败**只能**产生 SealVerification 记录、并把*活动包*标记为
verification_failed；绝不修改 ProblemEvent / PenaltyUnit / PenaltyVersion /
DuplicateCandidate 中的任何一行，也绝不自动合并候选。
"""
import json
import os
import tarfile
import tempfile
from dataclasses import dataclass, field

from django.db import transaction

from assessment.models import SealPackage, SealVerification
from assessment.services.seal_manifest import (
    bytes_sha256,
    sha256_hex,
)


@dataclass
class VerifyReport:
    result: str
    manifest_ok: bool
    manifest_digest: str = ""
    checked_files: int = 0
    mismatch_files: list = field(default_factory=list)
    missing_files: list = field(default_factory=list)
    extra_files: list = field(default_factory=list)
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "result": self.result,
            "manifest_ok": self.manifest_ok,
            "manifest_digest": self.manifest_digest,
            "checked_files": self.checked_files,
            "mismatch_files": self.mismatch_files,
            "missing_files": self.missing_files,
            "extra_files": self.extra_files,
            "detail": self.detail,
        }


def verify_manifest_object(manifest: dict, expected_digest: str) -> tuple[bool, str]:
    """复算 canonical manifest 摘要并与封存值比对。"""
    digest = _digest(manifest)
    return digest == expected_digest, digest


def _digest(manifest):
    from assessment.services.seal_manifest import digest_manifest

    return digest_manifest(manifest)[1]


def _hash_path(path: str) -> str | None:
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as fh:
        return bytes_sha256(fh.read())


def verify_bundle_dir(root: str) -> VerifyReport:
    """
    离线校验一个解压后的封存包目录（不依赖数据库/Django ORM 语义）。

    目录约定：
      manifest.json            canonical 清单
      manifest.json.sha256     封存时清单摘要（单行 hex，可带文件名）
      files/...                清单 files[].rel_path 对应的证据文件
      verify_offline.py        纯标准库校验器（同算法）
    """
    manifest_path = os.path.join(root, "manifest.json")
    digest_path = os.path.join(root, "manifest.json.sha256")
    if not os.path.isfile(manifest_path):
        return VerifyReport(
            result=SealVerification.Result.INVALID,
            manifest_ok=False,
            detail="缺少 manifest.json，无法离线校验",
        )

    with open(manifest_path, "rb") as fh:
        raw = fh.read()
    actual_manifest_digest = sha256_hex(raw)

    expected_digest = ""
    if os.path.isfile(digest_path):
        with open(digest_path, "r", encoding="utf-8") as fh:
            expected_digest = fh.read().strip().split()[0].strip().lower()

    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return VerifyReport(
            result=SealVerification.Result.INVALID,
            manifest_ok=False,
            manifest_digest=actual_manifest_digest,
            detail=f"manifest.json 不是合法 JSON: {exc}",
        )

    manifest_ok = bool(expected_digest) and actual_manifest_digest.lower() == expected_digest.lower()
    files = (manifest.get("content") or {}).get("files") or []

    report = VerifyReport(
        result=SealVerification.Result.VALID,
        manifest_ok=manifest_ok,
        manifest_digest=actual_manifest_digest,
    )
    if not manifest_ok:
        report.result = SealVerification.Result.INVALID
        report.detail = "清单摘要不符（manifest.json 被改动或 .sha256 不匹配）"

    listed_paths = set()
    for item in files:
        rel = item["rel_path"]
        listed_paths.add(rel)
        on_disk = os.path.join(root, rel)
        expected_sha = (item.get("sha256") or "").lower()
        if item.get("missing_at_seal"):
            # 封存时就缺失：有文件反而异常；没有文件（含 .MISSING 占位）属预期，不计失败
            if os.path.isfile(on_disk):
                report.extra_files.append(rel)
            continue
        if not os.path.isfile(on_disk):
            report.missing_files.append(rel)
            continue
        actual_sha = _hash_path(on_disk) or ""
        report.checked_files += 1
        if expected_sha and actual_sha.lower() != expected_sha:
            report.mismatch_files.append(
                {"rel_path": rel, "expected_sha256": expected_sha, "actual_sha256": actual_sha}
            )

    # 包外多余文件（排除工具、清单自身与缺失文件的 .MISSING 占位说明）
    allowed_extra = {"manifest.json", "manifest.json.sha256", "verify_offline.py", "README.txt"}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if rel in allowed_extra or rel in listed_paths or rel.endswith(".MISSING"):
                continue
            report.extra_files.append(rel)

    if report.missing_files or report.mismatch_files or report.extra_files:
        report.result = SealVerification.Result.INVALID
        if not report.detail:
            report.detail = "证据文件缺失、摘要不符或存在包外文件"
    elif report.result == SealVerification.Result.VALID:
        report.detail = "离线校验通过：清单与全部证据文件完整一致"
    return report


def restore_from_bundle_dir(root: str) -> dict:
    """
    从离线包还原“唯一处罚 + 完整证据”视图（不查库）。

    返回结构刻意扁平，供监督复核离线阅读：处罚单号唯一、当前版本、
    版本链、整改、候选判定、以及全部证据文件及其摘要。
    """
    with open(os.path.join(root, "manifest.json"), "rb") as fh:
        manifest = json.loads(fh.read().decode("utf-8"))
    return restore_from_manifest(manifest)


def restore_from_manifest(manifest: dict) -> dict:
    content = manifest.get("content") or {}
    subject = content.get("subject") or {}
    penalty = content.get("penalty") or {}
    versions = content.get("versions") or []
    current = max(versions, key=lambda v: v.get("version_no", 0)) if versions else None
    return {
        "package_no": manifest.get("package_no"),
        "sealed_at": manifest.get("sealed_at"),
        "lineage": manifest.get("lineage") or {},
        "unique_penalty": {
            "penalty_no": subject.get("penalty_no") or penalty.get("penalty_no"),
            "penalty_id": subject.get("penalty_id"),
            "event_no": subject.get("event_no"),
            "points": penalty.get("points"),
            "status": penalty.get("status"),
            "locked_version_no": penalty.get("locked_version_no"),
            "contractor_name": penalty.get("contractor_name"),
            "current_version": current,
        },
        "event": content.get("event"),
        "grid": content.get("grid"),
        "contract": content.get("contract"),
        "photos": content.get("photos") or [],
        "candidates": content.get("candidates") or [],
        "rectification": content.get("rectification"),
        "versions": versions,
        "escalations": content.get("escalations") or [],
        "reviews": content.get("reviews") or [],
        "policy": content.get("policy") or {},
        "files": content.get("files") or [],
    }


def verify_bundle_archive(tar_bytes: bytes) -> tuple[VerifyReport, dict | None]:
    """上传 tar 包 → 临时目录解压 → 纯离线校验 + 还原视图。"""
    tmp = tempfile.mkdtemp(prefix="seal-offline-")
    try:
        with tempfile.NamedTemporaryFile(suffix=".tar") as tmp_tar:
            tmp_tar.write(tar_bytes)
            tmp_tar.flush()
            with tarfile.open(tmp_tar.name, "r:") as tar:
                _safe_extract(tar, tmp)
        # 兼容打包时多一层目录
        root = _find_bundle_root(tmp)
        report = verify_bundle_dir(root)
        restored = None
        if report.manifest_ok or os.path.isfile(os.path.join(root, "manifest.json")):
            try:
                restored = restore_from_bundle_dir(root)
            except (ValueError, KeyError):
                restored = None
        return report, restored
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def _safe_extract(tar: tarfile.TarFile, dest: str) -> None:
    dest_abs = os.path.abspath(dest)
    for member in tar.getmembers():
        member_path = os.path.abspath(os.path.join(dest, member.name))
        if not member_path.startswith(dest_abs + os.sep) and member_path != dest_abs:
            raise ValueError(f"非法归档路径: {member.name}")
    tar.extractall(dest)


def _find_bundle_root(root: str) -> str:
    if os.path.isfile(os.path.join(root, "manifest.json")):
        return root
    for name in sorted(os.listdir(root)):
        sub = os.path.join(root, name)
        if os.path.isdir(sub) and os.path.isfile(os.path.join(sub, "manifest.json")):
            return sub
    return root


@transaction.atomic
def verify_active_package(
    package: SealPackage,
    *,
    source: str = SealVerification.Source.ONLINE,
    actor: str = "system",
) -> tuple[SealPackage, SealVerification, VerifyReport]:
    """
    在线校验：以 DB 中封存的 manifest 为准，逐一复算证据文件摘要。

    * 活动包失败 → verification_failed；恢复后再次校验通过 → 回到 sealed；
    * 历史包（supplemented/superseded）状态不变，只留痕；
    * 任何失败都不会反向修改业务链。
    """
    package = SealPackage.objects.select_for_update().get(pk=package.pk)
    manifest = package.manifest or {}
    expected_digest = package.manifest_digest
    _, recomputed = _digest2(manifest)
    manifest_ok = bool(expected_digest) and recomputed == expected_digest

    report = VerifyReport(
        result=SealVerification.Result.VALID,
        manifest_ok=manifest_ok,
        manifest_digest=recomputed,
    )
    if not manifest_ok:
        report.result = SealVerification.Result.INVALID
        report.detail = "封存清单自身摘要不符"

    for sf in package.files.order_by("rel_path"):
        if sf.missing_at_seal:
            continue
        photo = sf.photo
        digest = None
        exists = False
        if photo is not None:
            try:
                exists = photo.image.storage.exists(photo.image.name)
                if exists:
                    with photo.image.storage.open(photo.image.name, "rb") as fh:
                        digest = bytes_sha256(fh.read())
            except FileNotFoundError:
                exists = False
        if not exists or digest is None:
            report.missing_files.append(sf.rel_path)
            continue
        report.checked_files += 1
        if sf.sha256 and digest.lower() != sf.sha256.lower():
            report.mismatch_files.append(
                {"rel_path": sf.rel_path, "expected_sha256": sf.sha256, "actual_sha256": digest}
            )

    if report.missing_files or report.mismatch_files:
        report.result = SealVerification.Result.INVALID
        if not report.detail:
            report.detail = "证据文件缺失或摘要不符（业务链未受影响）"
    elif report.result == SealVerification.Result.VALID:
        report.detail = "在线校验通过：清单与全部证据文件完整一致"

    record = SealVerification.objects.create(
        package=package,
        source=source,
        result=report.result,
        manifest_ok=report.manifest_ok,
        checked_files=report.checked_files,
        mismatch_files=report.mismatch_files,
        missing_files=report.missing_files,
        extra_files=report.extra_files,
        detail=report.detail,
        actor=actor,
    )

    if package.is_active:
        if report.result == SealVerification.Result.INVALID:
            package.status = SealPackage.Status.VERIFICATION_FAILED
        elif package.status == SealPackage.Status.VERIFICATION_FAILED:
            # 文件恢复/补回后校验通过：回到已封存
            package.status = SealPackage.Status.SEALED
        package.save(update_fields=["status", "updated_at"])
    return package, record, report


def _digest2(manifest):
    from assessment.services.seal_manifest import digest_manifest

    raw, digest = digest_manifest(manifest)
    return raw, digest
