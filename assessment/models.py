"""
考核领域模型：

道路网格 RoadGrid ──┐
                  ├──< CleaningContract（合同责任区间：网格 × 起止时间 × 承包商）
                  └──< ProblemEvent（问题事件：发生位置、发生时间）
                              │
                   EvidencePhoto（感知哈希 + 候选关联，可挂接到事件）
                              │
                     PenaltyUnit 1:1（唯一扣分单元，归属发生时的合同）
                              │
              PenaltyVersion（只追加、不可变）/ ReviewRecord / EscalationRecord
"""
import uuid

from django.contrib.gis.db import models as gis
from django.db import models


class TimeStamped(models.Model):
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        abstract = True


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


class RoadGrid(gis.Model):
    """道路网格（PostGIS 多边形，SRID 4326）。"""

    code = models.CharField("网格编号", max_length=32, unique=True)
    name = models.CharField("网格名称", max_length=128, blank=True)
    geom = gis.PolygonField("网格范围", srid=4326)

    class Meta:
        verbose_name = "道路网格"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.code} {self.name}".strip()


class CleaningContract(TimeStamped):
    """
    保洁合同责任区间：同一网格在同一时间只允许一个生效合同
    （业务上由归属服务校验重叠，录入端也应避免重叠）。
    扣分归属按“事件发生时刻落在哪个 [valid_from, valid_to) 区间”确定。
    """

    code = models.CharField("合同编号", max_length=32, unique=True)
    grid = gis.ForeignKey(RoadGrid, on_delete=models.PROTECT, related_name="contracts", verbose_name="道路网格")
    contractor_name = models.CharField("承包商名称", max_length=128)
    valid_from = models.DateTimeField("责任开始时间（含）")
    valid_to = models.DateTimeField("责任结束时间（不含）")

    class Meta:
        verbose_name = "保洁合同"
        verbose_name_plural = verbose_name
        indexes = [
            models.Index(fields=["grid", "valid_from", "valid_to"]),
            models.Index(fields=["contractor_name"]),
        ]

    def __str__(self):
        return f"{self.code} {self.contractor_name}"


class ProblemEvent(TimeStamped):
    """
    现场问题事件。一个事件 = 一次扣分口径；不同角度照片挂同一事件，
    整改后复发必须新建事件。
    """

    class Category(models.TextChoices):
        LITTER = "litter", "散落垃圾"
        OVERFLOW = "overflow", "垃圾桶满溢"
        ROAD_DIRT = "road_dirt", "路面污渍"
        DEAD_CORNER = "dead_corner", "卫生死角"
        OTHER = "other", "其他问题"

    class Status(models.TextChoices):
        OPEN = "open", "待整改"
        RECTIFIED = "rectified", "已整改"

    event_no = models.CharField("事件编号", max_length=32, unique=True, editable=False)
    primary_photo = gis.ForeignKey(
        "EvidencePhoto",
        on_delete=models.PROTECT,
        related_name="primary_of",
        null=True,
        blank=True,
        verbose_name="首报照片",
    )
    grid = gis.ForeignKey(RoadGrid, on_delete=models.PROTECT, related_name="events", null=True, verbose_name="所在网格")
    # 归属快照：按“发生时刻”解析出的合同；后续合同/承包商变更不改变历史归属
    contract = models.ForeignKey(
        CleaningContract, on_delete=models.PROTECT, related_name="events",
        null=True, verbose_name="责任合同（按发生时解析）",
    )
    contractor_name = models.CharField("责任承包商快照", max_length=128, blank=True)
    category = models.CharField("问题类别", max_length=20, choices=Category.choices)
    description = models.CharField("问题描述", max_length=512, blank=True)
    location = gis.PointField("发生位置", srid=4326)
    occurred_at = models.DateTimeField("发生/拍摄时间", help_text="归属与 SLA 都以该时间为准，而不是录入时间")
    status = models.CharField("状态", max_length=16, choices=Status.choices, default=Status.OPEN)
    sla_hours = models.PositiveIntegerField("整改时限(小时)", default=24)
    # 外部业务幂等键，防止同一条立案请求重复提交
    dedup_key = models.CharField("外部去重键", max_length=64, null=True, blank=True, unique=True)

    class Meta:
        verbose_name = "问题事件"
        verbose_name_plural = verbose_name
        ordering = ["-occurred_at"]
        indexes = [
            models.Index(fields=["status", "occurred_at"]),
            models.Index(fields=["contractor_name"]),
        ]

    def save(self, *args, **kwargs):
        if not self.event_no:
            self.event_no = _new_id("EV")
        super().save(*args, **kwargs)

    def __str__(self):
        return self.event_no


