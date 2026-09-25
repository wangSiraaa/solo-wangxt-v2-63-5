"""
证据封存包服务。

核心规则：
* 封存（seal）：为处罚单元生成不可变清单 manifest——全部关联照片（含
  SHA-256 文件摘要）、关键元数据、候选判定、整改记录、当前处罚版本；
  清单规范化 JSON 的 SHA-256 即 manifest_hash。封存后 manifest 永不修改。
* 重复封存 / 并发请求：同一处罚单元至多一个活动包（pending/sealed）。
  服务层先对处罚单元行加锁串行化，数据库再以部分唯一约束兜底；
  已存在封存链时直接返回链头（幂等），绝不产生第二个活动包。
* 补充 / 替代（supplement / replace）：封存后发生补拍、整改、升级、更正，
  旧包一个字节都不改，只能基于当前活动包生成显式关联（parent）的新包，
  旧包状态变为 superseded（已补充/已替代）。新包同样是完整快照，
  可独立离线校验；与父包的摘要差异正是篡改/变更的痕迹。
* 校验（verify）：只重写封存包自身的 status / verify_report——文件摘要
  不符时标记 verify_failed，绝不回写事件、处罚单元或候选判定。
* 导出（export）：确定性 ZIP（固定时间戳、有序条目），中断后重试得到
  相同字节；导出前逐文件核对摘要，缺失/损坏即拒绝导出（409），
  不会悄悄发出不完整的证据包。
* 离线校验（verify_export_archive / docs/verify_evidence_package.py）：
  不依赖数据库，仅凭导出包即可还原唯一处罚单号、事件与完整证据清单。
"""
import hashlib
import json
import zipfile
from dataclasses import dataclass, field
from io import BytesIO

from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction

from assessment.exceptions import (
    InvalidExportArchive,
    PackageExportFailed,
    PackageNotActive,
)
from assessment.models import (
    DuplicateCandidate,
    EvidencePackage,
    EvidencePhoto,
    PenaltyUnit,
    _new_id,
)
from assessment.services.clock import Clock, SystemClock

MANIFEST_VERSION = 1
# ZIP 规范支持的最早时间（1980-01-01），固定它保证导出字节确定、可重试
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


# ---------------------------------------------------------------------------
# 清单（manifest）
# ---------------------------------------------------------------------------

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_manifest_bytes(manifest: dict) -> bytes:
    """清单的规范化字节形式：键排序、紧凑分隔符、UTF-8。哈希与导出都用它。"""
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def manifest_hash_of(manifest: dict) -> str:
    return sha256_hex(canonical_manifest_bytes(manifest))


def _iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None


def _point_payload(point) -> dict | None:
    if point is None:
        return None
    return {"lng": point.x, "lat": point.y, "srid": 4326}


def _file_payload(photo: EvidencePhoto) -> dict:
    """
    照片文件摘要。文件不可读（丢失/存储故障）时如实记录 readable=false，
    封存仍然成立——后续校验会把它标记为校验失败，而不是假装文件完好。
    """
    name = photo.image.name
    try:
        with default_storage.open(name, "rb") as fh:
            data = fh.read()
    except OSError:
        return {"path": name, "sha256": None, "size": None, "readable": False}
    return {"path": name, "sha256": sha256_hex(data), "size": len(data), "readable": True}


def _photo_payload(photo: EvidencePhoto, *, role: str) -> dict:
    return {
        "photo_id": photo.id,
        "role": role,
        "phash": photo.phash,
        "captured_at": _iso(photo.captured_at),
        "location": _point_payload(photo.location),
        "uploader": photo.uploader,
        "note": photo.note,
        "file": _file_payload(photo),
    }


def _candidate_payload(candidate: DuplicateCandidate) -> dict:
    return {
        "candidate_id": candidate.id,
        "photo_id": candidate.photo_id,
        "matched_photo_id": candidate.matched_photo_id,
        "hamming_distance": candidate.hamming_distance,
        "status": candidate.status,
        "decision_note": candidate.decision_note,
        "decided_by": candidate.decided_by,
        "decided_at": _iso(candidate.decided_at),
    }


