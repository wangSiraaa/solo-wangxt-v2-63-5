"""API 视图。所有写操作都委托给 services 层（领域规则集中、时钟可注入）。"""
from django.http import HttpResponse
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from assessment.models import (
    CleaningContract,
    DuplicateCandidate,
    EscalationRecord,
    EvidencePhoto,
    PenaltyUnit,
    PenaltyVersion,
    ProblemEvent,
    Rectification,
    RoadGrid,
    SealExportJob,
    SealPackage,
    SealVerification,
)
from assessment.serializers import (
    CandidateDecisionSerializer,
    CleaningContractSerializer,
    CreateEventFromPhotoSerializer,
    DuplicateCandidateSerializer,
    EscalationRecordSerializer,
    EscalationRunSerializer,
    EvidencePhotoSerializer,
    PenaltyCorrectionSerializer,
    PenaltyReviewSerializer,
    PenaltyUnitSerializer,
    PenaltyVersionSerializer,
    ProblemEventSerializer,
    RectificationReadSerializer,
    RectifyRequestSerializer,
    RoadGridSerializer,
    SealCreateSerializer,
    SealExportCreateSerializer,
    SealExportJobSerializer,
    SealExportRunSerializer,
    SealFinalizeSerializer,
    SealMigrateSerializer,
    SealOfflineVerifySerializer,
    SealPackageBriefSerializer,
    SealPackageSerializer,
    SealVerifySerializer,
)
from assessment.services.clock import resolve_clock
from assessment.services.decisions import decide_candidate
from assessment.services.escalation import run_escalation
from assessment.services.events import create_event_from_photo
from assessment.services.penalties import correct_penalty, review_penalty
from assessment.services.rectification import submit_rectification
from assessment.services.seal_export import (
    create_export_job,
    read_bundle_bytes,
    run_export_job,
)
from assessment.services.seal_verify import verify_active_package, verify_bundle_archive
from assessment.services.sealing import (
    finalize_seal,
    migrate_legacy_seals,
    seal_subject,
)


class RoadGridViewSet(viewsets.ModelViewSet):
    """道路网格（GeoJSON Feature 输入/输出）。"""

    queryset = RoadGrid.objects.all()
    serializer_class = RoadGridSerializer


class CleaningContractViewSet(viewsets.ModelViewSet):
    """保洁合同责任区间。"""

    queryset = CleaningContract.objects.select_related("grid").all()
    serializer_class = CleaningContractSerializer
    filterset_fields = ["grid", "contractor_name"]


class EvidencePhotoViewSet(viewsets.mixins.CreateModelMixin,
                          viewsets.mixins.RetrieveModelMixin,
                          viewsets.mixins.ListModelMixin,
                          viewsets.GenericViewSet):
    """
    证据照片：仅允许上传 / 查询，不允许修改删除（证据保全）。
    上传成功后响应中可查看自动生成的 pHash 与疑似候选（候选在 /candidates/）。
    """

    queryset = EvidencePhoto.objects.all()
    serializer_class = EvidencePhotoSerializer

    @action(detail=True, methods=["post"])
    def create_event(self, request, pk=None):
        """对一张尚未立案的照片直接创建事件（无候选时的入口）。"""
        photo = self.get_object()
        payload = CreateEventFromPhotoSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        event = create_event_from_photo(
            photo,
            category=data.get("category", ProblemEvent.Category.OTHER),
            description=data.get("description", ""),
            actor=data.get("actor", "system"),
            dedup_key=data.get("dedup_key"),
            occurred_at=data.get("occurred_at"),
        )
        return Response(ProblemEventSerializer(event).data, status=status.HTTP_201_CREATED)


