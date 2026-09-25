"""
证据封存包服务。

核心不变量（监督复核场景）：
* 封存 = 对事件/处罚当前状态建立不可变清单(manifest)并固化全部文件摘要；
* 封存后新增补拍 / 整改 / 升级 / 更正**不改旧包**，只能形成显式关联的
  补充包(supplement)或替代包(replacement)，旧包进入 supplemented/superseded；
* 同一处罚(事件)任意时刻至多一个活动包（DB 部分唯一索引兜底 + 行锁串行化），
  重复封存 / 并发请求不会产生两个活动包：内容未变直接返回既有活动包；
* 文件摘要不符只能把包标记为 verification_failed 并落 SealVerification，
  绝不修改事件、处罚或自动合并任何候选。
"""
from dataclasses import dataclass

from django.db import IntegrityError, transaction

from assessment.exceptions import DomainError
from assessment.models import PenaltyUnit, ProblemEvent, SealPackage, SealedFile
from assessment.services.clock import Clock, SystemClock
from assessment.services.seal_manifest import digest_manifest
from assessment.services.seal_snapshot import build_content, fingerprint_content


class SealError(DomainError):
    status_code = 400
    default_detail = "封存规则校验失败"


def _resolve_subject(*, event: ProblemEvent | None, penalty: PenaltyUnit | None):
    """事件与处罚 1:1：两个入口最终都解析为同一对 (event, penalty)。"""
    if penalty is None and event is not None:
        penalty = getattr(event, "penalty", None)
    if penalty is None:
        raise SealError("该事件尚无处罚单元，无法封存")
    if event is None:
        event = penalty.event
    if event is None:
        raise SealError("该处罚没有关联事件，无法封存")
    if penalty.event_id != event.id:
        raise SealError("事件与处罚不匹配")
    return event, penalty


def _lineage_chain(package: SealPackage) -> list[str]:
    """从链首到当前包的 package_no 序列。"""
    chain = []
    cur = package
    seen = set()
    while cur is not None and cur.id not in seen:
        seen.add(cur.id)
        chain.append(cur.package_no)
        cur = cur.parent
    chain.reverse()
    return chain


@dataclass
class SealResult:
    package: SealPackage
    created: bool


@transaction.atomic
def create_seal_request(
    *,
    event: ProblemEvent | None = None,
    penalty: PenaltyUnit | None = None,
    subject_kind: str = SealPackage.Subject.PENALTY,
    kind: str = SealPackage.Kind.SUPPLEMENT,
    actor: str = "system",
    note: str = "",
    client_token: str | None = None,
    legacy_migrated: bool = False,
    clock: Clock | None = None,
) -> SealResult:
    """
    第一步：登记封存请求，生成 pending 包并立刻占用“活动包”唯一位。

    * 已存在活动包且内容指纹未变（重复/并发请求）→ 直接返回既有包，不新建；
    * 内容已变化（补拍/整改/升级/更正）→ 旧活动包转 supplemented/superseded，
      新建 pending 包，parent 显式指向旧包。
    """
    clock = clock or SystemClock()
    event, penalty = _resolve_subject(event=event, penalty=penalty)

    if kind not in SealPackage.Kind.values:
        raise SealError(f"未知封存包类型 {kind}")

    # 锁定处罚行：把同一处罚上的并发封存请求串行化
    locked = PenaltyUnit.objects.select_for_update().select_related("event").get(pk=penalty.pk)
    event = locked.event

    active = (
        SealPackage.objects.select_for_update()
        .filter(penalty=locked, is_active=True)
        .order_by("-id")
        .first()
    )

    if active is not None:
        # 幂等键：同一客户端重试，无论内容是否变化都返回同一个包
        if client_token:
            same_token = (
                SealPackage.objects.filter(penalty=locked, client_token=client_token)
                .order_by("-id")
                .first()
            )
            if same_token is not None:
                return SealResult(same_token, created=False)
        if active.status == SealPackage.Status.PENDING:
            # 上一封存尚未完成：并发/重复请求不允许再开第二个活动包
            return SealResult(active, created=False)

        content_now, _ = build_content(
            event, locked,
            package_kind=kind,
            parent_package_no=active.package_no,
        )
        fp_now = fingerprint_content(content_now)
        # 历史迁移包（content_fingerprint 可能留空）或指纹一致：重复封存，返回原包
        if not active.content_fingerprint or active.content_fingerprint == fp_now:
            return SealResult(active, created=False)

        # 内容已变化：旧包不可改，仅状态落位；新包显式挂接
        old_status = (
            SealPackage.Status.SUPERSEDED if kind == SealPackage.Kind.REPLACEMENT
            else SealPackage.Status.SUPPLEMENTED
        )
        active.is_active = False
        active.status = old_status
        active.save(update_fields=["is_active", "status", "updated_at"])
        parent = active
        replaces = active if kind == SealPackage.Kind.REPLACEMENT else None
    else:
        parent = None
        replaces = None
        kind = SealPackage.Kind.INITIAL  # 链上首包强制为 initial

    package = SealPackage(
        subject_kind=subject_kind,
        event=event,
        penalty=locked,
        package_kind=kind,
        status=SealPackage.Status.PENDING,
        is_active=True,
        parent=parent,
        replaces=replaces,
        relation_note=note[:512],
        client_token=client_token or "",
        sealed_by=actor,
        legacy_migrated=legacy_migrated,
    )
    try:
        package.save()
    except IntegrityError:
        # 极端竞态兜底：绝不允许两个活动包，回退到既有活动包
        existing = SealPackage.objects.get(penalty=locked, is_active=True)
        return SealResult(existing, created=False)
    return SealResult(package, created=True)