def build_manifest(
    penalty: PenaltyUnit,
    *,
    package_no: str,
    kind: str,
    parent_package_no: str | None,
    sealed_at,
    sealed_by: str,
    note: str = "",
) -> dict:
    """
    按当前数据库状态构建不可变清单。

    取证边界（防止“跨地点同图候选混入同一封存包”）：
    * 照片只收 event 名下（含整改照片，角色区分）——其他事件的照片即使
      pHash 完全相同也绝不进入本包文件清单；
    * 候选判定只收“以本事件照片为新照片（photo）”的候选——即针对本事件
      照片做出的判定；被匹配照片只以 id/pHash 引用，不带对方文件。
    """
    event = penalty.event

    photos: list[dict] = []
    seen_photo_ids: set[int] = set()
    for photo in event.photos.order_by("captured_at", "id"):
        photos.append(_photo_payload(photo, role="evidence"))
        seen_photo_ids.add(photo.id)

    rectification = getattr(event, "rectification", None)
    rectification_payload = None
    if rectification is not None:
        rectification_payload = {
            "note": rectification.note,
            "submitted_by": rectification.submitted_by,
            "submitted_at": _iso(rectification.submitted_at),
            "photo_id": rectification.photo_id,
        }
        if rectification.photo_id and rectification.photo_id not in seen_photo_ids:
            photos.append(_photo_payload(rectification.photo, role="rectification"))
            seen_photo_ids.add(rectification.photo_id)

    candidates = (
        DuplicateCandidate.objects.filter(photo__event=event)
        .order_by("id")
    )

    current_version = penalty.versions.order_by("-version_no").first()
    current_version_payload = None
    if current_version is not None:
        current_version_payload = {
            "version_id": current_version.id,
            "version_no": current_version.version_no,
            "kind": current_version.kind,
            "points": str(current_version.points),
            "escalation_level": current_version.escalation_level,
            "reason": current_version.reason,
            "actor": current_version.actor,
            "created_at": _iso(current_version.created_at),
        }

    return {
        "manifest_version": MANIFEST_VERSION,
        "package_no": package_no,
        "kind": kind,
        "parent_package_no": parent_package_no,
        "sealed_at": _iso(sealed_at),
        "sealed_by": sealed_by,
        "note": note,
        "penalty": {
            "penalty_no": penalty.penalty_no,
            "status": penalty.status,
            "points": str(penalty.points),
            "escalation_level": penalty.escalation_level,
            "contractor_name": penalty.contractor_name,
            "contract_code": penalty.contract.code if penalty.contract_id else None,
            "locked_version_no": (
                penalty.locked_version.version_no if penalty.locked_version_id else None
            ),
            "current_version": current_version_payload,
        },
        "event": {
            "event_no": event.event_no,
            "category": event.category,
            "status": event.status,
            "description": event.description,
            "location": _point_payload(event.location),
            "occurred_at": _iso(event.occurred_at),
            "grid_code": event.grid.code if event.grid_id else None,
            "contract_code": event.contract.code if event.contract_id else None,
            "contractor_name": event.contractor_name,
            "sla_hours": event.sla_hours,
        },
        "photos": photos,
        "candidate_decisions": [_candidate_payload(c) for c in candidates],
        "rectification": rectification_payload,
    }


# ---------------------------------------------------------------------------
# 封存 / 补充 / 替代
# ---------------------------------------------------------------------------

def _chain_head(penalty: PenaltyUnit) -> EvidencePackage | None:
    """封存链的链头（没有任何后继的那个包）。一个处罚单元只有一条链。"""
    return (
        EvidencePackage.objects.filter(penalty=penalty, children__isnull=True)
        .order_by("-created_at", "-id")
        .first()
    )


def _create_package(
    penalty: PenaltyUnit,
    *,
    kind: str,
    parent: EvidencePackage | None,
    actor: str,
    note: str,
    clock: Clock | None,
) -> EvidencePackage:
    """在同一事务内：建包（先 pending）→ 固化清单 → sealed；若需顶替旧包，先把旧包置为 superseded。"""
    clock = clock or SystemClock()
    now = clock.now()

    if parent is not None:
        parent.status = EvidencePackage.Status.SUPERSEDED
        parent.save(update_fields=["status", "updated_at"])

    # 编号先定下来——清单里要写它
    package_no = _new_id("EP")
    manifest = build_manifest(
        penalty,
        package_no=package_no,
        kind=kind,
        parent_package_no=parent.package_no if parent else None,
        sealed_at=now,
        sealed_by=actor,
        note=note,
    )
    current_version = penalty.versions.order_by("-version_no").first()
    package = EvidencePackage(
        package_no=package_no,
        penalty=penalty,
        kind=kind,
        status=EvidencePackage.Status.SEALED,
        parent=parent,
        sealed_version=current_version,
        manifest=manifest,
        manifest_hash=manifest_hash_of(manifest),
        sealed_at=now,
        sealed_by=actor,
        note=note,
    )
    package.save()
    return package