class DuplicateCandidateViewSet(viewsets.mixins.RetrieveModelMixin,
                                viewsets.mixins.ListModelMixin,
                                viewsets.GenericViewSet):
    """疑似重复候选：只读列表 + 人工判定动作。"""

    queryset = DuplicateCandidate.objects.select_related("photo", "matched_photo").all()
    serializer_class = DuplicateCandidateSerializer
    filterset_fields = ["status", "photo", "matched_photo"]

    @action(detail=True, methods=["post"])
    def decide(self, request, pk=None):
        """
        人工判定：
        attach=同一问题不同角度（挂接、不重复扣分）；
        create_new=另立新事件（已整改事件复发/不同地点）；
        different=标记不同不合并。
        """
        candidate = self.get_object()
        payload = CandidateDecisionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        decided = decide_candidate(
            candidate,
            action=data["action"],
            actor=data.get("actor", "system"),
            category=data.get("category"),
            description=data.get("description", ""),
            dedup_key=data.get("dedup_key"),
            note=data.get("note", ""),
        )
        return Response(DuplicateCandidateSerializer(decided).data)


class ProblemEventViewSet(viewsets.mixins.RetrieveModelMixin,
                          viewsets.mixins.ListModelMixin,
                          viewsets.GenericViewSet):
    """问题事件：只读 + 整改回调。"""

    queryset = ProblemEvent.objects.select_related(
        "grid", "contract", "penalty", "rectification", "primary_photo",
    ).prefetch_related("photos").all()
    serializer_class = ProblemEventSerializer
    filterset_fields = ["status", "category", "grid", "contract", "contractor_name"]

    @action(detail=True, methods=["post"])
    def rectify(self, request, pk=None):
        """
        整改回调。重复回调返回 409（幂等忽略，不改变扣分）。
        可传 now 注入整改时间（测试/补录场景）。
        """
        event = self.get_object()
        payload = RectifyRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        clock = resolve_clock(data.get("now"))
        rectification = submit_rectification(
            event,
            note=data.get("note", ""),
            actor=data.get("actor", "system"),
            photo=data.get("photo_id"),
            clock=clock,
        )
        event.refresh_from_db()
        return Response(
            {
                "rectification": RectificationReadSerializer(rectification).data,
                "event": ProblemEventSerializer(event).data,
            },
            status=status.HTTP_201_CREATED,
        )


class RectificationViewSet(viewsets.mixins.RetrieveModelMixin,
                           viewsets.mixins.ListModelMixin,
                           viewsets.GenericViewSet):
    queryset = Rectification.objects.select_related("event", "photo").all()
    serializer_class = RectificationReadSerializer
    filterset_fields = ["event"]


class PenaltyUnitViewSet(viewsets.mixins.RetrieveModelMixin,
                         viewsets.mixins.ListModelMixin,
                         viewsets.GenericViewSet):
    """
    处罚单元：只读检索（含完整版本链/证据/升级/复核记录，支撑扣分追溯），
    并提供“人工更正（追加版本）”与“复核（锁定版本）”两个动作。
    """

    queryset = PenaltyUnit.objects.select_related(
        "event", "contract", "locked_version",
    ).prefetch_related("versions", "escalations", "reviews").all()
    serializer_class = PenaltyUnitSerializer
    filterset_fields = ["status", "contractor_name", "contract", "event"]

    @action(detail=True, methods=["post"])
    def correct(self, request, pk=None):
        """人工更正：追加一个 correction 版本，历史版本不动。"""
        penalty = self.get_object()
        payload = PenaltyCorrectionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        version = correct_penalty(
            penalty,
            points=data["points"],
            reason=data["reason"],
            actor=data.get("actor", "system"),
        )
        penalty.refresh_from_db()
        return Response(PenaltyUnitSerializer(penalty).data, status=status.HTTP_201_CREATED,
                        headers={"Version-No": str(version.version_no)})

    @action(detail=True, methods=["post"])
    def review(self, request, pk=None):
        """复核：approved=true 时锁定当前最新版本。"""
        penalty = self.get_object()
        payload = PenaltyReviewSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        review_penalty(
            penalty,
            approved=data["approved"],
            actor=data.get("actor", "reviewer"),
            comment=data.get("comment", ""),
        )
        penalty.refresh_from_db()
        return Response(PenaltyUnitSerializer(penalty).data)


