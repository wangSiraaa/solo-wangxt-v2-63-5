"""
证据封存包端到端测试（真实 PostgreSQL/PostGIS）。

验收点：
1. 正常封存可离线校验，并还原唯一处罚与完整证据；
2. 封存后新增更正/补拍/整改只产生显式关联的补充包，旧包不可变；
3. 重复封存与并发请求至多一个活动包；
4. 文件损坏/缺失时校验只标 verification_failed，原业务链仍可追溯、候选不自动合并；
5. 历史数据迁移幂等可续跑，不产生第二个活动包；
6. 导出中断后同 token 重试续作，最终只产出同一个 bundle；
7. 跨地点同图候选（照片分属不同事件）不会混入同一封存包；
8. pending 状态与 OpenAPI 覆盖。
"""
import io
import json
import os
import tarfile
import threading
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connections
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from assessment.mockimages import scene_png_bytes
from assessment.models import (
    CleaningContract,
    DuplicateCandidate,
    PenaltyUnit,
    PenaltyVersion,
    RoadGrid,
    SealExportJob,
    SealPackage,
    SealVerification,
    SealedFile,
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


class SealAPITests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.now = timezone.now().replace(microsecond=0)
        cls.grid1 = RoadGrid.objects.create(code="G1", name="网格一", geom=json.dumps(GRID1))
        cls.grid2 = RoadGrid.objects.create(code="G2", name="网格二(远)", geom=json.dumps(GRID2))
        CleaningContract.objects.create(
            code="C1", grid=cls.grid1, contractor_name="甲保洁公司",
            valid_from=cls.now - timedelta(days=400), valid_to=cls.now + timedelta(days=400),
        )
        CleaningContract.objects.create(
            code="C2", grid=cls.grid2, contractor_name="乙保洁公司",
            valid_from=cls.now - timedelta(days=400), valid_to=cls.now + timedelta(days=400),
        )

    def upload_photo(self, scene, lng, lat, captured_at, note=""):
        upload = SimpleUploadedFile(
            f"{scene}-{note or 'x'}.png", scene_png_bytes(scene), content_type="image/png"
        )
        resp = self.client.post(
            "/api/photos/",
            {"image": upload, "lng": lng, "lat": lat,
             "captured_at": captured_at.isoformat(), "note": note},
            format="multipart",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    def create_event(self, photo_id):
        resp = self.client.post(
            f"/api/photos/{photo_id}/create_event/", {"category": "litter"}, format="json"
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    def decide(self, cand_id, action, **extra):
        payload = {"action": action, "actor": "监督员-王", "note": action}
        payload.update(extra)
        resp = self.client.post(f"/api/candidates/{cand_id}/decide/", payload, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def pending_candidate(self, photo_id, matched_photo_id):
        resp = self.client.get(
            f"/api/candidates/?photo={photo_id}&status=pending&matched_photo={matched_photo_id}"
        )
        self.assertEqual(resp.status_code, 200)
        results = resp.json()["results"]
        self.assertEqual(len(results), 1)
        return results[0]

    def seal(self, **payload):
        body = {"actor": "监督员-王"}
        body.update(payload)
        resp = self.client.post("/api/seals/", body, format="json")
        return resp

    # ------------------------------------------------------------------
    def test_seal_offline_verify_and_restore_unique_penalty_full_evidence(self):
        t = self.now
        p1 = self.upload_photo("scene_a_angle1", A1_LNG, A1_LAT, t - timedelta(hours=2), "首报")
        # 先立案（attach 要求被匹配照片已关联事件），再挂接不同角度补拍
        event = self.create_event(p1["id"])
        p2 = self.upload_photo("scene_a_angle2", A2_LNG, A2_LAT, t - timedelta(hours=2), "补拍角度")
        cand = self.pending_candidate(p2["id"], p1["id"])
        self.decide(cand["id"], "attach", note="同一问题不同角度")
        penalty_id = event["penalty"]["id"]

        resp = self.seal(penalty=penalty_id)
        self.assertEqual(resp.status_code, 201, resp.content)
        sp1 = resp.json()
        self.assertEqual(sp1["status"], "sealed")
        self.assertTrue(sp1["is_active"])
        self.assertEqual(sp1["package_kind"], "initial")
        self.assertEqual(len(sp1["files"]), 2)
        for f in sp1["files"]:
            self.assertEqual(len(f["sha256"]), 64)
            self.assertFalse(f["missing_at_seal"])

        # 在线校验通过
        resp = self.client.post(f"/api/seals/{sp1['id']}/verify/", {"actor": "复核员"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["report"]["result"], "valid")
        self.assertEqual(resp.json()["report"]["checked_files"], 2)

        # 导出
        resp = self.client.post(
            f"/api/seals/{sp1['id']}/exports/",
            {"client_token": "tok-1", "actor": "复核员"}, format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        job = resp.json()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["total_files"], 2)
        self.assertEqual(job["done_files"], 2)

        # 下载 tar 并离线校验
        dl = self.client.get(f"/api/seals/{sp1['id']}/exports/{job['id']}/download/")
        self.assertEqual(dl.status_code, 200, dl.content)
        self.assertEqual(dl["X-Bundle-SHA256"], job["bundle_digest"])

        upload = SimpleUploadedFile("bundle.tar", dl.content, content_type="application/x-tar")
        resp = self.client.post(
            "/api/seals/offline-verify/", {"bundle": upload}, format="multipart"
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["report"]["result"], "valid")
        self.assertTrue(body["report"]["manifest_ok"])
        restored = body["restored"]
        self.assertEqual(
            restored["unique_penalty"]["penalty_no"], event["penalty"]["penalty_no"]
        )
        self.assertEqual(len(restored["photos"]), 2)  # 完整证据
        self.assertEqual(restored["unique_penalty"]["current_version"]["version_no"], 1)
        self.assertEqual(restored["event"]["event_no"], event["event_no"])
        # 唯一处罚
        self.assertEqual(PenaltyUnit.objects.filter(event_id=event["id"]).count(), 1)

        # 包内自带纯标准库校验器可独立运行
        with tarfile.open(fileobj=io.BytesIO(dl.content)) as tar:
            names = tar.getnames()
        self.assertTrue(any(n.endswith("verify_offline.py") for n in names))
        self.assertTrue(any(n.endswith("manifest.json") for n in names))

        # 在完全脱离 Django 的子进程里运行自带校验器：退出码 0
        import subprocess
        import sys as _sys
        import tempfile

        with tempfile.TemporaryDirectory() as tmpd:
            with tarfile.open(fileobj=io.BytesIO(dl.content)) as tar:
                tar.extractall(tmpd)
            verifier = os.path.join(tmpd, "verify_offline.py")
            proc = subprocess.run(
                [_sys.executable, verifier, tmpd],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("RESULT: PASS", proc.stdout)

            # 篡改一个证据文件后：退出码 1 且报 MISMATCH（业务系统无感知）
            target = None
            for rootd, _, files in os.walk(os.path.join(tmpd, "files")):
                for name in files:
                    target = os.path.join(rootd, name)
            self.assertIsNotNone(target)
            with open(target, "r+b") as fh:
                fh.seek(0)
                fh.write(b"X")
            proc = subprocess.run(
                [_sys.executable, verifier, tmpd],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(proc.returncode, 1, proc.stdout)
            self.assertIn("MISMATCH", proc.stdout)

    # ------------------------------------------------------------------
    def test_correction_and_rectification_only_create_supplement_package(self):
        t = self.now
        p1 = self.upload_photo("scene_a_angle1", A1_LNG, A1_LAT, t - timedelta(hours=2), "首报")
        event = self.create_event(p1["id"])
        penalty_id = event["penalty"]["id"]

        r = self.seal(penalty=penalty_id, client_token="s1")
        self.assertEqual(r.status_code, 201)
        sp1 = r.json()
        old_digest = sp1["manifest_digest"]

        # 重复封存（无变化）→ 同一个活动包，不新建
        r = self.seal(penalty=penalty_id)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("Seal-Created"), "0")
        self.assertEqual(r.json()["id"], sp1["id"])
        self.assertEqual(SealPackage.objects.filter(penalty_id=penalty_id).count(), 1)

        # 人工更正（追加处罚版本）后再封存 → 补充包
        resp = self.client.post(
            f"/api/penalties/{penalty_id}/correct/",
            {"points": "1.5", "reason": "核减", "actor": "监督员-王"}, format="json",
        )
        self.assertEqual(resp.status_code, 201)

        r = self.seal(penalty=penalty_id, note="更正后补充封存")
        self.assertEqual(r.status_code, 201, r.content)
        sp2 = r.json()
        self.assertEqual(sp2["package_kind"], "supplement")
        self.assertEqual(sp2["parent"], sp1["id"])
        self.assertTrue(sp2["is_active"])
        self.assertNotEqual(sp2["manifest_digest"], old_digest)

        # 旧包未被改动：仍可读取，状态 supplemented、非活动
        old = SealPackage.objects.get(pk=sp1["id"])
        self.assertFalse(old.is_active)
        self.assertEqual(old.status, SealPackage.Status.SUPPLEMENTED)
        self.assertEqual(old.manifest_digest, old_digest)

        # 旧包 manifest 内容里处罚仍是更正前的 2.0
        old_current = old.manifest["content"]["penalty"]["points"]
        self.assertEqual(old_current, "2.0")
        self.assertEqual(sp2["manifest"]["content"]["penalty"]["points"], "1.5")
        self.assertEqual(len(sp2["manifest"]["content"]["versions"]), 2)

        # 整改后再封存 → 又一个补充包；整改照片入包
        rp = self.upload_photo("scene_a_repost", A1_LNG, A1_LAT, t - timedelta(minutes=5), "整改照")
        resp = self.client.post(
            f"/api/events/{event['id']}/rectify/",
            {"actor": "班组长", "note": "已清理", "photo_id": rp["id"],
             "now": (t - timedelta(minutes=4)).isoformat()},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)

        r = self.client.post(
            f"/api/seals/{sp2['id']}/supplement/",
            {"actor": "监督员-王", "note": "整改补充"}, format="json",
        )
        self.assertEqual(r.status_code, 201, r.content)
        sp3 = r.json()
        self.assertEqual(sp3["package_kind"], "supplement")
        self.assertEqual(sp3["parent"], sp2["id"])
        roles = {f["role"] for f in sp3["files"]}
        self.assertIn("rectification", roles)
        # 链：sp1 -> sp2 -> sp3
        chain = self.client.get(f"/api/seals/{sp3['id']}/lineage/").json()["lineage"]
        self.assertEqual([c["package_no"] for c in chain],
                         [sp1["package_no"], sp2["package_no"], sp3["package_no"]])

        # 显式替代：旧包 superseded，新包 replaces 指向
        r = self.client.post(
            f"/api/seals/{sp3['id']}/replace/",
            {"actor": "监督员-王", "note": "封存口径更正替代"}, format="json",
        )
        # 内容未变时替代也不重复造活动包——先制造一处变化（复核锁定）
        self.assertEqual(r.status_code, 200)
        self.client.post(f"/api/penalties/{penalty_id}/review/",
                         {"approved": True, "actor": "复核员"}, format="json")
        r = self.client.post(
            f"/api/seals/{sp3['id']}/replace/",
            {"actor": "监督员-王", "note": "复核锁定后替代"}, format="json",
        )
        self.assertEqual(r.status_code, 201, r.content)
        sp4 = r.json()
        self.assertEqual(sp4["package_kind"], "replacement")
        self.assertEqual(sp4["replaces"], sp3["id"])
        self.assertEqual(SealPackage.objects.get(pk=sp3["id"]).status,
                         SealPackage.Status.SUPERSEDED)

    # ------------------------------------------------------------------
    def test_duplicate_seal_single_active_package(self):
        t = self.now
        p1 = self.upload_photo("scene_c_bins", A1_LNG, A1_LAT, t, "重复封存")
        event = self.create_event(p1["id"])
        penalty_id = event["penalty"]["id"]
        r1 = self.seal(penalty=penalty_id, client_token="dup")
        r2 = self.seal(penalty=penalty_id, client_token="dup")
        self.assertEqual(r1.json()["id"], r2.json()["id"])
        # 无 token 的内容未变重复封存同样复用
        r3 = self.seal(penalty=penalty_id)
        self.assertEqual(r3.json()["id"], r1.json()["id"])
        self.assertEqual(
            SealPackage.objects.filter(penalty_id=penalty_id, is_active=True).count(), 1
        )

    # ------------------------------------------------------------------
    def test_tampered_and_missing_files_mark_failed_but_chain_intact(self):
        t = self.now
        p1 = self.upload_photo("scene_a_angle1", A1_LNG, A1_LAT, t, "首报")
        event = self.create_event(p1["id"])
        penalty_id = event["penalty"]["id"]
        sp = self.seal(penalty=penalty_id).json()

        photo = self._photo_model(p1["id"])
        path = photo.image.path

        # 1) 篡改文件
        with open(path, "wb") as fh:
            fh.write(b"tampered-bytes-not-an-image")

        resp = self.client.post(f"/api/seals/{sp['id']}/verify/", {}, format="json")
        self.assertEqual(resp.status_code, 409)
        report = resp.json()["report"]
        self.assertEqual(report["result"], "invalid")
        self.assertEqual(len(report["mismatch_files"]), 1)
        self.assertEqual(SealPackage.objects.get(pk=sp["id"]).status,
                         SealPackage.Status.VERIFICATION_FAILED)
        verification = SealVerification.objects.get(pk=resp.json()["verification_id"])
        self.assertEqual(verification.result, "invalid")

        # 关键：业务链未受影响——处罚/事件/候选没有被改
        penalty = PenaltyUnit.objects.get(pk=penalty_id)
        self.assertEqual(str(penalty.points), "2.0")
        self.assertEqual(PenaltyVersion.objects.filter(penalty_id=penalty_id).count(), 1)
        # 追溯详情仍可读取
        detail = self.client.get(f"/api/penalties/{penalty_id}/").json()
        self.assertEqual(detail["penalty_no"], event["penalty"]["penalty_no"])
        self.assertEqual(len(detail["versions"]), 1)
        # 篡改没有触发任何自动合并
        self.assertFalse(
            DuplicateCandidate.objects.exclude(status="pending")
            .filter(photo_id=p1["id"]).exclude(status="different").exists()
        )

        # 旧包 manifest 里封存的摘要仍在（不可变）
        sealed_sha = SealedFile.objects.get(package_id=sp["id"]).sha256
        self.assertEqual(len(sealed_sha), 64)

        # 2) 文件恢复（写回正确字节）→ 再校验通过，活动包回到 sealed
        with open(path, "wb") as fh:
            fh.write(scene_png_bytes("scene_a_angle1"))
        resp = self.client.post(f"/api/seals/{sp['id']}/verify/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["report"]["result"], "valid")
        self.assertEqual(SealPackage.objects.get(pk=sp["id"]).status,
                         SealPackage.Status.SEALED)

        # 3) 文件缺失
        os.remove(path)
        resp = self.client.post(f"/api/seals/{sp['id']}/verify/", {}, format="json")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["report"]["missing_files"],
                         [SealedFile.objects.get(package_id=sp["id"]).rel_path])
        # 原链仍可追溯（manifest 不依赖磁盘文件）
        detail = self.client.get(f"/api/penalties/{penalty_id}/").json()
        self.assertEqual(detail["event"]["event_no"], event["event_no"])
        self.assertEqual(PenaltyUnit.objects.count(), 1)

    def _photo_model(self, photo_id):
        from assessment.models import EvidencePhoto

        return EvidencePhoto.objects.get(pk=photo_id)

    # ------------------------------------------------------------------
    def test_missing_file_at_seal_time_still_seals_and_traces(self):
        t = self.now
        p1 = self.upload_photo("scene_c_bins", A1_LNG, A1_LAT, t, "先丢文件再封存")
        event = self.create_event(p1["id"])
        photo = self._photo_model(p1["id"])
        os.remove(photo.image.path)

        sp = self.seal(penalty=event["penalty"]["id"]).json()
        self.assertEqual(sp["status"], "sealed")
        self.assertTrue(sp["missing_files"])
        sf = sp["files"][0]
        self.assertTrue(sf["missing_at_seal"])
        self.assertEqual(sf["sha256"], "")
        # 追溯仍完整
        self.assertEqual(sp["manifest"]["content"]["subject"]["penalty_no"],
                         event["penalty"]["penalty_no"])

        # 带缺失文件的封存包导出后仍可离线校验通过（占位文件不算异常）
        r = self.client.post(
            f"/api/seals/{sp['id']}/exports/",
            {"client_token": "missing-1"}, format="json",
        )
        self.assertEqual(r.status_code, 201, r.content)
        dl = self.client.get(
            f"/api/seals/{sp['id']}/exports/{r.json()['id']}/download/"
        )
        upload = SimpleUploadedFile("m.tar", dl.content, content_type="application/x-tar")
        r = self.client.post("/api/seals/offline-verify/",
                             {"bundle": upload}, format="multipart")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["report"]["result"], "valid")

    # ------------------------------------------------------------------
    def test_cross_location_duplicate_candidate_not_mixed_into_package(self):
        t = self.now
        # 事件一：网格1
        p1 = self.upload_photo("scene_a_angle1", A1_LNG, A1_LAT, t - timedelta(hours=2), "首报")
        e1 = self.create_event(p1["id"])
        # 跨地点同图（逐像素相同）：网格2
        p_far = self.upload_photo("scene_a_elsewhere_copy", FAR_LNG, FAR_LAT,
                                  t - timedelta(hours=1), "误传同图")
        far_cand = self.pending_candidate(p_far["id"], p1["id"])
        self.decide(far_cand["id"], "different", note="跨地点不合并")
        e2 = self.create_event(p_far["id"])

        sp1 = self.seal(penalty=e1["penalty"]["id"]).json()
        sp2 = self.seal(penalty=e2["penalty"]["id"]).json()

        # 每个包只含本事件照片
        self.assertEqual({f["photo"] for f in sp1["files"]}, {p1["id"]})
        self.assertEqual({f["photo"] for f in sp2["files"]}, {p_far["id"]})
        # 跨地点候选（双方不同事件）不进入任何包
        self.assertEqual(sp1["manifest"]["content"]["candidates"], [])
        self.assertEqual(sp2["manifest"]["content"]["candidates"], [])

        # 而同事件内的 attach 候选应进入包
        p2 = self.upload_photo("scene_a_angle2", A2_LNG, A2_LAT, t - timedelta(hours=2), "角度2")
        cand = self.pending_candidate(p2["id"], p1["id"])
        self.decide(cand["id"], "attach", note="同一问题")
        sp3 = self.seal(penalty=e1["penalty"]["id"], note="补拍后补充").json()
        cands = sp3["manifest"]["content"]["candidates"]
        self.assertEqual(len(cands), 1)
        self.assertEqual({cands[0]["photo_id"], cands[0]["matched_photo_id"]},
                         {p1["id"], p2["id"]})

    # ------------------------------------------------------------------
    def test_pending_then_finalize_flow(self):
        t = self.now
        p1 = self.upload_photo("scene_c_bins", A1_LNG, A1_LAT, t, "pending")
        event = self.create_event(p1["id"])

        resp = self.seal(penalty=event["penalty"]["id"], finalize=False)
        self.assertEqual(resp.status_code, 202, resp.content)
        sp = resp.json()
        self.assertEqual(sp["status"], "pending")
        self.assertIsNone(sp["manifest"])

        # pending 期间重复请求不开第二个活动包
        resp2 = self.seal(penalty=event["penalty"]["id"], finalize=False)
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.json()["id"], sp["id"])

        resp = self.client.post(f"/api/seals/{sp['id']}/finalize/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["status"], "sealed")
        self.assertEqual(len(resp.json()["manifest_digest"]), 64)

        # 已封存再 finalize 幂等
        resp = self.client.post(f"/api/seals/{sp['id']}/finalize/", {}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "sealed")


class SealMigrationTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.now = timezone.now().replace(microsecond=0)
        cls.grid = RoadGrid.objects.create(code="G1", name="网格", geom=json.dumps(GRID1))
        CleaningContract.objects.create(
            code="C1", grid=cls.grid, contractor_name="甲公司",
            valid_from=cls.now - timedelta(days=400), valid_to=cls.now + timedelta(days=400),
        )

    def _make_event(self, note):
        upload = SimpleUploadedFile(
            f"{note}.png", scene_png_bytes("scene_c_bins"), content_type="image/png"
        )
        r = self.client.post(
            "/api/photos/",
            {"image": upload, "lng": A1_LNG, "lat": A1_LAT,
             "captured_at": self.now.isoformat(), "note": note},
            format="multipart",
        )
        self.assertEqual(r.status_code, 201)
        r = self.client.post(f"/api/photos/{r.json()['id']}/create_event/",
                             {"category": "litter"}, format="json")
        self.assertEqual(r.status_code, 201)
        return r.json()

    def test_legacy_migration_idempotent_and_single_active(self):
        e1 = self._make_event("legacy-1")
        e2 = self._make_event("legacy-2")
        self.assertEqual(PenaltyUnit.objects.count(), 2)
        self.assertEqual(SealPackage.objects.count(), 0)

        # 分批迁移（limit=1），验证可续跑
        r = self.client.post("/api/seals/migrate/", {"limit": 1}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["migrated"], 1)
        r = self.client.post("/api/seals/migrate/", {"limit": 1}, format="json")
        self.assertEqual(r.json()["migrated"], 1)
        # 再次迁移：没有缺口（查询本身已排除有包的处罚）
        r = self.client.post("/api/seals/migrate/", {}, format="json")
        self.assertEqual(r.json(), {"migrated": 0, "skipped": 0})

        self.assertEqual(SealPackage.objects.count(), 2)
        for sp in SealPackage.objects.all():
            self.assertTrue(sp.legacy_migrated)
            self.assertEqual(sp.status, "sealed")
            self.assertTrue(sp.is_active)
            self.assertEqual(sp.package_kind, "initial")
        self.assertEqual(SealPackage.objects.filter(is_active=True).count(), 2)

        # 迁移包同样可在线校验
        first = SealPackage.objects.first()
        resp = self.client.post(f"/api/seals/{first.id}/verify/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)

        # 迁移后新增更正 → 只产生补充包，迁移包转 supplemented 不被改写
        e1_penalty = e1["penalty"]["id"]
        original = SealPackage.objects.get(penalty_id=e1_penalty)
        digest_before = original.manifest_digest
        self.client.post(
            f"/api/penalties/{e1_penalty}/correct/",
            {"points": "0.5", "reason": "迁移后更正", "actor": "x"}, format="json",
        )
        r = self.client.post("/api/seals/",
                             {"penalty": e1_penalty, "actor": "x"}, format="json")
        self.assertEqual(r.status_code, 201)
        original.refresh_from_db()
        self.assertEqual(original.manifest_digest, digest_before)
        self.assertFalse(original.is_active)
        self.assertEqual(original.status, "supplemented")


class SealExportResumeTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.now = timezone.now().replace(microsecond=0)
        cls.grid = RoadGrid.objects.create(code="G1", name="网格", geom=json.dumps(GRID1))
        CleaningContract.objects.create(
            code="C1", grid=cls.grid, contractor_name="甲公司",
            valid_from=cls.now - timedelta(days=400), valid_to=cls.now + timedelta(days=400),
        )

    def test_export_interrupt_and_resume_single_bundle(self):
        # 两张照片 → 2 个文件
        up1 = SimpleUploadedFile("a.png", scene_png_bytes("scene_a_angle1"),
                                 content_type="image/png")
        r = self.client.post(
            "/api/photos/",
            {"image": up1, "lng": A1_LNG, "lat": A1_LAT,
             "captured_at": self.now.isoformat()}, format="multipart",
        )
        p1 = r.json()
        event = self.client.post(f"/api/photos/{p1['id']}/create_event/",
                                 {"category": "litter"}, format="json").json()
        up2 = SimpleUploadedFile("b.png", scene_png_bytes("scene_a_angle2"),
                                 content_type="image/png")
        r = self.client.post(
            "/api/photos/",
            {"image": up2, "lng": A2_LNG, "lat": A2_LAT,
             "captured_at": self.now.isoformat()}, format="multipart",
        )
        p2 = r.json()
        cand = self.client.get(
            f"/api/candidates/?photo={p2['id']}&status=pending&matched_photo={p1['id']}"
        ).json()["results"][0]
        self.client.post(f"/api/candidates/{cand['id']}/decide/",
                         {"action": "attach", "actor": "w"}, format="json")

        sp = self.client.post("/api/seals/",
                              {"penalty": event["penalty"]["id"], "actor": "w"},
                              format="json").json()
        self.assertEqual(sp["status"], "sealed")

        # 第一次导出：复制 1 个文件后中断（202 + building，进度保留）
        r = self.client.post(
            f"/api/seals/{sp['id']}/exports/",
            {"client_token": "resume-1", "fail_after": 1}, format="json",
        )
        self.assertEqual(r.status_code, 202, r.content)
        job = r.json()
        self.assertEqual(job["status"], "building")
        self.assertEqual(job["done_files"], 1)
        self.assertEqual(job["total_files"], 2)
        self.assertEqual(job["bundle_digest"], "")

        # 同 token 重试导出（create 端点幂等返回 building 任务，不新建）
        r = self.client.post(
            f"/api/seals/{sp['id']}/exports/",
            {"client_token": "resume-1"}, format="json",
        )
        self.assertEqual(r.status_code, 201, r.content)
        job = r.json()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["done_files"], 2)

        # 只有一个导出任务、一个 bundle
        self.assertEqual(SealExportJob.objects.filter(package_id=sp["id"]).count(), 1)
        self.assertTrue(os.path.isfile(job["bundle_path"]))

        # 同 token 再跑：completed 直接复用，不重建
        r = self.client.post(
            f"/api/seals/{sp['id']}/exports/",
            {"client_token": "resume-1"}, format="json",
        )
        self.assertEqual(r.json()["id"], job["id"])
        self.assertEqual(r.json()["bundle_digest"], job["bundle_digest"])

        # 续作完成的包离线校验通过
        dl = self.client.get(f"/api/seals/{sp['id']}/exports/{job['id']}/download/")
        self.assertEqual(dl.status_code, 200)
        upload = SimpleUploadedFile("b.tar", dl.content, content_type="application/x-tar")
        r = self.client.post("/api/seals/offline-verify/",
                             {"bundle": upload}, format="multipart")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["report"]["result"], "valid")

        # 也可通过 seal-exports 任务端点续作（另一任务中断后 run）
        r = self.client.post(
            f"/api/seals/{sp['id']}/exports/",
            {"client_token": "resume-2", "fail_after": 1}, format="json",
        )
        job2 = r.json()
        r = self.client.post(f"/api/seal-exports/{job2['id']}/run/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], "completed")


class SealConcurrencyTests(TransactionTestCase):
    """真正的跨连接并发：TransactionTestCase 不包裹测试事务。"""

    reset_sequences = True

    def setUp(self):
        now = timezone.now().replace(microsecond=0)
        grid = RoadGrid.objects.create(code="G1", name="网格", geom=json.dumps(GRID1))
        CleaningContract.objects.create(
            code="C1", grid=grid, contractor_name="甲公司",
            valid_from=now - timedelta(days=400), valid_to=now + timedelta(days=400),
        )
        upload = SimpleUploadedFile("c.png", scene_png_bytes("scene_c_bins"),
                                    content_type="image/png")
        client = APIClient()
        p = client.post(
            "/api/photos/",
            {"image": upload, "lng": A1_LNG, "lat": A1_LAT,
             "captured_at": now.isoformat()}, format="multipart",
        ).json()
        event = client.post(f"/api/photos/{p['id']}/create_event/",
                            {"category": "litter"}, format="json").json()
        self.penalty_id = event["penalty"]["id"]

    def test_parallel_seals_never_create_two_active_packages(self):
        errors = []
        results = []
        barrier = threading.Barrier(6)

        def worker(i):
            client = None
            try:
                from rest_framework.test import APIClient

                client = APIClient()
                barrier.wait()
                resp = client.post(
                    "/api/seals/",
                    {"penalty": self.penalty_id, "actor": f"w{i}",
                     "client_token": f"tok-{i}"},
                    format="json",
                )
                if resp.status_code not in (200, 201):
                    errors.append(f"status={resp.status_code} body={resp.content!r}")
                else:
                    results.append(resp.json()["id"])
            except Exception as exc:  # pragma: no cover
                errors.append(repr(exc))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 6)
        self.assertEqual(set(results), {results[0]})  # 全部落到同一个包
        self.assertEqual(
            SealPackage.objects.filter(penalty_id=self.penalty_id).count(), 1
        )
        self.assertEqual(
            SealPackage.objects.filter(penalty_id=self.penalty_id, is_active=True).count(), 1
        )


class SealSchemaTests(APITestCase):
    def test_openapi_covers_seal_endpoints(self):
        resp = self.client.get("/api/schema/", HTTP_ACCEPT="application/vnd.oai.openapi+json")
        self.assertEqual(resp.status_code, 200)
        schema = json.loads(resp.content)
        for path in [
            "/api/seals/",
            "/api/seals/{id}/",
            "/api/seals/{id}/finalize/",
            "/api/seals/{id}/supplement/",
            "/api/seals/{id}/replace/",
            "/api/seals/{id}/verify/",
            "/api/seals/{id}/exports/",
            "/api/seals/{id}/exports/{job_id}/download/",
            "/api/seals/{id}/lineage/",
            "/api/seals/offline-verify/",
            "/api/seals/migrate/",
            "/api/seal-exports/",
            "/api/seal-exports/{id}/",
            "/api/seal-exports/{id}/run/",
        ]:
            self.assertIn(path, schema["paths"], path)
