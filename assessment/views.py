"""API 视图。所有写操作都委托给 services 层（领域规则集中、时钟可注入）。"""
from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers as drf_serializers
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from assessment.models import (
    CleaningContract,
    DuplicateCandidate,
    EscalationRecord,
    EvidencePackage,
    EvidencePhoto,
    PenaltyUnit,
    PenaltyVersion,
    ProblemEvent,
    Rectification,
    RoadGrid,
)
from assessment.serializers import (
    CandidateDecisionSerializer,
    CleaningContractSerializer,
    CreateEventFromPhotoSerializer,
    DuplicateCandidateSerializer,
    EscalationRecordSerializer,
    EscalationRunSerializer,
    EvidencePackageSerializer,
    EvidencePhotoSerializer,
    PenaltyCorrectionSerializer,
    PenaltyReviewSerializer,
    PenaltyUnitSerializer,
    PenaltyVersionSerializer,
    ProblemEventSerializer,
    RectificationReadSerializer,
    RectifyRequestSerializer,
    RoadGridSerializer,
    SealRequestSerializer,
    VerifyOfflineSerializer,
)
from assessment.services.clock import resolve_clock
from assessment.services.decisions import decide_candidate
from assessment.services.escalation import run_escalation
from assessment.services.events import create_event_from_photo
from assessment.services.penalties import correct_penalty, review_penalty
from assessment.services.rectification import submit_rectification
from assessment.services.sealing import (
    build_export_archive,
    create_successor_package,
    seal_penalty,
    verify_export_archive,
    verify_package,
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
    """问题事件：只读 + 整改回调 + 证据封存入口。"""

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

    @extend_schema(
        request=SealRequestSerializer,
        responses={200: EvidencePackageSerializer, 201: EvidencePackageSerializer},
    )
    @action(detail=True, methods=["post"])
    def seal(self, request, pk=None):
        """对事件对应的处罚单元建立证据封存包（与 /api/penalties/{id}/seal/ 等价）。"""
        event = self.get_object()
        penalty = getattr(event, "penalty", None)
        if penalty is None:
            return Response(
                {"detail": "事件尚无处罚单元，无法封存"},
                status=status.HTTP_409_CONFLICT,
            )
        return _seal_penalty_response(penalty, request)


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

    @extend_schema(
        request=SealRequestSerializer,
        responses={200: EvidencePackageSerializer, 201: EvidencePackageSerializer},
    )
    @action(detail=True, methods=["post"])
    def seal(self, request, pk=None):
        """
        证据封存：为该处罚单元建立首个封存包（不可变清单 + 文件摘要）。
        幂等——已存在封存链时返回链头（200），重复/并发请求不会产生第二个活动包。
        """
        return _seal_penalty_response(self.get_object(), request)


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


def _seal_penalty_response(penalty: PenaltyUnit, request) -> Response:
    """事件/处罚两个封存入口共用的实现。"""
    payload = SealRequestSerializer(data=request.data)
    payload.is_valid(raise_exception=True)
    data = payload.validated_data
    package, created = seal_penalty(
        penalty,
        actor=data.get("actor", "system"),
        note=data.get("note", ""),
        clock=resolve_clock(data.get("now")),
    )
    return Response(
        EvidencePackageSerializer(package).data,
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


# 校验报告（在线/离线结构一致）的 OpenAPI 描述
VerifyReportSchema = inline_serializer(
    name="VerifyReport",
    fields={
        "ok": drf_serializers.BooleanField(),
        "package_no": drf_serializers.CharField(allow_null=True),
        "penalty_no": drf_serializers.CharField(allow_null=True),
        "event_no": drf_serializers.CharField(allow_null=True),
        "manifest_hash": drf_serializers.CharField(required=False),
        "files_total": drf_serializers.IntegerField(),
        "files_ok": drf_serializers.IntegerField(),
        "checks": drf_serializers.ListField(child=drf_serializers.DictField()),
    },
)


class EvidencePackageViewSet(viewsets.mixins.RetrieveModelMixin,
                             viewsets.mixins.ListModelMixin,
                             viewsets.GenericViewSet):
    """
    证据封存包：只读检索 + 补充/替代/校验/导出/离线校验动作。
    不提供修改、删除（405）；封存后清单不可变，后续变化只能派生新包。
    """

    queryset = EvidencePackage.objects.select_related(
        "penalty", "parent", "sealed_version",
    ).prefetch_related("children").all()
    serializer_class = EvidencePackageSerializer
    filterset_fields = ["penalty", "status", "kind"]

    @extend_schema(request=SealRequestSerializer, responses={201: EvidencePackageSerializer})
    @action(detail=True, methods=["post"])
    def supplement(self, request, pk=None):
        """基于当前活动包生成补充包（补拍/整改/升级/更正后），旧包置为已补充。"""
        return self._successor(request, kind=EvidencePackage.Kind.SUPPLEMENT)

    @extend_schema(request=SealRequestSerializer, responses={201: EvidencePackageSerializer})
    @action(detail=True, methods=["post"])
    def replace(self, request, pk=None):
        """基于当前活动包生成替代包（完整重封当前状态），旧包置为已替代。"""
        return self._successor(request, kind=EvidencePackage.Kind.REPLACEMENT)

    def _successor(self, request, *, kind: str) -> Response:
        parent = self.get_object()
        payload = SealRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        package = create_successor_package(
            parent,
            kind=kind,
            actor=data.get("actor", "system"),
            note=data.get("note", ""),
            clock=resolve_clock(data.get("now")),
        )
        return Response(EvidencePackageSerializer(package).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses={200: VerifyReportSchema})
    @action(detail=True, methods=["post"])
    def verify(self, request, pk=None):
        """
        在线校验：重算清单哈希与全部文件摘要。
        结果只写回封存包自身状态（不符→校验失败），不影响事件/处罚/候选。
        """
        report = verify_package(self.get_object())
        return Response(report, status=status.HTTP_200_OK)

    @extend_schema(
        responses={(200, "application/zip"): OpenApiTypes.BINARY},
    )
    @action(detail=True, methods=["get"])
    def export(self, request, pk=None):
        """
        导出封存包 ZIP（manifest.json + manifest.sha256 + 全部证据文件）。
        确定性字节，中断后重试得到相同内容；文件缺失/摘要不符时拒绝导出（409）。
        """
        package = self.get_object()
        archive = build_export_archive(package)
        response = HttpResponse(archive, content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="{package.package_no}.zip"'
        return response

    @extend_schema(request=VerifyOfflineSerializer, responses={200: VerifyReportSchema})
    @action(detail=False, methods=["post"], url_path="verify_offline")
    def verify_offline(self, request):
        """
        离线校验：上传导出的 ZIP，不读数据库，仅凭包内清单与文件校验，
        并还原唯一处罚单号、事件编号与完整证据清单。
        """
        payload = VerifyOfflineSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        archive = payload.validated_data["archive"]
        report = verify_export_archive(archive.read())
        return Response(report, status=status.HTTP_200_OK)
