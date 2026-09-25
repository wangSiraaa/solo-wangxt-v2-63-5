"""DRF 序列化器。"""
from django.contrib.gis.geos import Point
from django.db import models
from rest_framework import serializers
from rest_framework_gis.fields import GeometryField
from drf_spectacular.utils import extend_schema_field

from assessment.models import (
    CleaningContract,
    DuplicateCandidate,
    EscalationRecord,
    EvidencePhoto,
    PenaltyUnit,
    PenaltyVersion,
    ProblemEvent,
    Rectification,
    ReviewRecord,
    RoadGrid,
    SealExportJob,
    SealPackage,
    SealVerification,
    SealedFile,
)
from assessment.services.duplicates import generate_candidates_for_photo
from assessment.services.phash import compute_phash_hex


class RoadGridSerializer(serializers.ModelSerializer):
    """道路网格；geom 为 GeoJSON Polygon（EPSG:4326）。"""

    geom = GeometryField()

    class Meta:
        model = RoadGrid
        fields = ["id", "code", "name", "geom"]


class CleaningContractSerializer(serializers.ModelSerializer):
    class Meta:
        model = CleaningContract
        fields = ["id", "code", "grid", "contractor_name", "valid_from", "valid_to", "created_at"]
        read_only_fields = ["created_at"]


class EvidencePhotoSerializer(serializers.ModelSerializer):
    # 上传时只给经纬度，服务端构造 Point；location 以 GeoJSON 只读返回
    lat = serializers.FloatField(write_only=True, min_value=-90, max_value=90)
    lng = serializers.FloatField(write_only=True, min_value=-180, max_value=180)
    location = GeometryField(read_only=True)

    class Meta:
        model = EvidencePhoto
        fields = [
            "id", "image", "phash", "captured_at", "lat", "lng",
            "location", "uploader", "note", "event", "created_at",
        ]
        read_only_fields = ["phash", "event", "created_at"]

    def create(self, validated_data):
        lat = validated_data.pop("lat")
        lng = validated_data.pop("lng")
        image = validated_data["image"]
        validated_data["phash"] = compute_phash_hex(image)
        image.seek(0)
        validated_data["location"] = Point(lng, lat, srid=4326)
        photo = EvidencePhoto.objects.create(**validated_data)
        # 仅依据 pHash 生成疑似重复候选；不做任何自动合并
        generate_candidates_for_photo(photo)
        return photo


class PhotoBriefSerializer(serializers.ModelSerializer):
    location = GeometryField(read_only=True)

    class Meta:
        model = EvidencePhoto
        fields = ["id", "phash", "captured_at", "location", "event", "note"]


class DuplicateCandidateSerializer(serializers.ModelSerializer):
    photo = PhotoBriefSerializer(read_only=True)
    matched_photo = PhotoBriefSerializer(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = DuplicateCandidate
        fields = [
            "id", "photo", "matched_photo", "hamming_distance",
            "status", "status_display", "decision_note", "decided_by", "decided_at",
        ]
        read_only_fields = fields


class CandidateDecisionSerializer(serializers.Serializer):
    class Action(models.TextChoices):
        ATTACH = "attach", "确认同一问题，挂接到被匹配事件（不重复扣分）"
        CREATE_NEW = "create_new", "另立新事件（整改后复发 / 不同地点）"
        DIFFERENT = "different", "标记为不同，暂不处理"
        REJECTED = "rejected", "误报忽略"

    action = serializers.ChoiceField(choices=Action.choices)
    category = serializers.ChoiceField(choices=ProblemEvent.Category.choices, required=False)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    dedup_key = serializers.CharField(required=False, allow_blank=False, max_length=64)
    note = serializers.CharField(required=False, allow_blank=True, default="")
    actor = serializers.CharField(required=False, default="system", max_length=64)


class CreateEventFromPhotoSerializer(serializers.Serializer):
    category = serializers.ChoiceField(choices=ProblemEvent.Category.choices, required=False)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    dedup_key = serializers.CharField(required=False, allow_blank=False, max_length=64)
    occurred_at = serializers.DateTimeField(required=False)
    actor = serializers.CharField(required=False, default="system", max_length=64)


class RectificationReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = Rectification
        fields = ["id", "event", "photo", "note", "submitted_by", "submitted_at"]


class RectifyRequestSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, default="")
    actor = serializers.CharField(required=False, default="system", max_length=64)
    photo_id = serializers.PrimaryKeyRelatedField(
        queryset=EvidencePhoto.objects.all(), required=False, allow_null=True,
    )
    now = serializers.DateTimeField(required=False, help_text="可选：注入整改提交时间")


class PenaltyVersionSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)

    class Meta:
        model = PenaltyVersion
        fields = [
            "id", "version_no", "points", "escalation_level",
            "kind", "kind_display", "reason", "actor", "created_at",
        ]
        read_only_fields = fields


class EscalationRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = EscalationRecord
        fields = ["id", "penalty", "level", "version", "reason", "actor", "ran_at"]
        read_only_fields = fields


class ReviewRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReviewRecord
        fields = ["id", "penalty", "version", "approved", "comment", "reviewer", "reviewed_at"]
        read_only_fields = fields


class EventBriefSerializer(serializers.ModelSerializer):
    location = GeometryField(read_only=True)

    class Meta:
        model = ProblemEvent
        fields = [
            "id", "event_no", "category", "status", "location",
            "occurred_at", "grid", "contractor_name",
        ]


class PenaltyUnitSerializer(serializers.ModelSerializer):
    event = EventBriefSerializer(read_only=True)
    versions = PenaltyVersionSerializer(many=True, read_only=True)
    escalations = EscalationRecordSerializer(many=True, read_only=True)
    reviews = ReviewRecordSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    locked_version_no = serializers.SerializerMethodField()

    class Meta:
        model = PenaltyUnit
        fields = [
            "id", "penalty_no", "event", "contract", "contractor_name",
            "points", "escalation_level", "status", "status_display",
            "locked_version", "locked_version_no",
            "versions", "escalations", "reviews", "created_at",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_locked_version_no(self, obj) -> int | None:
        return obj.locked_version.version_no if obj.locked_version_id else None


class PenaltyCorrectionSerializer(serializers.Serializer):
    points = serializers.DecimalField(max_digits=6, decimal_places=1, min_value=0)
    reason = serializers.CharField(max_length=512)
    actor = serializers.CharField(required=False, default="system", max_length=64)


class PenaltyReviewSerializer(serializers.Serializer):
    approved = serializers.BooleanField()
    comment = serializers.CharField(required=False, allow_blank=True, default="")
    actor = serializers.CharField(required=False, default="reviewer", max_length=64)


class EscalationRunSerializer(serializers.Serializer):
    now = serializers.DateTimeField(required=False, help_text="注入的当前时间；不传则用服务器时钟")
    default_sla_hours = serializers.IntegerField(required=False, min_value=1)
    actor = serializers.CharField(required=False, default="escalation-job", max_length=64)


class ProblemEventSerializer(serializers.ModelSerializer):
    location = GeometryField(read_only=True)
    photos = PhotoBriefSerializer(many=True, read_only=True)
    penalty = PenaltyUnitSerializer(read_only=True)
    rectification = RectificationReadSerializer(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    category_display = serializers.CharField(source="get_category_display", read_only=True)

    class Meta:
        model = ProblemEvent
        fields = [
            "id", "event_no", "primary_photo", "grid", "contract", "contractor_name",
            "category", "category_display", "description", "location",
            "occurred_at", "status", "status_display", "sla_hours", "dedup_key",
            "photos", "penalty", "rectification", "created_at",
        ]
        read_only_fields = fields


# ============================ 证据封存包 ============================

class SealedFileSerializer(serializers.ModelSerializer):
    role_display = serializers.CharField(source="get_role_display", read_only=True)

    class Meta:
        model = SealedFile
        fields = [
            "id", "photo", "role", "role_display", "rel_path", "filename",
            "content_type", "size_bytes", "sha256", "missing_at_seal",
            "phash", "captured_at", "lng", "lat",
        ]
        read_only_fields = fields


class SealVerificationSerializer(serializers.ModelSerializer):
    source_display = serializers.CharField(source="get_source_display", read_only=True)

    class Meta:
        model = SealVerification
        fields = [
            "id", "source", "source_display", "result", "manifest_ok",
            "checked_files", "mismatch_files", "missing_files", "extra_files",
            "detail", "actor", "created_at",
        ]
        read_only_fields = fields


class SealPackageBriefSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    package_kind_display = serializers.CharField(source="get_package_kind_display", read_only=True)

    class Meta:
        model = SealPackage
        fields = [
            "id", "package_no", "subject_kind", "event", "penalty",
            "package_kind", "package_kind_display", "status", "status_display",
            "is_active", "parent", "replaces", "manifest_digest",
            "sealed_at", "sealed_by", "legacy_migrated", "missing_files",
            "created_at",
        ]
        read_only_fields = fields


class SealPackageSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    package_kind_display = serializers.CharField(source="get_package_kind_display", read_only=True)
    files = SealedFileSerializer(many=True, read_only=True)
    verifications = SealVerificationSerializer(many=True, read_only=True)
    children = SealPackageBriefSerializer(many=True, read_only=True)
    parent_no = serializers.SerializerMethodField()
    replaces_no = serializers.SerializerMethodField()

    class Meta:
        model = SealPackage
        fields = [
            "id", "package_no", "subject_kind", "event", "penalty",
            "package_kind", "package_kind_display", "status", "status_display",
            "is_active", "parent", "parent_no", "replaces", "replaces_no",
            "relation_note", "manifest", "manifest_digest", "content_fingerprint",
            "sealed_at", "sealed_by", "legacy_migrated", "missing_files",
            "files", "verifications", "children", "created_at",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_parent_no(self, obj):
        return obj.parent.package_no if obj.parent_id else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_replaces_no(self, obj):
        return obj.replaces.package_no if obj.replaces_id else None


class SealCreateSerializer(serializers.Serializer):
    event = serializers.PrimaryKeyRelatedField(
        queryset=ProblemEvent.objects.all(), required=False, allow_null=True,
    )
    penalty = serializers.PrimaryKeyRelatedField(
        queryset=PenaltyUnit.objects.all(), required=False, allow_null=True,
    )
    subject_kind = serializers.ChoiceField(
        choices=SealPackage.Subject.choices, required=False,
        default=SealPackage.Subject.PENALTY,
    )
    # 内容已变化时的显式关联方式；首次封存自动为 initial
    kind = serializers.ChoiceField(
        choices=[SealPackage.Kind.SUPPLEMENT, SealPackage.Kind.REPLACEMENT],
        required=False, default=SealPackage.Kind.SUPPLEMENT,
    )
    note = serializers.CharField(required=False, allow_blank=True, default="", max_length=512)
    actor = serializers.CharField(required=False, default="system", max_length=64)
    client_token = serializers.CharField(
        required=False, allow_blank=False, max_length=64,
        help_text="幂等键：重复/并发请求携带同键只返回同一封存包",
    )
    finalize = serializers.BooleanField(
        required=False, default=True, help_text="true=登记并立即封存；false=只建待封存包",
    )


class SealFinalizeSerializer(serializers.Serializer):
    actor = serializers.CharField(required=False, default="system", max_length=64)


class SealVerifySerializer(serializers.Serializer):
    source = serializers.ChoiceField(
        choices=[SealVerification.Source.ONLINE, SealVerification.Source.EXPORT],
        required=False, default=SealVerification.Source.ONLINE,
    )
    actor = serializers.CharField(required=False, default="system", max_length=64)


class SealExportCreateSerializer(serializers.Serializer):
    client_token = serializers.CharField(max_length=64, help_text="导出幂等键，中断后用同键重试续作")
    actor = serializers.CharField(required=False, default="system", max_length=64)
    run = serializers.BooleanField(required=False, default=True)
    # 测试/演练钩子：复制 N 个文件后模拟中断
    fail_after = serializers.IntegerField(required=False, allow_null=True, min_value=1)


class SealExportJobSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = SealExportJob
        fields = [
            "id", "package", "client_token", "status", "status_display",
            "bundle_path", "bundle_digest", "size_bytes",
            "total_files", "done_files", "cursor", "error",
            "created_by", "created_at", "updated_at", "completed_at", "download_url",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_download_url(self, obj):
        if obj.status == SealExportJob.Status.COMPLETED:
            return f"/api/seals/{obj.package_id}/exports/{obj.id}/download/"
        return None


class SealExportRunSerializer(serializers.Serializer):
    fail_after = serializers.IntegerField(required=False, allow_null=True, min_value=1)


class SealOfflineVerifySerializer(serializers.Serializer):
    bundle = serializers.FileField(required=True, help_text="导出的 .tar 离线封存包")
    actor = serializers.CharField(required=False, default="offline-checker", max_length=64)


class SealMigrateSerializer(serializers.Serializer):
    limit = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    actor = serializers.CharField(required=False, default="legacy-migration", max_length=64)