@transaction.atomic
def seal_penalty(
    penalty: PenaltyUnit,
    *,
    actor: str = "system",
    note: str = "",
    clock: Clock | None = None,
) -> tuple[EvidencePackage, bool]:
    """
    为处罚单元建立首个封存包。幂等：已存在封存链时返回链头，不新建。

    返回 (package, created)。并发安全：先锁处罚单元行串行化，
    部分唯一约束 uniq_active_evidence_package 兜底。
    """
    penalty = PenaltyUnit.objects.select_for_update().get(pk=penalty.pk)
    head = _chain_head(penalty)
    if head is not None:
        return head, False
    try:
        return _create_package(
            penalty, kind=EvidencePackage.Kind.ORIGINAL, parent=None,
            actor=actor, note=note, clock=clock,
        ), True
    except IntegrityError:
        # 极端竞态：另一个事务已抢先建包——以对方为准，保证只有一个活动包
        head = _chain_head(penalty)
        if head is not None:
            return head, False
        raise


@transaction.atomic
def create_successor_package(
    parent: EvidencePackage,
    *,
    kind: str,
    actor: str = "system",
    note: str = "",
    clock: Clock | None = None,
) -> EvidencePackage:
    """
    基于当前活动包生成补充包 / 替代包：
    * parent 必须是链头且处于 sealed / verify_failed（pending 正在封存、
      superseded 已有后继，都不允许再派生）；
    * 新包按当前数据库状态重新取完整快照，parent 显式关联旧包；
    * 旧包只改状态为 superseded，manifest / manifest_hash 一字节不动。
    """
    if kind not in (EvidencePackage.Kind.SUPPLEMENT, EvidencePackage.Kind.REPLACEMENT):
        raise PackageNotActive(f"不支持的后继包类型 {kind}")
    penalty = PenaltyUnit.objects.select_for_update().get(pk=parent.penalty_id)
    parent = EvidencePackage.objects.select_for_update().get(pk=parent.pk)

    if parent.status not in (EvidencePackage.Status.SEALED, EvidencePackage.Status.VERIFY_FAILED):
        raise PackageNotActive(
            f"封存包 {parent.package_no} 当前状态为 {parent.get_status_display()}，"
            "只能基于已封存（或校验失败待重封）的链头创建补充/替代包"
        )
    if parent.children.exists():
        raise PackageNotActive(f"封存包 {parent.package_no} 已有后继包，只能基于链头创建")

    return _create_package(penalty, kind=kind, parent=parent, actor=actor, note=note, clock=clock)


def package_is_stale(package: EvidencePackage) -> bool:
    """
    活动包是否已“过时”：封存后又发生了补拍/整改/升级/更正。
    非活动包（已替代/校验失败）不算过时——它们本身就是历史或异常。
    """
    if package.status not in EvidencePackage.ACTIVE_STATUSES:
        return False
    manifest = package.manifest or {}
    penalty = package.penalty
    event = penalty.event

    latest = penalty.versions.order_by("-version_no").first()
    sealed_version_no = (manifest.get("penalty", {}).get("current_version") or {}).get("version_no")
    if latest is not None and sealed_version_no != latest.version_no:
        return True

    current_photo_ids = set(event.photos.values_list("id", flat=True))
    rectification = getattr(event, "rectification", None)
    if rectification is not None and rectification.photo_id:
        current_photo_ids.add(rectification.photo_id)
    manifest_photo_ids = {p["photo_id"] for p in manifest.get("photos", [])}
    if current_photo_ids != manifest_photo_ids:
        return True

    manifest_rect = manifest.get("rectification")
    if (rectification is None) != (manifest_rect is None):
        return True
    if rectification is not None and manifest_rect is not None:
        if _iso(rectification.submitted_at) != manifest_rect.get("submitted_at"):
            return True
    return False


# ---------------------------------------------------------------------------
# 校验（只动封存包自身）
# ---------------------------------------------------------------------------

