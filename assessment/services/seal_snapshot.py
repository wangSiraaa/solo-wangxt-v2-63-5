"""
封存快照装配：把事件/处罚在某一时刻的全部关联证据与判定组装成
与数据库行结构无关的普通 dict（供封存清单与离线还原共用）。

刻意只依赖模型传入的对象，不在这里开启事务、不做写操作。
"""
import json
import os
from decimal import Decimal

from django.conf import settings

from assessment.models import (
    DuplicateCandidate,
    Rectification,
)
from assessment.services.penalties import BASE_POINTS, ESCALATION_STEP, MAX_ESCALATION_LEVEL


def _dt(value):
    return value.isoformat() if value is not None else None


def _num(value):
    """Decimal 统一转字符串，保证 canonical JSON 跨进程一致。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _point(point):
    if point is None:
        return None
    return {"lng": float(point.x), "lat": float(point.y), "srid": point.srid}


def _geom_json(geom):
    if geom is None:
        return None
    return json.loads(geom.geojson)


def photo_rel_path(photo) -> str:
    """包内相对路径：以照片主键为前缀，天然唯一且不受存储目录影响。"""
    basename = os.path.basename(photo.image.name) or f"photo-{photo.id}.bin"
    return f"files/photos/{photo.id}-{basename}"


def hash_photo_file(photo) -> tuple[str, int | None, bool]:
    """
    计算证据文件摘要。

    文件缺失（存储中找不到）不抛异常：封存必须可在“文件已丢失”的情况下
    继续完成，缺失事实由 (空摘要, missing=True) 显式入清单。
    """
    try:
        exists = photo.image.storage.exists(photo.image.name)
    except Exception:
        exists = False
    if not exists or not photo.image.name:
        return "", None, True
    try:
        with photo.image.storage.open(photo.image.name, "rb") as fh:
            import hashlib

            h = hashlib.sha256()
            size = 0
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
                size += len(chunk)
        return h.hexdigest(), size, False
    except FileNotFoundError:
        return "", None, True


def collect_photos(event) -> list[tuple[object, str]]:
    """
    返回 (photo, role) 列表：事件全部关联照片 + 整改照片。

    整改照片若是一张独立上传（未挂到事件）也必须入包——它是整改证据。
    顺序按主键，保证清单确定性。
    """
    tagged: dict[int, tuple[object, str]] = {}
    for photo in sorted(event.photos.all(), key=lambda p: p.id):
        tagged[photo.id] = (photo, "evidence")

    rectification = Rectification.objects.filter(event=event).select_related("photo").first()
    if rectification is not None and rectification.photo_id is not None:
        photo = rectification.photo
        tagged[photo.id] = (photo, "rectification")

    return [tagged[k] for k in sorted(tagged)]


def collect_candidates(event, photo_ids: set[int]) -> list[dict]:
    """
    候选判定快照。

    只收录**双方照片都属于本事件**的候选——跨地点同图候选（一张在本事件、
    另一张在远处事件）绝不混入本封存包；那条判定属于对方事件的包。
    状态不过滤：pending 候选同样是“候选判定”的一部分。
    """
    qs = DuplicateCandidate.objects.filter(photo_id__in=photo_ids, matched_photo_id__in=photo_ids)
    out = []
    for cand in qs.select_related("photo", "matched_photo").order_by("id"):
        out.append(
            {
                "id": cand.id,
                "photo_id": cand.photo_id,
                "matched_photo_id": cand.matched_photo_id,
                "hamming_distance": cand.hamming_distance,
                "status": cand.status,
                "status_display": cand.get_status_display(),
                "decision_note": cand.decision_note,
                "decided_by": cand.decided_by,
                "decided_at": _dt(cand.decided_at),
                "photo": _photo_meta(cand.photo),
                "matched_photo": _photo_meta(cand.matched_photo),
            }
        )
    return out


def _photo_meta(photo) -> dict:
    return {
        "id": photo.id,
        "phash": photo.phash,
        "captured_at": _dt(photo.captured_at),
        "location": _point(photo.location),
        "event_id": photo.event_id,
        "uploader": photo.uploader,
        "note": photo.note,
    }


def build_content(event, penalty, *, package_kind: str, parent_package_no: str | None) -> dict:
    """
    组装封存内容（不含 sealed_at/digest——这些在封存提交时才定稿）。

    同时返回：
    files_meta: [{photo, role, rel_path, storage_path, filename, content_type,
                  sha256, size_bytes, missing, phash, captured_at, lng, lat}]
    供 SealedFile 行与导出复用，避免二次读盘摘要。
    """
    grid = event.grid
    contract = event.contract or penalty.contract

    tagged = collect_photos(event)
    photo_ids = {p.id for p, _ in tagged}

    files_meta = []
    photo_entries = []
    for photo, role in tagged:
        digest, size, missing = hash_photo_file(photo)
        rel_path = photo_rel_path(photo)
        filename = os.path.basename(photo.image.name) if photo.image.name else f"photo-{photo.id}"
        files_meta.append(
            {
                "photo_id": photo.id,
                "role": role,
                "rel_path": rel_path,
                "storage_path": photo.image.name,
                "filename": filename,
                "content_type": "image/png",
                "size_bytes": size,
                "sha256": digest,
                "missing_at_seal": missing,
                "phash": photo.phash,
                "captured_at": photo.captured_at,
                "lng": photo.location.x if photo.location else None,
                "lat": photo.location.y if photo.location else None,
            }
        )
        entry = _photo_meta(photo)
        entry.update(
            {
                "role": role,
                "rel_path": rel_path,
                "storage_path": photo.image.name,
                "filename": filename,
                "size_bytes": size,
                "sha256": digest,
                "missing_at_seal": missing,
                "created_at": _dt(photo.created_at),
            }
        )
        photo_entries.append(entry)

    rectification = Rectification.objects.filter(event=event).select_related("photo").first()
    rect_snapshot = None
    if rectification is not None:
        rect_snapshot = {
            "id": rectification.id,
            "photo_id": rectification.photo_id,
            "note": rectification.note,
            "submitted_by": rectification.submitted_by,
            "submitted_at": _dt(rectification.submitted_at),
        }

    versions = [
        {
            "id": v.id,
            "version_no": v.version_no,
            "points": _num(v.points),
            "escalation_level": v.escalation_level,
            "kind": v.kind,
            "kind_display": v.get_kind_display(),
            "reason": v.reason,
            "actor": v.actor,
            "created_at": _dt(v.created_at),
        }
        for v in penalty.versions.order_by("version_no")
    ]
    escalations = [
        {
            "level": r.level,
            "version_id": r.version_id,
            "reason": r.reason,
            "actor": r.actor,
            "ran_at": _dt(r.ran_at),
        }
        for r in penalty.escalations.order_by("level")
    ]
    reviews = [
        {
            "version_id": r.version_id,
            "approved": r.approved,
            "comment": r.comment,
            "reviewer": r.reviewer,
            "reviewed_at": _dt(r.reviewed_at),
        }
        for r in penalty.reviews.order_by("reviewed_at")
    ]

    locked_no = penalty.locked_version.version_no if penalty.locked_version_id else None

    content = {
        "schema": "sanitation-seal/1",
        "subject": {
            "event_id": event.id,
            "event_no": event.event_no,
            "penalty_id": penalty.id,
            "penalty_no": penalty.penalty_no,
        },
        "package": {"kind": package_kind, "parent_package_no": parent_package_no},
        "event": {
            "id": event.id,
            "event_no": event.event_no,
            "category": event.category,
            "category_display": event.get_category_display(),
            "description": event.description,
            "status": event.status,
            "location": _point(event.location),
            "occurred_at": _dt(event.occurred_at),
            "sla_hours": event.sla_hours,
            "primary_photo_id": event.primary_photo_id,
            "contractor_name": event.contractor_name,
            "created_at": _dt(event.created_at),
        },
        "grid": (
            {
                "id": grid.id,
                "code": grid.code,
                "name": grid.name,
                "geom": _geom_json(grid.geom),
            }
            if grid else None
        ),
        "contract": (
            {
                "id": contract.id,
                "code": contract.code,
                "contractor_name": contract.contractor_name,
                "valid_from": _dt(contract.valid_from),
                "valid_to": _dt(contract.valid_to),
            }
            if contract else None
        ),
        "photos": photo_entries,
        "candidates": collect_candidates(event, photo_ids),
        "rectification": rect_snapshot,
        "penalty": {
            "id": penalty.id,
            "penalty_no": penalty.penalty_no,
            "points": _num(penalty.points),
            "escalation_level": penalty.escalation_level,
            "status": penalty.status,
            "locked_version_id": penalty.locked_version_id,
            "locked_version_no": locked_no,
            "contractor_name": penalty.contractor_name,
            "created_at": _dt(penalty.created_at),
        },
        "versions": versions,
        "escalations": escalations,
        "reviews": reviews,
        # 处罚依据快照：基础扣分表 / 升级步长 / 等级上限 / 阈值 / SLA
        "policy": {
            "base_points": {k: _num(v) for k, v in BASE_POINTS.items()},
            "escalation_step": _num(ESCALATION_STEP),
            "max_escalation_level": MAX_ESCALATION_LEVEL,
            "phash_threshold": int(settings.ASSESSMENT["PHASH_THRESHOLD"]),
            "default_sla_hours": int(settings.ASSESSMENT["DEFAULT_SLA_HOURS"]),
        },
        "files": [
            {
                "rel_path": f["rel_path"],
                "role": f["role"],
                "photo_id": f["photo_id"],
                "filename": f["filename"],
                "content_type": f["content_type"],
                "size_bytes": f["size_bytes"],
                "sha256": f["sha256"],
                "missing_at_seal": f["missing_at_seal"],
                "phash": f["phash"],
                "captured_at": _dt(f["captured_at"]),
                "location": {"lng": f["lng"], "lat": f["lat"]} if f["lng"] is not None else None,
            }
            for f in files_meta
        ],
    }
    return content, files_meta


def fingerprint_content(content: dict) -> str:
    """
    结构指纹：不含文件摘要/大小、不含封存时间、不含包类型与父链，
    仅描述“封存了什么业务内容”。

    用于重复封存判定——补拍/整改/升级/更正都会改变指纹；
    单纯文件被篡改不改变指纹（那是 verify 的职责）；
    以 supplement 还是 replacement 方式封存也不改变指纹（那只是包间关系）。
    """
    from copy import deepcopy

    from assessment.services.seal_manifest import digest_manifest

    view = deepcopy(content)
    view.pop("package", None)
    for entry in view.get("files", []):
        entry.pop("sha256", None)
        entry.pop("size_bytes", None)
        entry.pop("missing_at_seal", None)
    for entry in view.get("photos", []):
        entry.pop("sha256", None)
        entry.pop("size_bytes", None)
        entry.pop("missing_at_seal", None)
    _, digest = digest_manifest({"fingerprint_of": view})
    return digest