class EvidencePhoto(TimeStamped):
    """
    证据照片。上传时由 Pillow 计算 256 位感知哈希(pHash)。
    pHash 只用于生成 DuplicateCandidate（疑似重复候选）；
    照片是否属于同一事件，最终由人工按位置/时间判断。
    照片一旦创建不可删除、不可篡改（证据保全）。
    """

    image = models.ImageField("图片文件", upload_to="evidence/%Y/%m/%d")
    phash = models.CharField("感知哈希(hex)", max_length=64, db_index=True, editable=False)
    captured_at = models.DateTimeField("拍摄时间")
    location = gis.PointField("拍摄位置", srid=4326)
    uploader = models.CharField("上传人", max_length=64, blank=True, default="")
    note = models.CharField("备注", max_length=256, blank=True)
    event = models.ForeignKey(
        ProblemEvent, on_delete=models.PROTECT, related_name="photos",
        null=True, blank=True, verbose_name="关联事件",
    )

    class Meta:
        verbose_name = "证据照片"
        verbose_name_plural = verbose_name
        ordering = ["-captured_at"]


class DuplicateCandidate(models.Model):
    """
    疑似重复候选（pHash 汉明距离 <= 阈值时生成）。
    只表达“图片相似”，不自动合并事件；最终状态由人工判定给出。
    """

    class Status(models.TextChoices):
        PENDING = "pending", "待判定"
        CONFIRMED_DUPLICATE = "confirmed_duplicate", "确认同问题（挂接，不重复扣分）"
        RECURRENCE = "recurrence", "整改后复发（新建事件）"
        DIFFERENT = "different", "不同地点/不同问题（不合并）"
        REJECTED = "rejected", "误报忽略"

    photo = models.ForeignKey(EvidencePhoto, on_delete=models.CASCADE, related_name="candidates", verbose_name="新照片")
    matched_photo = models.ForeignKey(EvidencePhoto, on_delete=models.CASCADE, related_name="matched_as", verbose_name="相似照片")
    hamming_distance = models.PositiveSmallIntegerField("pHash汉明距离")
    status = models.CharField("判定状态", max_length=24, choices=Status.choices, default=Status.PENDING)
    decision_note = models.CharField("判定说明", max_length=256, blank=True)
    decided_by = models.CharField("判定人", max_length=64, blank=True)
    decided_at = models.DateTimeField("判定时间", null=True, blank=True)

    class Meta:
        verbose_name = "疑似重复候选"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(fields=["photo", "matched_photo"], name="uniq_candidate_pair"),
        ]
        indexes = [models.Index(fields=["status"])]
        ordering = ["-id"]

    def __str__(self):
        return f"{self.photo_id}~{self.matched_photo_id} d={self.hamming_distance} {self.status}"


class Rectification(models.Model):
    """整改记录（每个事件至多一条；重复回调返回 409 并被忽略）。"""

    event = models.OneToOneField(
        ProblemEvent, on_delete=models.PROTECT, related_name="rectification", verbose_name="问题事件",
    )
    photo = models.ForeignKey(
        EvidencePhoto, on_delete=models.PROTECT, related_name="rectifications",
        null=True, blank=True, verbose_name="整改后照片",
    )
    note = models.CharField("整改说明", max_length=512, blank=True)
    submitted_by = models.CharField("提交人", max_length=64, blank=True, default="")
    submitted_at = models.DateTimeField("整改提交时间")

    class Meta:
        verbose_name = "整改记录"
        verbose_name_plural = verbose_name