@transaction.atomic
def verify_package(package: EvidencePackage, *, clock: Clock | None = None) -> dict:
    """
    在线校验：重算清单哈希 + 逐文件重算 SHA-256。
    结果只写回本包的 status / verify_report / verified_at——
    事件、处罚、候选判定一律不动（校验异常绝不反向污染业务链）。
    """
    clock = clock or SystemClock()
    now = clock.now()
    package = EvidencePackage.objects.select_for_update().get(pk=package.pk)

    checks: list[dict] = []
    manifest = package.manifest or {}

    actual_hash = manifest_hash_of(manifest)
    checks.append({
        "check": "manifest_hash",
        "ok": actual_hash == package.manifest_hash,
        "expected": package.manifest_hash,
        "actual": actual_hash,
    })

    files_total = 0
    files_ok = 0
    for photo in manifest.get("photos", []):
        file_info = photo.get("file") or {}
        path = file_info.get("path")
        if not path:
            continue
        files_total += 1
        entry = {
            "check": "file_sha256",
            "photo_id": photo.get("photo_id"),
            "path": path,
            "expected_sha256": file_info.get("sha256"),
        }
        if not file_info.get("readable", True):
            entry.update(ok=False, actual_sha256=None, error="unreadable_at_seal")
        else:
            try:
                with default_storage.open(path, "rb") as fh:
                    data = fh.read()
                digest = sha256_hex(data)
                ok = digest == file_info.get("sha256") and len(data) == file_info.get("size")
                entry.update(ok=ok, actual_sha256=digest)
                if not ok:
                    entry["error"] = "digest_mismatch"
            except OSError:
                entry.update(ok=False, actual_sha256=None, error="missing")
        checks.append(entry)
        if entry["ok"]:
            files_ok += 1

    ok = all(c["ok"] for c in checks)
    report = {
        "ok": ok,
        "package_no": package.package_no,
        "penalty_no": (manifest.get("penalty") or {}).get("penalty_no"),
        "event_no": (manifest.get("event") or {}).get("event_no"),
        "verified_at": _iso(now),
        "files_total": files_total,
        "files_ok": files_ok,
        "checks": checks,
    }

    if ok:
        # 校验恢复：失败过的包回到应有状态（有后继→已替代，否则→已封存）
        if package.status == EvidencePackage.Status.VERIFY_FAILED:
            package.status = (
                EvidencePackage.Status.SUPERSEDED
                if package.children.exists()
                else EvidencePackage.Status.SEALED
            )
    else:
        package.status = EvidencePackage.Status.VERIFY_FAILED
    package.verified_at = now
    package.verify_report = report
    package.save(update_fields=["status", "verified_at", "verify_report", "updated_at"])
    return report


# ---------------------------------------------------------------------------
# 导出 / 离线校验
# ---------------------------------------------------------------------------