class PenaltyVersionViewSet(viewsets.mixins.RetrieveModelMixin,
                            viewsets.mixins.ListModelMixin,
                            viewsets.GenericViewSet):
    """处罚版本（只读——版本只追加、不可改）。写方法一律 405。"""

    queryset = PenaltyVersion.objects.all()
    serializer_class = PenaltyVersionSerializer
    filterset_fields = ["penalty", "kind"]


class EscalationRecordViewSet(viewsets.mixins.RetrieveModelMixin,
                              viewsets.mixins.ListModelMixin,
                              viewsets.GenericViewSet):
    queryset = EscalationRecord.objects.select_related("penalty", "version").all()
    serializer_class = EscalationRecordSerializer
    filterset_fields = ["penalty", "level"]

    @action(detail=False, methods=["post"])
    def run(self, request):
        """
        执行一次逾期升级扫描。
        body 可传 {"now": "2026-09-25T10:00:00+08:00"} 注入时钟，
        不传则使用服务器当前时间。重复执行幂等。
        """
        payload = EscalationRunSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        result = run_escalation(
            clock=resolve_clock(data.get("now")),
            default_sla_hours=data.get("default_sla_hours"),
            actor=data.get("actor", "escalation-job"),
        )
        return Response(
            {
                "inspected_open_events": result.inspected,
                "overdue_open_events": result.open_overdue,
                "created_count": result.created_count,
                "created": EscalationRecordSerializer(result.created, many=True).data,
            },
            status=status.HTTP_201_CREATED if result.created_count else status.HTTP_200_OK,
        )


