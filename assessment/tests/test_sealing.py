"""
证据封存包端到端测试（真实 PostgreSQL/PostGIS）。

验收覆盖：
1. 正常封存可离线校验，并从导出包还原唯一处罚与完整证据；
2. 封存后新增更正/补拍/整改，只产生显式关联的补充/替代包，旧包不被修改；
3. 重复封存与并发请求只产生一个活动包；
4. 文件损坏或缺失时只标记校验失败，事件/处罚/候选判定不受影响，原链仍可追溯；
5. 旧数据迁移补建封存包（幂等）；
6. 导出确定性、中断重试可恢复；
7. 跨地点同图候选不会混入同一封存包。
"""
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError, connections, transaction
from django.utils import timezone
from rest_framework.test import APITestCase, APITransactionTestCase

from assessment.mockimages import scene_png_bytes
from assessment.models import (
    CleaningContract,
    DuplicateCandidate,
    EvidencePackage,
    EvidencePhoto,
    PenaltyUnit,
    PenaltyVersion,
    ProblemEvent,
    RoadGrid,
)
from assessment.services.sealing import (
    backfill_missing_packages,
    canonical_manifest_bytes,
    seal_penalty,
    verify_export_archive,
)

GRID1 = {
    "type": "Polygon",
    "coordinates": [[
        [121.4700, 31.2300], [121.4800, 31.2300],
        [121.4800, 31.2400], [121.4700, 31.2400],
        [121.4700, 31.2300],
    ]],
}
GRID2 = {
    "type": "Polygon",
    "coordinates": [[
        [121.5000, 31.2500], [121.5200, 31.2500],
        [121.5200, 31.2700], [121.5000, 31.2700],
        [121.5000, 31.2500],
    ]],
}
A1_LNG, A1_LAT = 121.4750, 31.2351
A2_LNG, A2_LAT = 121.4751, 31.2352
FAR_LNG, FAR_LAT = 121.5100, 31.2601
ROOT = Path(__file__).resolve().parents[2]


class EvidencePackageTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.now = timezone.now().replace(microsecond=0)
        cls.grid1 = RoadGrid.objects.create(code="G1", name="人民东路一段", geom=json.dumps(GRID1))
        cls.grid2 = RoadGrid.objects.create(code="G2", name="人民东路二段(远)", geom=json.dumps(GRID2))
        CleaningContract.objects.create(
            code="B-NEW", grid=cls.grid1, contractor_name="乙保洁公司",
            valid_from=cls.now - timedelta(days=10), valid_to=cls.now + timedelta(days=300),
        )
        CleaningContract.objects.create(
            code="C-FAR", grid=cls.grid2, contractor_name="丙保洁公司",
            valid_from=cls.now - timedelta(days=10), valid_to=cls.now + timedelta(days=300),
        )

    # ---------- 辅助 ----------
    def upload_photo(self, scene, lng, lat, captured_at, note=""):
        upload = SimpleUploadedFile(f"{scene}.png", scene_png_bytes(scene), content_type="image/png")
        resp = self.client.post(
            "/api/photos/",
            {"image": upload, "lng": lng, "lat": lat,
             "captured_at": captured_at.isoformat(), "note": note},
            format="multipart",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    def create_event(self, photo_id, **extra):
        payload = {"category": "litter"}
        payload.update(extra)
        resp = self.client.post(f"/api/photos/{photo_id}/create_event/", payload, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    def make_event(self, scene="scene_a_angle1", lng=A1_LNG, lat=A1_LAT, hours_ago=2):
        photo = self.upload_photo(scene, lng, lat, self.now - timedelta(hours=hours_ago))
        return self.create_event(photo["id"]), photo

    def seal(self, penalty_id, expect=201, **payload):
        resp = self.client.post(f"/api/penalties/{penalty_id}/seal/", payload, format="json")
        self.assertEqual(resp.status_code, expect, resp.content)
        return resp.json()

    def get_package(self, package_id):
        resp = self.client.get(f"/api/packages/{package_id}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def export_package(self, package_id, expect=200):
        return self.client.get(f"/api/packages/{package_id}/export/")

    def verify_online(self, package_id):
        resp = self.client.post(f"/api/packages/{package_id}/verify/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def verify_offline(self, archive_bytes):
        upload = SimpleUploadedFile("pkg.zip", archive_bytes, content_type="application/zip")
        resp = self.client.post("/api/packages/verify_offline/", {"archive": upload}, format="multipart")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    @staticmethod
    def photo_abs_path(photo_id):
        return EvidencePhoto.objects.get(pk=photo_id).image.path

    # ---------- 1. 正常封存 + 离线校验 + 还原唯一处罚与完整证据 ----------
    def test_seal_export_and_offline_verify_restores_penalty_and_evidence(self):
        t = self.now
        # 首报立案 + 不同角度挂接（同一问题只扣一次）+ 整改
        p1 = self.upload_photo("scene_a_angle1", A1_LNG, A1_LAT, t - timedelta(hours=2), "首报")
        e1 = self.create_event(p1["id"])
        p2 = self.upload_photo("scene_a_angle2", A2_LNG, A2_LAT, t - timedelta(hours=2), "换角度")
        cand = self.client.get(f"/api/candidates/?photo={p2['id']}&status=pending").json()["results"][0]
        self.client.post(f"/api/candidates/{cand['id']}/decide/",
                         {"action": "attach", "actor": "监督员-王"}, format="json")
        self.client.post(f"/api/events/{e1['id']}/rectify/",
                         {"actor": "乙班组长", "note": "已清理",
                          "now": (t - timedelta(hours=1)).isoformat()}, format="json")

        penalty_id = e1["penalty"]["id"]
        package = self.seal(penalty_id, actor="监督员-王", note="复核前封存")
        self.assertEqual(package["status"], "sealed")
        self.assertEqual(package["kind"], "original")
        self.assertIsNone(package["parent"])
        self.assertFalse(package["is_stale"])

        # 不可变清单内容：照片（含文件摘要）、元数据、候选判定、整改、当前处罚版本
        manifest = package["manifest"]
        self.assertEqual(manifest["penalty"]["penalty_no"], e1["penalty"]["penalty_no"])
        self.assertEqual(manifest["penalty"]["current_version"]["version_no"], 1)
        self.assertEqual(manifest["event"]["event_no"], e1["event_no"])
        self.assertEqual(manifest["event"]["contractor_name"], "乙保洁公司")
        self.assertEqual({p["photo_id"] for p in manifest["photos"]}, {p1["id"], p2["id"]})
        for entry in manifest["photos"]:
            self.assertEqual(len(entry["file"]["sha256"]), 64)
            self.assertTrue(entry["file"]["readable"])
            self.assertIsNotNone(entry["location"])
            self.assertIsNotNone(entry["captured_at"])
        self.assertEqual(len(manifest["candidate_decisions"]), 1)
        self.assertEqual(manifest["candidate_decisions"][0]["status"], "confirmed_duplicate")
        self.assertEqual(manifest["rectification"]["note"], "已清理")
        # 清单哈希可复算
        self.assertEqual(
            hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest(),
            package["manifest_hash"],
        )

        # 在线校验通过
        report = self.verify_online(package["id"])
        self.assertTrue(report["ok"])
        self.assertEqual(report["files_total"], 2)
        self.assertEqual(report["files_ok"], 2)

        # 导出 → 离线校验（API 与独立脚本）→ 还原唯一处罚与完整证据
        archive = self.export_package(package["id"]).content
        offline = self.verify_offline(archive)
        self.assertTrue(offline["ok"], offline)
        self.assertEqual(offline["penalty_no"], e1["penalty"]["penalty_no"])
        self.assertEqual(offline["event_no"], e1["event_no"])
        self.assertEqual(offline["manifest_hash"], package["manifest_hash"])
        self.assertEqual(offline["files_total"], 2)
        self.assertEqual({ph["photo_id"] for ph in offline["photos"]}, {p1["id"], p2["id"]})
        # 纯标准库脚本（真正脱离服务离线校验）
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(archive)
            tmp_path = tmp.name
        proc = subprocess.run(
            [sys.executable, str(ROOT / "docs" / "verify_evidence_package.py"), tmp_path],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        script_report = json.loads(proc.stdout)
        self.assertTrue(script_report["ok"])
        self.assertEqual(script_report["penalty_no"], e1["penalty"]["penalty_no"])

        # 追溯详情：处罚单 → 封存链
        penalty = self.client.get(f"/api/penalties/{penalty_id}/").json()
        self.assertEqual(len(penalty["packages"]), 1)
        self.assertEqual(penalty["packages"][0]["package_no"], package["package_no"])
        self.assertEqual(penalty["packages"][0]["status"], "sealed")

    # ---------- 2a. 封存后更正只产生补充包，旧包不动 ----------
    def test_correction_after_seal_yields_supplement_only(self):
        event, _ = self.make_event()
        penalty_id = event["penalty"]["id"]
        original = self.seal(penalty_id)
        original_hash = original["manifest_hash"]

        # 封存后人工更正（追加处罚版本）→ 活动包标记为过时
        self.client.post(f"/api/penalties/{penalty_id}/correct/",
                         {"points": "1.0", "reason": "核减", "actor": "监督员-王"}, format="json")
        stale = self.get_package(original["id"])
        self.assertTrue(stale["is_stale"])
        # 旧包未被修改
        self.assertEqual(stale["manifest_hash"], original_hash)
        self.assertEqual(stale["status"], "sealed")

        # 形成显式关联的补充包
        supplement = self.client.post(
            f"/api/packages/{original['id']}/supplement/",
            {"actor": "监督员-王", "note": "更正后补充封存"}, format="json",
        )
        self.assertEqual(supplement.status_code, 201, supplement.content)
        supplement = supplement.json()
        self.assertEqual(supplement["kind"], "supplement")
        self.assertEqual(supplement["parent"], original["id"])
        self.assertEqual(supplement["parent_package_no"], original["package_no"])
        self.assertEqual(supplement["status"], "sealed")
        self.assertFalse(supplement["is_stale"])
        self.assertEqual(supplement["manifest"]["penalty"]["current_version"]["version_no"], 2)
        self.assertEqual(supplement["manifest"]["penalty"]["current_version"]["kind"], "correction")

        # 旧包状态变为已补充/已替代，但清单与摘要一字未动
        original_after = self.get_package(original["id"])
        self.assertEqual(original_after["status"], "superseded")
        self.assertEqual(original_after["manifest_hash"], original_hash)
        self.assertEqual(original_after["manifest"]["penalty"]["current_version"]["version_no"], 1)
        self.assertEqual(EvidencePackage.objects.get(pk=original["id"]).manifest_hash, original_hash)

        # 全链只有一个活动包；处罚单可追溯完整封存链
        packages = self.client.get(f"/api/packages/?penalty={penalty_id}").json()["results"]
        self.assertEqual(len(packages), 2)
        active = [p for p in packages if p["status"] in ("pending", "sealed")]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], supplement["id"])
        # 处罚版本链保持 append-only
        self.assertEqual(PenaltyVersion.objects.filter(penalty_id=penalty_id).count(), 2)

        # 补充包自身也是完整快照，可独立离线校验
        archive = self.export_package(supplement["id"]).content
        offline = self.verify_offline(archive)
        self.assertTrue(offline["ok"], offline)
        self.assertEqual(offline["current_version_no"], 2)

    # ---------- 2b. 封存后补拍/整改/升级 → 补充或替代包 ----------
    def test_new_evidence_and_escalation_after_seal(self):
        t = self.now
        event, p1 = self.make_event()
        penalty_id = event["penalty"]["id"]
        original = self.seal(penalty_id)

        # 逾期升级（事件仍待整改，追加 escalation 版本）→ 替代包重封当前状态
        run = self.client.post("/api/escalations/run/",
                               {"now": (t + timedelta(hours=30)).isoformat()}, format="json")
        self.assertEqual(run.json()["created_count"], 1)
        replacement = self.client.post(
            f"/api/packages/{original['id']}/replace/", {"note": "升级后重封"}, format="json",
        )
        self.assertEqual(replacement.status_code, 201, replacement.content)
        replacement = replacement.json()
        self.assertEqual(replacement["kind"], "replacement")
        self.assertEqual(replacement["parent"], original["id"])
        self.assertEqual(replacement["manifest"]["penalty"]["current_version"]["kind"], "escalation")
        self.assertEqual(self.get_package(original["id"])["status"], "superseded")

        # 补拍（挂接）+ 整改（附整改照片）→ 补充包
        p2 = self.upload_photo("scene_a_angle2", A2_LNG, A2_LAT, t + timedelta(hours=28), "补拍")
        cand = self.client.get(f"/api/candidates/?photo={p2['id']}&status=pending").json()["results"][0]
        self.client.post(f"/api/candidates/{cand['id']}/decide/", {"action": "attach"}, format="json")
        p3 = self.upload_photo("scene_c_bins", A1_LNG, A1_LAT, t + timedelta(hours=29), "整改后")
        self.client.post(f"/api/events/{event['id']}/rectify/",
                         {"photo_id": p3["id"], "note": "已清理并拍照",
                          "now": (t + timedelta(hours=31)).isoformat()}, format="json")

        supplement = self.client.post(
            f"/api/packages/{replacement['id']}/supplement/", {"note": "补拍+整改"}, format="json",
        )
        self.assertEqual(supplement.status_code, 201, supplement.content)
        supplement = supplement.json()
        roles = {p["photo_id"]: p["role"] for p in supplement["manifest"]["photos"]}
        self.assertEqual(roles, {p1["id"]: "evidence", p2["id"]: "evidence", p3["id"]: "rectification"})
        self.assertIsNotNone(supplement["manifest"]["rectification"])
        self.assertEqual(supplement["manifest"]["rectification"]["photo_id"], p3["id"])
        self.assertEqual(self.get_package(replacement["id"])["status"], "superseded")

        # 已替代的旧包不能再派生新包（只能基于链头）
        resp = self.client.post(f"/api/packages/{original['id']}/supplement/", {}, format="json")
        self.assertEqual(resp.status_code, 409)
        # 链式结构完整：original <- replacement <- supplement
        chain = self.client.get(f"/api/packages/?penalty={penalty_id}").json()["results"]
        self.assertEqual(len(chain), 3)
        by_id = {p["id"]: p for p in chain}
        self.assertEqual(by_id[replacement["id"]]["parent"], original["id"])
        self.assertEqual(by_id[supplement["id"]]["parent"], replacement["id"])

    # ---------- 3. 重复封存请求只产生一个活动包 ----------
    def test_duplicate_seal_requests_yield_single_active_package(self):
        event, _ = self.make_event()
        penalty_id = event["penalty"]["id"]

        first = self.seal(penalty_id)
        again = self.seal(penalty_id, expect=200)  # 幂等返回链头
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(EvidencePackage.objects.filter(penalty_id=penalty_id).count(), 1)

        # 事件入口同样幂等，且与处罚入口共享同一封存链
        via_event = self.client.post(f"/api/events/{event['id']}/seal/", {}, format="json")
        self.assertEqual(via_event.status_code, 200)
        self.assertEqual(via_event.json()["id"], first["id"])
        self.assertEqual(EvidencePackage.objects.filter(penalty_id=penalty_id).count(), 1)

        # 数据库约束兜底：任何途径都无法写入第二个活动包
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                EvidencePackage.objects.create(
                    penalty_id=penalty_id, kind=EvidencePackage.Kind.ORIGINAL,
                    status=EvidencePackage.Status.SEALED,
                    manifest={}, manifest_hash="0" * 64,
                )

    # ---------- 4a. 文件损坏 → 校验失败；业务链不受污染、仍可追溯；修复后可恢复 ----------
    def test_corrupted_file_marks_verify_failed_without_touching_business_chain(self):
        event, p1 = self.make_event()
        penalty_id = event["penalty"]["id"]
        package = self.seal(penalty_id)

        path = self.photo_abs_path(p1["id"])
        original_bytes = Path(path).read_bytes()
        try:
            Path(path).write_bytes(b"tampered-bytes")

            report = self.verify_online(package["id"])
            self.assertFalse(report["ok"])
            failed = [c for c in report["checks"] if c["check"] == "file_sha256"]
            self.assertEqual(failed[0]["error"], "digest_mismatch")
            self.assertEqual(self.get_package(package["id"])["status"], "verify_failed")

            # 校验异常只落在封存包上：事件、处罚、候选判定均不变
            penalty = PenaltyUnit.objects.get(pk=penalty_id)
            self.assertEqual(float(penalty.points), 2.0)
            self.assertEqual(penalty.status, "draft")
            self.assertEqual(penalty.versions.count(), 1)
            evt = ProblemEvent.objects.get(pk=event["id"])
            self.assertEqual(evt.status, "open")
            self.assertEqual(DuplicateCandidate.objects.count(), 0)

            # 原链仍可追溯：处罚详情、封存包详情、清单都完好可读
            trace = self.client.get(f"/api/penalties/{penalty_id}/")
            self.assertEqual(trace.status_code, 200)
            self.assertEqual(trace.json()["packages"][0]["status"], "verify_failed")
            detail = self.get_package(package["id"])
            self.assertEqual(detail["manifest"]["penalty"]["penalty_no"], event["penalty"]["penalty_no"])
            self.assertFalse(detail["is_stale"])  # 校验失败不算“过时”，它是异常

            # 摘要损坏的文件拒绝导出（不会悄悄发出污染的证据包）
            self.assertEqual(self.export_package(package["id"]).status_code, 409)
        finally:
            Path(path).write_bytes(original_bytes)

        # 文件恢复后重新校验 → 状态回到已封存
        report = self.verify_online(package["id"])
        self.assertTrue(report["ok"])
        self.assertEqual(self.get_package(package["id"])["status"], "sealed")
        self.assertEqual(self.export_package(package["id"]).status_code, 200)

    # ---------- 4b. 文件缺失 → 校验失败；原链仍可追溯 ----------
    def test_missing_file_marks_verify_failed_and_chain_stays_traceable(self):
        event, p1 = self.make_event()
        penalty_id = event["penalty"]["id"]
        package = self.seal(penalty_id)

        path = Path(self.photo_abs_path(p1["id"]))
        saved = path.read_bytes()
        path.unlink()
        try:
            report = self.verify_online(package["id"])
            self.assertFalse(report["ok"])
            errors = {c.get("error") for c in report["checks"] if c["check"] == "file_sha256"}
            self.assertEqual(errors, {"missing"})
            self.assertEqual(self.get_package(package["id"])["status"], "verify_failed")
            self.assertEqual(self.export_package(package["id"]).status_code, 409)

            # 处罚单号 → 事件 → 封存链 依旧完整可查
            penalties = self.client.get("/api/penalties/").json()["results"]
            mine = next(p for p in penalties if p["id"] == penalty_id)
            self.assertEqual(mine["event"]["event_no"], event["event_no"])
            self.assertEqual(mine["packages"][0]["package_no"], package["package_no"])
            # 离线侧：导不出新包，但已导出的旧包仍可独立校验（此处验证缺失时清单本身完好）
            self.assertEqual(
                hashlib.sha256(canonical_manifest_bytes(
                    self.get_package(package["id"])["manifest"])).hexdigest(),
                package["manifest_hash"],
            )
        finally:
            path.write_bytes(saved)

    # ---------- 5. 旧数据迁移：补建封存包，幂等 ----------
    def test_backfill_seals_legacy_penalties_idempotently(self):
        e1, _ = self.make_event(scene="scene_a_angle1", hours_ago=5)
        e2, _ = self.make_event(scene="scene_c_bins", hours_ago=3)
        self.assertEqual(EvidencePackage.objects.count(), 0)

        result = backfill_missing_packages(actor="迁移测试")
        self.assertEqual(result.created_count, 2)
        self.assertEqual(EvidencePackage.objects.count(), 2)
        for package in EvidencePackage.objects.all():
            self.assertEqual(package.status, "sealed")
            self.assertEqual(package.kind, "original")
            self.assertEqual(package.sealed_by, "迁移测试")
            self.assertTrue(package.manifest["penalty"]["penalty_no"])

        # 管理命令入口 + 幂等：再跑一遍不新增
        call_command("seal_existing_packages")
        self.assertEqual(EvidencePackage.objects.count(), 2)
        # 补建的包可正常校验
        for package in EvidencePackage.objects.all():
            self.assertTrue(self.verify_online(package.id)["ok"])
        # 已建链的处罚单再调 seal 接口不会重复建包
        self.seal(e1["penalty"]["id"], expect=200)
        self.assertEqual(EvidencePackage.objects.count(), 2)

    # ---------- 6. 导出确定性 + 中断重试 ----------
    def test_export_is_deterministic_and_retryable_after_failure(self):
        event, _ = self.make_event()
        package = self.seal(event["penalty"]["id"])

        first = self.export_package(package["id"])
        self.assertEqual(first.status_code, 200)
        second = self.export_package(package["id"])
        self.assertEqual(first.content, second.content)  # 字节级确定

        # 模拟导出中断（构建抛错）→ 重试恢复正常且字节一致
        with mock.patch("assessment.views.build_export_archive", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.client.get(f"/api/packages/{package['id']}/export/")
        third = self.export_package(package["id"])
        self.assertEqual(third.status_code, 200)
        self.assertEqual(third.content, first.content)
        self.assertTrue(verify_export_archive(third.content)["ok"])

    # ---------- 7. 跨地点同图候选不混入同一封存包 ----------
    def test_cross_location_identical_image_not_mixed_into_package(self):
        t = self.now
        # 网格1 首报立案
        p1 = self.upload_photo("scene_a_angle1", A1_LNG, A1_LAT, t - timedelta(hours=2), "首报")
        e1 = self.create_event(p1["id"])
        # 完全相同的图片误传到 3 公里外的网格2 → 人工判 different → 网格2 单独立案
        p3 = self.upload_photo("scene_a_elsewhere_copy", FAR_LNG, FAR_LAT, t - timedelta(hours=1), "误传")
        cand = self.client.get(f"/api/candidates/?photo={p3['id']}&status=pending").json()["results"][0]
        self.client.post(f"/api/candidates/{cand['id']}/decide/",
                         {"action": "different", "note": "坐标不符"}, format="json")
        e2 = self.create_event(p3["id"])

        pkg1 = self.seal(e1["penalty"]["id"])
        pkg2 = self.seal(e2["penalty"]["id"])

        # 网格1 的包：只有 p1 的文件；不含跨地点候选（那是针对 p3 的判定）
        self.assertEqual([p["photo_id"] for p in pkg1["manifest"]["photos"]], [p1["id"]])
        self.assertEqual(pkg1["manifest"]["candidate_decisions"], [])
        # 网格2 的包：只有 p3 的文件；候选判定只以 id 引用 p1，绝不带入 p1 的文件
        self.assertEqual([p["photo_id"] for p in pkg2["manifest"]["photos"]], [p3["id"]])
        self.assertEqual(len(pkg2["manifest"]["candidate_decisions"]), 1)
        decision = pkg2["manifest"]["candidate_decisions"][0]
        self.assertEqual(decision["matched_photo_id"], p1["id"])
        self.assertEqual(decision["status"], "different")

        # 导出物层面互证：两个包的 files/ 目录各自只有本事件照片
        archive1 = self.export_package(pkg1["id"]).content
        archive2 = self.export_package(pkg2["id"]).content
        names1 = self._zip_file_names(archive1)
        names2 = self._zip_file_names(archive2)
        self.assertEqual(len(names1), 1)
        self.assertEqual(len(names2), 1)
        self.assertNotEqual(next(iter(names1)), next(iter(names2)))
        self.assertTrue(self.verify_offline(archive1)["ok"])
        self.assertTrue(self.verify_offline(archive2)["ok"])

    @staticmethod
    def _zip_file_names(archive_bytes):
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
            return {n for n in zf.namelist() if "/files/" in n}


class ConcurrentSealTests(APITransactionTestCase):
    """并发封存：多线程同时请求同一处罚单，最终只能有一个活动包。"""

    def test_concurrent_seal_requests_single_active_package(self):
        now = timezone.now().replace(microsecond=0)
        grid = RoadGrid.objects.create(code="G1", name="人民东路一段", geom=json.dumps(GRID1))
        CleaningContract.objects.create(
            code="B-NEW", grid=grid, contractor_name="乙保洁公司",
            valid_from=now - timedelta(days=10), valid_to=now + timedelta(days=300),
        )
        photo = EvidencePhoto.objects.create(
            image=SimpleUploadedFile("a.png", scene_png_bytes("scene_a_angle1"), content_type="image/png"),
            phash="0" * 16,
            captured_at=now - timedelta(hours=2),
            location=f"POINT({A1_LNG} {A1_LAT})",
        )
        from assessment.services.events import create_event_from_photo

        event = create_event_from_photo(photo, category="litter", actor="并发测试")
        penalty_id = event.penalty.id

        results, errors = [], []

        def worker():
            try:
                package, _ = seal_penalty(PenaltyUnit.objects.get(pk=penalty_id), actor="并发")
                results.append(package.package_no)
            except Exception as exc:  # noqa: BLE001 - 测试里要收集一切异常
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        # 全部线程拿到同一个包，库里只有一个活动包
        self.assertEqual(len(set(results)), 1)
        packages = EvidencePackage.objects.filter(penalty_id=penalty_id)
        self.assertEqual(packages.count(), 1)
        self.assertEqual(packages.get().status, EvidencePackage.Status.SEALED)