def _zip_writestr(zf: zipfile.ZipFile, arcname: str, data: bytes) -> None:
    info = zipfile.ZipInfo(arcname, date_time=_ZIP_EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    zf.writestr(info, data)


def build_export_archive(package: EvidencePackage) -> bytes:
    """
    导出确定性 ZIP：

        <package_no>/manifest.json      不可变清单（规范化字节，哈希=manifest_hash）
        <package_no>/manifest.sha256    清单摘要（便于离线核对）
        <package_no>/files/<原始路径>    全部证据文件

    导出前逐文件重算摘要，缺失/损坏即抛 PackageExportFailed（409）——
    宁可拒绝导出，也不发出不完整或被污染的证据包。重试导出得到相同字节。
    """
    manifest = package.manifest or {}
    file_entries = []
    for photo in manifest.get("photos", []):
        file_info = photo.get("file") or {}
        path = file_info.get("path")
        if not path:
            continue
        try:
            with default_storage.open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            raise PackageExportFailed(f"证据文件缺失，无法导出完整封存包：{path}")
        if file_info.get("sha256") and sha256_hex(data) != file_info["sha256"]:
            raise PackageExportFailed(f"证据文件摘要与封存清单不符，已拒绝导出：{path}")
        file_entries.append((path, data))
    file_entries.sort(key=lambda item: item[0])

    buf = BytesIO()
    base = package.package_no
    with zipfile.ZipFile(buf, "w") as zf:
        manifest_bytes = canonical_manifest_bytes(manifest)
        _zip_writestr(zf, f"{base}/manifest.json", manifest_bytes)
        _zip_writestr(zf, f"{base}/manifest.sha256", f"{package.manifest_hash}  manifest.json\n".encode("ascii"))
        for path, data in file_entries:
            _zip_writestr(zf, f"{base}/files/{path}", data)
    return buf.getvalue()


def verify_export_archive(data: bytes) -> dict:
    """
    离线校验导出包（不读数据库、不写任何东西）：
    1. manifest.json 的字节级 SHA-256 必须等于 manifest.sha256 记录的摘要；
    2. 清单内每个证据文件的实际 SHA-256 / 大小必须与清单一致；
    3. 通过后即可从清单还原唯一处罚单号、事件编号与完整证据清单。
    """
    try:
        zf = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile:
        raise InvalidExportArchive("不是有效的封存包导出文件（ZIP 解析失败）")

    with zf:
        names = set(zf.namelist())
        # 结构：<package_no>/manifest.json（恰好一层目录）
        manifest_names = sorted(n for n in names if n.endswith("/manifest.json") and n.count("/") == 1)
        if len(manifest_names) != 1:
            raise InvalidExportArchive("导出包结构无效：未找到唯一的 manifest.json")
        manifest_name = manifest_names[0]
        base = manifest_name.rsplit("/", 1)[0]

        manifest_bytes = zf.read(manifest_name)
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise InvalidExportArchive("导出包结构无效：manifest.json 无法解析")

        checks: list[dict] = []
        computed_hash = sha256_hex(manifest_bytes)
        recorded_hash = None
        sha_name = f"{base}/manifest.sha256"
        if sha_name in names:
            recorded_hash = zf.read(sha_name).decode("ascii").split()[0]
        checks.append({
            "check": "manifest_hash",
            "ok": recorded_hash == computed_hash,
            "expected": recorded_hash,
            "actual": computed_hash,
        })

        files_total = 0
        files_ok = 0
        for photo in manifest.get("photos", []):
            file_info = photo.get("file") or {}
            path = file_info.get("path")
            if not path:
                continue
            files_total += 1
            entry = {
                "check": "file_sha256",
                "photo_id": photo.get("photo_id"),
                "path": path,
                "expected_sha256": file_info.get("sha256"),
            }
            arcname = f"{base}/files/{path}"
            if arcname not in names:
                entry.update(ok=False, actual_sha256=None, error="missing")
            else:
                payload = zf.read(arcname)
                digest = sha256_hex(payload)
                ok = digest == file_info.get("sha256") and len(payload) == file_info.get("size")
                entry.update(ok=ok, actual_sha256=digest)
                if not ok:
                    entry["error"] = "digest_mismatch"
            checks.append(entry)
            if entry["ok"]:
                files_ok += 1

    ok = all(c["ok"] for c in checks)
    return {
        "ok": ok,
        "package_no": manifest.get("package_no"),
        "kind": manifest.get("kind"),
        "parent_package_no": manifest.get("parent_package_no"),
        "penalty_no": (manifest.get("penalty") or {}).get("penalty_no"),
        "event_no": (manifest.get("event") or {}).get("event_no"),
        "contractor_name": (manifest.get("penalty") or {}).get("contractor_name"),
        "current_version_no": ((manifest.get("penalty") or {}).get("current_version") or {}).get("version_no"),
        "manifest_hash": computed_hash,
        "files_total": files_total,
        "files_ok": files_ok,
        "photos": [
            {
                "photo_id": p.get("photo_id"),
                "role": p.get("role"),
                "captured_at": p.get("captured_at"),
                "location": p.get("location"),
                "sha256": (p.get("file") or {}).get("sha256"),
            }
            for p in manifest.get("photos", [])
        ],
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# 旧数据迁移（回填）
# ---------------------------------------------------------------------------

@dataclass
class BackfillResult:
    created: list[EvidencePackage] = field(default_factory=list)
    skipped_existing: int = 0

    @property
    def created_count(self) -> int:
        return len(self.created)


def backfill_missing_packages(
    *,
    actor: str = "migration",
    clock: Clock | None = None,
) -> BackfillResult:
    """
    旧数据迁移：为尚无任何封存包的处罚单元补建原始封存包。
    幂等——已有封存链的处罚单元直接跳过，可反复执行
    （数据迁移 0004 与管理命令 seal_existing_packages 都走这里）。
    """
    result = BackfillResult()
    penalties = (
        PenaltyUnit.objects.filter(packages__isnull=True)
        .select_related("event", "contract", "locked_version")
        .order_by("id")
    )
    for penalty in penalties:
        package, created = seal_penalty(penalty, actor=actor, note="旧数据迁移补建封存包", clock=clock)
        if created:
            result.created.append(package)
        else:
            result.skipped_existing += 1
    return result