class PenaltyUnit(TimeStamped):
    """
    处罚单元（扣分的最小且唯一归属单元）：一个事件一个 PenaltyUnit。
    每笔扣分都能通过 penalty_no 追到：事件、责任合同/承包商、全部证据、全部版本。
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "待复核"
        LOCKED = "locked", "已锁定"

    penalty_no = models.CharField("处罚单号", max_length=32, unique=True, editable=False)
    event = models.OneToOneField(ProblemEvent, on_delete=models.PROTECT, related_name="penalty", verbose_name="问题事件")
    contract = models.ForeignKey(
        CleaningContract, on_delete=models.PROTECT, related_name="penalties",
        null=True, verbose_name="责任合同快照",
    )
    contractor_name = models.CharField("责任承包商快照", max_length=128, blank=True)
    points = models.DecimalField("当前有效扣分", max_digits=6, decimal_places=1)
    escalation_level = models.PositiveSmallIntegerField("逾期升级等级", default=0)
    status = models.CharField("状态", max_length=16, choices=Status.choices, default=Status.DRAFT)
    locked_version = models.ForeignKey(
        "PenaltyVersion", on_delete=models.PROTECT, related_name="locked_by_penalties",
        null=True, blank=True, verbose_name="最近锁定版本",
    )

    class Meta:
        verbose_name = "处罚单元"
        verbose_name_plural = verbose_name
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.penalty_no:
            self.penalty_no = _new_id("PN")
        super().save(*args, **kwargs)

    def __str__(self):
        return self.penalty_no


class PenaltyVersion(models.Model):
    """
    处罚版本——只追加(append-only)、不可变：
    初版立案、逾期升级、人工更正都只能新增一行；
    复核通过时由 PenaltyUnit.locked_version 指向锁定行，历史行永不修改。
    """

    class Kind(models.TextChoices):
        INITIAL = "initial", "初版立案"
        ESCALATION = "escalation", "逾期升级"
        CORRECTION = "correction", "人工更正"

    penalty = models.ForeignKey(PenaltyUnit, on_delete=models.PROTECT, related_name="versions", verbose_name="处罚单元")
    version_no = models.PositiveIntegerField("版本号")
    points = models.DecimalField("扣分", max_digits=6, decimal_places=1)
    escalation_level = models.PositiveSmallIntegerField("逾期等级", default=0)
    kind = models.CharField("版本类型", max_length=16, choices=Kind.choices)
    reason = models.CharField("原因", max_length=512)
    actor = models.CharField("操作人/任务", max_length=64, blank=True, default="")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "处罚版本"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(fields=["penalty", "version_no"], name="uniq_penalty_version_no"),
        ]
        ordering = ["penalty_id", "version_no"]


class EscalationRecord(models.Model):
    """逾期升级执行记录（可注入时钟，按 (处罚单, 等级) 幂等）。"""

    penalty = models.ForeignKey(PenaltyUnit, on_delete=models.PROTECT, related_name="escalations", verbose_name="处罚单元")
    level = models.PositiveSmallIntegerField("升级到等级")
    version = models.ForeignKey(PenaltyVersion, on_delete=models.PROTECT, related_name="escalations", verbose_name="产生的处罚版本")
    reason = models.CharField("升级原因", max_length=512)
    actor = models.CharField("执行任务", max_length=64, blank=True, default="")
    ran_at = models.DateTimeField("执行时间(注入时钟)")

    class Meta:
        verbose_name = "升级记录"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(fields=["penalty", "level"], name="uniq_escalation_level"),
        ]
        ordering = ["penalty_id", "level"]


class ReviewRecord(models.Model):
    """复核记录：复核通过即锁定当前版本。"""

    penalty = models.ForeignKey(PenaltyUnit, on_delete=models.PROTECT, related_name="reviews", verbose_name="处罚单元")
    version = models.ForeignKey(PenaltyVersion, on_delete=models.PROTECT, related_name="reviews", verbose_name="被复核版本")
    approved = models.BooleanField("是否通过")
    comment = models.CharField("复核意见", max_length=512, blank=True)
    reviewer = models.CharField("复核人", max_length=64, blank=True, default="")
    reviewed_at = models.DateTimeField("复核时间", auto_now_add=True)

    class Meta:
        verbose_name = "复核记录"
        verbose_name_plural = verbose_name
        ordering = ["-reviewed_at"]


class SealPackage(TimeStamped):
    """
    证据封存包（不可变清单）。

    封存时把某事件/处罚在该时刻的全部关联照片、文件摘要、关键元数据、
    候选判定、整改记录与当前处罚版本固化为一份带哈希的清单(manifest)。

    不变量：
    * 封存后新增补拍 / 整改 / 升级 / 更正**不得改旧包**，只能形成显式关联的
      补充包(supplement)或替代包(superseded)；
    * 同一处罚(事件)在任意时刻至多有一个“活动包”(is_active=True)；
      重复封存/并发请求要么返回既有活动包，要么在唯一约束上失败后回退；
    * 文件摘要不符只把包标记为 verification_failed（并写 SealVerification），
      绝不改变事件、处罚或自动合并候选；
    * 状态：pending(待封存) / sealed(已封存) / supplemented(已补充,历史包) /
      verification_failed(校验失败,仍是活动包) / superseded(已替代,历史包)。
    """

    class Status(models.TextChoices):
        PENDING = "pending", "待封存"
        SEALED = "sealed", "已封存"
        SUPPLEMENTED = "supplemented", "已补充（历史包）"
        VERIFICATION_FAILED = "verification_failed", "校验失败"
        SUPERSEDED = "superceded", "已替代（历史包）"

    class Kind(models.TextChoices):
        INITIAL = "initial", "首次封存"
        SUPPLEMENT = "supplement", "补充包"
        REPLACEMENT = "replacement", "替代包"

    class Subject(models.TextChoices):
        EVENT = "event", "事件封存"
        PENALTY = "penalty", "处罚封存"

    package_no = models.CharField("封存包编号", max_length=32, unique=True, editable=False)
    subject_kind = models.CharField("封存对象类型", max_length=16, choices=Subject.choices)
    event = models.ForeignKey(
        ProblemEvent, on_delete=models.PROTECT, related_name="seal_packages", verbose_name="问题事件",
    )
    penalty = models.ForeignKey(
        PenaltyUnit, on_delete=models.PROTECT, related_name="seal_packages", verbose_name="处罚单元",
    )
    package_kind = models.CharField("包类型", max_length=16, choices=Kind.choices, default=Kind.INITIAL)
    status = models.CharField("状态", max_length=24, choices=Status.choices, default=Status.PENDING, db_index=True)
    is_active = models.BooleanField("是否为当前活动包", default=True)
    # 显式关联：补充包/替代包指向它所依据的上一个活动包；旧包行本身不变
    parent = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True,
        related_name="children", verbose_name="上一版本包",
    )
    replaces = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True,
        related_name="replaced_by_set", verbose_name="被替代的包",
    )
    relation_note = models.CharField("补充/替代说明", max_length=512, blank=True, default="")
    # 客户端幂等键：同键重试落在同一个包上（不要求全局唯一，仅同处罚内唯一）
    client_token = models.CharField("客户端幂等键", max_length=64, blank=True, default="", db_index=True)
    # 规范化清单（canonical JSON）与摘要；pending 阶段为空，封存成功后写入且不再变化
    manifest = models.JSONField("封存清单", null=True, blank=True, editable=False)
    manifest_digest = models.CharField("清单摘要(sha256)", max_length=64, blank=True, default="", db_index=True)
    content_fingerprint = models.CharField(
        "内容指纹(不含文件摘要)", max_length=64, blank=True, default="",
        help_text="重复封存时若指纹相同则直接返回既有活动包，不产生第二个活动包",
    )
    sealed_at = models.DateTimeField("封存时间", null=True, blank=True)
    sealed_by = models.CharField("封存操作人", max_length=64, blank=True, default="")
    legacy_migrated = models.BooleanField("历史数据迁移生成", default=False)
    missing_files = models.BooleanField("封存时即有文件缺失", default=False)

    class Meta:
        verbose_name = "证据封存包"
        verbose_name_plural = verbose_name
        ordering = ["-created_at"]
        constraints = [
            # 同一处罚至多一个活动包——重复封存与并发请求的最终防线
            models.UniqueConstraint(
                fields=["penalty"],
                condition=models.Q(is_active=True),
                name="uniq_active_seal_per_penalty",
            ),
            models.UniqueConstraint(
                fields=["event"],
                condition=models.Q(is_active=True),
                name="uniq_active_seal_per_event",
            ),
            # 同一处罚 + 幂等键至多一个包（空串不参与）
            models.UniqueConstraint(
                fields=["penalty", "client_token"],
                condition=~models.Q(client_token=""),
                name="uniq_seal_penalty_token",
            ),
        ]
        indexes = [
            models.Index(fields=["penalty", "package_kind"]),
            models.Index(fields=["status", "is_active"]),
        ]

    def save(self, *args, **kwargs):
        if not self.package_no:
            self.package_no = _new_id("SP")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.package_no} {self.penalty_id} {self.status}"


class SealedFile(models.Model):
    """封存包内每个证据文件的清单行（照片 / 整改照片 / 外部文件）。"""

    class Role(models.TextChoices):
        EVIDENCE = "evidence", "证据照片"
        RECTIFICATION = "rectification", "整改照片"
        ATTACHMENT = "attachment", "外部文件"

    package = models.ForeignKey(
        SealPackage, on_delete=models.PROTECT, related_name="files", verbose_name="封存包",
    )
    photo = models.ForeignKey(
        EvidencePhoto, on_delete=models.PROTECT, null=True, blank=True,
        related_name="sealed_files", verbose_name="证据照片",
    )
    role = models.CharField("文件角色", max_length=16, choices=Role.choices)
    rel_path = models.CharField("包内相对路径", max_length=512)
    storage_path = models.CharField("存储路径", max_length=512, blank=True, default="")
    filename = models.CharField("文件名", max_length=256)
    content_type = models.CharField("内容类型", max_length=64, blank=True, default="")
    size_bytes = models.BigIntegerField("文件大小(字节)", null=True, blank=True)
    sha256 = models.CharField("封存时文件摘要(sha256)", max_length=64, blank=True, default="")
    missing_at_seal = models.BooleanField("封存时文件缺失", default=False)
    # 冗余关键元数据，便于列表/追溯直接查询
    phash = models.CharField("感知哈希(hex)", max_length=64, blank=True, default="")
    captured_at = models.DateTimeField("拍摄时间", null=True, blank=True)
    lng = models.FloatField("经度", null=True, blank=True)
    lat = models.FloatField("纬度", null=True, blank=True)

    class Meta:
        verbose_name = "封存清单文件"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(fields=["package", "rel_path"], name="uniq_sealed_file_path"),
        ]
        indexes = [models.Index(fields=["photo"]), models.Index(fields=["sha256"])]
        ordering = ["rel_path"]


class SealVerification(models.Model):
    """
    封存包校验记录（在线/离线/导出复核都落一行）。
    校验失败仅更新包状态与本表，不允许触碰事件/处罚/候选。
    """

    class Result(models.TextChoices):
        VALID = "valid", "校验通过"
        INVALID = "invalid", "校验失败"

    class Source(models.TextChoices):
        ONLINE = "online", "在线校验"
        OFFLINE = "offline", "离线校验"
        EXPORT = "export", "导出校验"

    package = models.ForeignKey(
        SealPackage, on_delete=models.PROTECT, related_name="verifications", verbose_name="封存包",
    )
    source = models.CharField("校验来源", max_length=16, choices=Source.choices)
    result = models.CharField("校验结果", max_length=16, choices=Result.choices)
    manifest_ok = models.BooleanField("清单摘要一致", default=False)
    checked_files = models.PositiveIntegerField("已校验文件数", default=0)
    mismatch_files = models.JSONField("摘要不符文件", default=list, blank=True)
    missing_files = models.JSONField("缺失文件", default=list, blank=True)
    extra_files = models.JSONField("包外多余文件", default=list, blank=True)
    detail = models.CharField("说明", max_length=512, blank=True, default="")
    actor = models.CharField("校验人/任务", max_length=64, blank=True, default="")
    created_at = models.DateTimeField("校验时间", auto_now_add=True)

    class Meta:
        verbose_name = "封存校验记录"
        verbose_name_plural = verbose_name
        ordering = ["-created_at"]


class SealExportJob(models.Model):
    """
    封存包导出任务：生成可离线校验的 tar 包（内含文件与校验器）。

    以 package + client_token 幂等；构建过程按文件推进 cursor，
    中断后用同一 client_token 重试可续作，不重复生成活动导出。
    """

    class Status(models.TextChoices):
        PENDING = "pending", "待导出"
        BUILDING = "building", "构建中"
        COMPLETED = "completed", "已完成"
        FAILED = "failed", "失败"

    package = models.ForeignKey(
        SealPackage, on_delete=models.PROTECT, related_name="export_jobs", verbose_name="封存包",
    )
    client_token = models.CharField("客户端幂等键", max_length=64)
    status = models.CharField("状态", max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    bundle_path = models.CharField("导出包路径", max_length=512, blank=True, default="")
    bundle_digest = models.CharField("导出包摘要(sha256)", max_length=64, blank=True, default="")
    size_bytes = models.BigIntegerField("导出包大小", null=True, blank=True)
    total_files = models.PositiveIntegerField("待导出文件数", default=0)
    done_files = models.PositiveIntegerField("已导出文件数", default=0)
    cursor = models.CharField("续作游标(最后写入的 rel_path)", max_length=512, blank=True, default="")
    error = models.CharField("失败原因", max_length=512, blank=True, default="")
    created_by = models.CharField("请求人", max_length=64, blank=True, default="")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)
    completed_at = models.DateTimeField("完成时间", null=True, blank=True)

    class Meta:
        verbose_name = "封存导出任务"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(fields=["package", "client_token"], name="uniq_export_token"),
        ]
        ordering = ["-created_at"]