class SealPackageViewSet(viewsets.mixins.CreateModelMixin,
                         viewsets.mixins.RetrieveModelMixin,
                         viewsets.mixins.ListModelMixin,
                         viewsets.GenericViewSet):
    """
    证据封存包：只读检索 + 封存/补充/替代/校验/导出/离线校验动作。

    不变量：
    * 封存后新增补拍/整改/升级/更正不改旧包，只产生显式关联的补充包或替代包；
    * 重复封存/并发请求至多一个活动包（内容未变返回原包）；
    * 文件摘要不符只标记 verification_failed 并留痕，不改业务链、不合并候选。
    """

    queryset = (
        SealPackage.objects.select_related("event", "penalty", "parent", "replaces")
        .prefetch_related("files", "verifications", "children")
        .all()
    )
    serializer_class = SealPackageSerializer
    filterset_fields = ["penalty", "event", "status", "is_active", "package_kind", "legacy_migrated"]

    def create(self, request, *args, **kwargs):
        """
        封存（或在内容已变化时建立补充/替代包）。

        传 event 或 penalty 之一；client_token 用于重复/并发请求幂等。
        内容未变的重复封存返回 200 + 既有活动包；新封存返回 201。
        """
        payload = SealCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        if not data.get("event") and not data.get("penalty"):
            from rest_framework import serializers

            raise serializers.ValidationError({"event": "event 与 penalty 至少提供一个"})

        if data.get("finalize", True):
            result = seal_subject(
                event=data.get("event"),
                penalty=data.get("penalty"),
                subject_kind=data.get("subject_kind", SealPackage.Subject.PENALTY),
                kind=data.get("kind", SealPackage.Kind.SUPPLEMENT),
                actor=data.get("actor", "system"),
                note=data.get("note", ""),
                client_token=data.get("client_token"),
            )
            http = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
            return Response(SealPackageSerializer(result.package).data, status=http,
                            headers={"Seal-Created": "1" if result.created else "0"})

        from assessment.services.sealing import create_seal_request

        result = create_seal_request(
            event=data.get("event"),
            penalty=data.get("penalty"),
            subject_kind=data.get("subject_kind", SealPackage.Subject.PENALTY),
            kind=data.get("kind", SealPackage.Kind.SUPPLEMENT),
            actor=data.get("actor", "system"),
            note=data.get("note", ""),
            client_token=data.get("client_token"),
        )
        http = status.HTTP_202_ACCEPTED if result.created else status.HTTP_200_OK
        return Response(SealPackageSerializer(result.package).data, status=http)

    @action(detail=True, methods=["post"])
    def finalize(self, request, pk=None):
        """完成待封存(pending)包：计算文件摘要、固化不可变清单。"""
        payload = SealFinalizeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        package = finalize_seal(self.get_object())
        return Response(SealPackageSerializer(package).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="supplement")
    def supplement(self, request, pk=None):
        """
        建立补充包：封存后新增补拍/整改/升级/更正后调用。
        旧包转 supplemented（不改内容），新包 parent 指向旧包。
        """
        return self._create_related(request, SealPackage.Kind.SUPPLEMENT)

    @action(detail=True, methods=["post"], url_path="replace")
    def replace(self, request, pk=None):
        """
        建立替代包（显式更正封存口径）。旧包转 superseded（不改内容），
        新包 parent/replaces 指向旧包。
        """
        return self._create_related(request, SealPackage.Kind.REPLACEMENT)

    def _create_related(self, request, kind: str):
        package = self.get_object()
        actor = request.data.get("actor", "system") if isinstance(request.data, dict) else "system"
        note = request.data.get("note", "") if isinstance(request.data, dict) else ""
        client_token = request.data.get("client_token", "") if isinstance(request.data, dict) else ""
        result = seal_subject(
            penalty=package.penalty,
            subject_kind=package.subject_kind,
            kind=kind,
            actor=actor,
            note=note,
            client_token=client_token or None,
        )
        http = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
        return Response(SealPackageSerializer(result.package).data, status=http)

    @action(detail=True, methods=["post"])
    def verify(self, request, pk=None):
        """
        在线校验：复算清单与全部证据文件摘要。

        失败只把活动包标记 verification_failed 并记录；绝不修改事件/处罚/候选。
        """
        payload = SealVerifySerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        package, record, report = verify_active_package(
            self.get_object(),
            source=data.get("source", SealVerification.Source.ONLINE),
            actor=data.get("actor", "system"),
        )
        return Response(
            {
                "package": SealPackageSerializer(package).data,
                "verification_id": record.id,
                "report": report.as_dict(),
            },
            status=status.HTTP_200_OK
            if report.result == SealVerification.Result.VALID
            else status.HTTP_409_CONFLICT,
        )

    @action(detail=True, methods=["post"], url_path="exports")
    def exports(self, request, pk=None):
        """
        创建并（默认）推进导出任务，产出可离线校验的 tar 包。
        client_token 幂等：中断后同键重试只续作、产出同一个 bundle。
        """
        package = self.get_object()
        payload = SealExportCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        job = create_export_job(
            package,
            client_token=data["client_token"],
            actor=data.get("actor", "system"),
        )
        interrupted = False
        if data.get("run", True) and job.status != SealExportJob.Status.COMPLETED:
            try:
                job = run_export_job(job.id, fail_after=data.get("fail_after"))
            except Exception:
                # fail_after 模拟中断属于预期分支：返回 building 任务供续作
                job = SealExportJob.objects.get(pk=job.id)
                if data.get("fail_after"):
                    interrupted = True
                else:
                    raise
        http = status.HTTP_201_CREATED if not interrupted else status.HTTP_202_ACCEPTED
        return Response(SealExportJobSerializer(job).data, status=http)

    @action(detail=True, methods=["get"], url_path=r"exports/(?P<job_id>[0-9]+)/download")
    def export_download(self, request, pk=None, job_id=None):
        """下载已完成的离线封存 tar 包。"""
        package = self.get_object()
        job = SealExportJob.objects.get(pk=job_id, package=package)
        data = read_bundle_bytes(job)
        resp = HttpResponse(data, content_type="application/x-tar")
        resp["Content-Disposition"] = f'attachment; filename="{package.package_no}.tar"'
        resp["Content-Length"] = str(len(data))
        resp["X-Bundle-SHA256"] = job.bundle_digest
        return resp

    @action(detail=True, methods=["get"])
    def lineage(self, request, pk=None):
        """追溯详情：沿 parent 链给出首包到当前活动包的完整封存谱系。"""
        package = self.get_object()
        chain = []
        cur = package
        seen = set()
        while cur is not None and cur.id not in seen:
            seen.add(cur.id)
            chain.append(
                {
                    "package_no": cur.package_no,
                    "package_kind": cur.package_kind,
                    "status": cur.status,
                    "is_active": cur.is_active,
                    "manifest_digest": cur.manifest_digest,
                    "sealed_at": cur.sealed_at,
                    "sealed_by": cur.sealed_by,
                    "legacy_migrated": cur.legacy_migrated,
                    "relation_note": cur.relation_note,
                }
            )
            cur = cur.parent
        chain.reverse()
        children = list(package.children.order_by("id")) if package.is_active else []
        return Response(
            {
                "current": SealPackageBriefSerializer(package).data,
                "lineage": chain,
                "newer_packages": [
                    {
                        "package_no": c.package_no,
                        "package_kind": c.package_kind,
                        "status": c.status,
                    }
                    for c in children
                ],
            }
        )

    @action(detail=False, methods=["post"], url_path="offline-verify",
            parser_classes=[MultiPartParser, FormParser])
    def offline_verify(self, request):
        """
        离线校验：上传导出的 .tar 封存包，在服务端复算全部摘要（算法与
        verify_offline.py 完全一致），并还原唯一处罚与完整证据视图。
        该动作不读取/修改任何业务数据。
        """
        payload = SealOfflineVerifySerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        upload = payload.validated_data["bundle"]
        tar_bytes = upload.read()
        report, restored = verify_bundle_archive(tar_bytes)
        return Response(
            {
                "report": report.as_dict(),
                "restored": restored,
                "actor": payload.validated_data.get("actor", "offline-checker"),
            },
            status=status.HTTP_200_OK
            if report.result == SealVerification.Result.VALID
            else status.HTTP_409_CONFLICT,
        )

    @action(detail=False, methods=["post"])
    def migrate(self, request):
        """
        历史数据迁移：为尚无封存包的处罚批量补建 legacy 封存包。
        幂等可续跑，中断重跑只补缺口，绝不产生第二个活动包。
        """
        payload = SealMigrateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        result = migrate_legacy_seals(
            limit=data.get("limit"), actor=data.get("actor", "legacy-migration"),
        )
        return Response(result, status=status.HTTP_200_OK)


class SealExportJobViewSet(viewsets.mixins.RetrieveModelMixin,
                          viewsets.mixins.ListModelMixin,
                          viewsets.GenericViewSet):
    """导出任务：查询进度、续作（中断重试）。"""

    queryset = SealExportJob.objects.select_related("package").all()
    serializer_class = SealExportJobSerializer
    filterset_fields = ["package", "status", "client_token"]

    @action(detail=True, methods=["post"], url_path="run")
    def run(self, request, pk=None):
        """续作导出（中断后用同一任务即可，无需新建）。"""
        payload = SealExportRunSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        job = self.get_object()
        try:
            job = run_export_job(job.id, fail_after=payload.validated_data.get("fail_after"))
        except Exception:
            if payload.validated_data.get("fail_after"):
                job = SealExportJob.objects.get(pk=job.id)
                return Response(SealExportJobSerializer(job).data, status=status.HTTP_202_ACCEPTED)
            raise
        return Response(SealExportJobSerializer(job).data)