@transaction.atomic
def finalize_seal(package: SealPackage, *, clock: Clock | None = None) -> SealPackage:
    """
    第二步：为 pending 包计算文件摘要、固化清单与 SealedFile 行，翻转为 sealed。

    可安全重入：pending 包重算覆盖；非 pending 包直接返回（幂等）。
    """
    clock = clock or SystemClock()
    package = SealPackage.objects.select_for_update().get(pk=package.pk)
    if package.status != SealPackage.Status.PENDING:
        return package

    event = package.event
    penalty = package.penalty
    parent_no = package.parent.package_no if package.parent_id else None
    content, files_meta = build_content(
        event, penalty,
        package_kind=package.package_kind,
        parent_package_no=parent_no,
    )
    fingerprint = fingerprint_content(content)

    sealed_at = clock.now()
    manifest = {
        "schema": "sanitation-seal/1",
        "package_no": package.package_no,
        "subject_kind": package.subject_kind,
        "sealed_at": sealed_at.isoformat(),
        "sealed_by": package.sealed_by,
        "legacy_migrated": package.legacy_migrated,
        "lineage": {
            "chain": _lineage_chain(package),
            "parent_package_no": parent_no,
            "replaces_package_no": package.replaces.package_no if package.replaces_id else None,
        },
        "content": content,
    }
    _, digest = digest_manifest(manifest)

    # 清掉重算残留（重入场景），再按清单重建文件行
    package.files.all().delete()
    SealedFile.objects.bulk_create(
        [
            SealedFile(
                package=package,
                photo_id=f["photo_id"],
                role=f["role"],
                rel_path=f["rel_path"],
                storage_path=f["storage_path"],
                filename=f["filename"],
                content_type=f["content_type"],
                size_bytes=f["size_bytes"],
                sha256=f["sha256"],
                missing_at_seal=f["missing_at_seal"],
                phash=f["phash"],
                captured_at=f["captured_at"],
                lng=f["lng"],
                lat=f["lat"],
            )
            for f in files_meta
        ]
    )

    package.manifest = manifest
    package.manifest_digest = digest
    package.content_fingerprint = fingerprint
    package.sealed_at = sealed_at
    package.missing_files = any(f["missing_at_seal"] for f in files_meta)
    package.status = SealPackage.Status.SEALED
    package.save()
    return package


def seal_subject(**kwargs) -> SealResult:
    """登记 + 固化一步完成（API 正常封存入口）。"""
    result = create_seal_request(**kwargs)
    if result.created:
        finalize_seal(result.package)
        result.package.refresh_from_db()
    return result


def migrate_legacy_seals(
    *, limit: int | None = None, actor: str = "legacy-migration", batch_size: int = 200
) -> dict:
    """
    历史数据迁移：为尚无任何封存包的处罚建立 legacy 封存包。

    幂等可续跑：已经有包（任意状态）的处罚跳过；因此中断后重跑只会补齐缺口，
    不会产生第二个活动包。本函数不套外层事务——每个处罚独立提交，
    中途失败不回滚已完成的迁移。
    """
    qs = PenaltyUnit.objects.exclude(seal_packages__isnull=False).select_related("event")
    if limit is not None:
        qs = qs[:limit]
    migrated = 0
    skipped = 0
    for penalty in qs.iterator(chunk_size=batch_size):
        if SealPackage.objects.filter(penalty=penalty).exists():
            skipped += 1
            continue
        try:
            result = create_seal_request(
                penalty=penalty,
                subject_kind=SealPackage.Subject.PENALTY,
                actor=actor,
                note="历史数据迁移自动封存",
                legacy_migrated=True,
            )
            if result.created:
                finalize_seal(result.package)
                migrated += 1
            else:
                skipped += 1
        except Exception:
            # 单条失败不拖垮整批；下次重跑会重试该处罚
            skipped += 1
    return {"migrated": migrated, "skipped": skipped}
