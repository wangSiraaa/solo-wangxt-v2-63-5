"""
端到端 API 测试（真实 PostgreSQL/PostGIS）。

覆盖需求：
1. 同图跨地点误传 —— pHash 只产生候选，人工按位置判定为“不同”，分别立案分别扣分；
2. 同地点复发 —— 先整改，复发照片另立新事件/新处罚单元（候选标 recurrence）；
3. 同一问题不同角度 —— 人工挂接，不重复扣分；重复整改回调 409；
4. 扣分归属按事件发生时的合同（老事件归甲，新事件归乙），与录入时间无关；
5. 逾期升级基于注入时钟，可重放、幂等；
6. 复核通过锁定版本；更正只能追加；历史版本不可变；写接口 405；
7. 每笔扣分可追溯到唯一处罚单元与证据；OpenAPI schema 可生成。
"""
import json
from datetime import timedelta
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APITestCase

from assessment.mockimages import scene_png_bytes
from assessment.models import (
    CleaningContract,
    DuplicateCandidate,
    PenaltyUnit,
    PenaltyVersion,
    ProblemEvent,
    RoadGrid,
)
from assessment.services.phash import compute_phash_hex, hamming_distance

GRID1 = {
    "type": "Polygon",
    "coordinates": [[
        [121.4700, 31.2300], [121.4800, 31.2300],
        [121.4800, 31.2400], [121.4700, 31.2400],
        [121.4700, 31.2300],
    ]],
}
# 远处的第二个网格（跨地点误传落点）
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
FAR_LNG, FAR_LAT = 121.5100, 31.2601  # 落在 GRID2


class AssessmentFlowTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.now = timezone.now().replace(microsecond=0)
        cls.grid1 = RoadGrid.objects.create(code="G1", name="人民东路一段", geom=json.dumps(GRID1))
        cls.grid2 = RoadGrid.objects.create(code="G2", name="人民东路二段(远)", geom=json.dumps(GRID2))
        # 网格1：甲公司去年~昨天；乙公司 昨天~明年（责任以发生时刻为准）
        CleaningContract.objects.create(
            code="A-OLD", grid=cls.grid1, contractor_name="甲保洁公司",
            valid_from=cls.now - timedelta(days=400), valid_to=cls.now - timedelta(days=1),
        )
        CleaningContract.objects.create(
            code="B-NEW", grid=cls.grid1, contractor_name="乙保洁公司",
            valid_from=cls.now - timedelta(days=1), valid_to=cls.now + timedelta(days=300),
        )
        CleaningContract.objects.create(
            code="C-FAR", grid=cls.grid2, contractor_name="丙保洁公司",
            valid_from=cls.now - timedelta(days=400), valid_to=cls.now + timedelta(days=300),
        )

    # ---------- 辅助 ----------
    def upload_photo(self, scene, lng, lat, captured_at, note=""):
        data = scene_png_bytes(scene)
        upload = SimpleUploadedFile(f"{scene}.png", data, content_type="image/png")
        resp = self.client.post(
            "/api/photos/",
            {
                "image": upload,
                "lng": lng, "lat": lat,
                "captured_at": captured_at.isoformat(),
                "note": note,
            },
            format="multipart",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    def pending_candidate(self, photo_id, matched_photo_id):
        resp = self.client.get(
            f"/api/candidates/?photo={photo_id}&status=pending&matched_photo={matched_photo_id}"
        )
        self.assertEqual(resp.status_code, 200)
        results = resp.json()["results"]
        self.assertEqual(len(results), 1)
        return results[0]

    def create_event(self, photo_id, **extra):
        payload = {"category": "litter"}
        payload.update(extra)
        resp = self.client.post(f"/api/photos/{photo_id}/create_event/", payload, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    def test_phash_known_distances(self):
        h1 = compute_phash_hex_bytes(scene_png_bytes("scene_a_angle1"))
        h2 = compute_phash_hex_bytes(scene_png_bytes("scene_a_angle2"))
        hc = compute_phash_hex_bytes(scene_png_bytes("scene_a_elsewhere_copy"))
        hr = compute_phash_hex_bytes(scene_png_bytes("scene_a_repost"))
        hb = compute_phash_hex_bytes(scene_png_bytes("scene_c_bins"))
        self.assertEqual(hamming_distance(h1, hc), 0)       # 误传文件逐像素相同
        self.assertLessEqual(hamming_distance(h1, h2), 5)   # 不同角度仍候选
        self.assertLessEqual(hamming_distance(h1, hr), 5)   # 复发仍候选
        self.assertGreater(hamming_distance(h1, hb), 5)     # 不同现场不候选

    def test_full_flow(self):
        t = self.now
        # 1) 首次发现并立案
        p1 = self.upload_photo("scene_a_angle1", A1_LNG, A1_LAT, t - timedelta(hours=2), "首报")
        e1 = self.create_event(p1["id"])
        self.assertEqual(e1["contractor_name"], "乙保洁公司")  # 发生在乙的区间
        pn1 = e1["penalty"]["penalty_no"]
        self.assertEqual(len(e1["penalty"]["versions"]), 1)

        # 2) 不同角度补拍：pHash 产生候选（与位置无关，即便如此这里位置也很近）
        p2 = self.upload_photo("scene_a_angle2", A2_LNG, A2_LAT, t - timedelta(hours=2), "换角度")
        cand = self.pending_candidate(p2["id"], p1["id"])
        resp = self.client.post(f"/api/candidates/{cand['id']}/decide/",
                                {"action": "attach", "actor": "监督员-王", "note": "同一堆垃圾"},
                                format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["status"], "confirmed_duplicate")
        p2_detail = self.client.get(f"/api/photos/{p2['id']}/").json()
        self.assertEqual(p2_detail["event"], e1["id"])
        # 关键：挂接不产生新事件、不产生新扣分
        self.assertEqual(ProblemEvent.objects.count(), 1)
        self.assertEqual(PenaltyUnit.objects.count(), 1)

        # 3) 同图跨地点误传：完全相同的文件、坐标在 3 公里外的网格2
        p3 = self.upload_photo("scene_a_elsewhere_copy", FAR_LNG, FAR_LAT, t - timedelta(hours=1),
                               "巡查APP误传了相册旧图")
        # pHash 照样报候选——证明候选生成阶段刻意不使用位置
        far_cand = self.pending_candidate(p3["id"], p1["id"])
        self.assertLessEqual(far_cand["hamming_distance"], 5)
        resp = self.client.post(f"/api/candidates/{far_cand['id']}/decide/",
                                {"action": "different", "actor": "监督员-王",
                                 "note": "像素相同但坐标在网格2，不能合并"},
                                format="json")
        self.assertEqual(resp.json()["status"], "different")
        # 监督员核实网格2确实有该问题 → 对误传照片单独立案 → 归丙公司，独立处罚
        e2 = self.create_event(p3["id"], note="网格2现场核实属实")
        self.assertEqual(e2["contractor_name"], "丙保洁公司")
        self.assertNotEqual(e2["id"], e1["id"])
        self.assertEqual(PenaltyUnit.objects.count(), 2)
        self.assertNotEqual(e2["penalty"]["penalty_no"], pn1)

        # 4) 整改回调 + 重复回调幂等拒绝
        resp = self.client.post(f"/api/events/{e1['id']}/rectify/",
                                {"actor": "乙班组长", "note": "已清理",
                                 "now": (t - timedelta(minutes=30)).isoformat()},
                                format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["event"]["status"], "rectified")
        resp = self.client.post(f"/api/events/{e1['id']}/rectify/",
                                {"actor": "乙班组长", "note": "重复回调"}, format="json")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(PenaltyUnit.objects.count(), 2)  # 整改不改变扣分

        # 5) 同一位置复发：新照片产生候选，人工判定 create_new → recurrence + 新事件
        p4 = self.upload_photo("scene_a_repost", A1_LNG, A1_LAT, t - timedelta(minutes=10),
                               "整改后又出现垃圾")
        rec_cand = self.pending_candidate(p4["id"], p1["id"])
        resp = self.client.post(f"/api/candidates/{rec_cand['id']}/decide/",
                                {"action": "create_new", "actor": "监督员-王",
                                 "note": "原事件已整改，复发另立案"},
                                format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["status"], "recurrence")
        self.assertEqual(ProblemEvent.objects.count(), 3)
        self.assertEqual(PenaltyUnit.objects.count(), 3)
        e3 = self.client.get(f"/api/photos/{p4['id']}/").json()
        self.assertIsNotNone(e3["event"])
        self.assertNotEqual(e3["event"], e1["id"])
        e3_detail = self.client.get(f"/api/events/{e3['event']}/").json()
        self.assertEqual(e3_detail["status"], "open")
        self.assertEqual(e3_detail["contractor_name"], "乙保洁公司")
        # 原事件证据与复发事件证据互不串扰
        self.assertEqual({ph["id"] for ph in e3_detail["photos"]}, {p4["id"]})
        e1_detail = self.client.get(f"/api/events/{e1['id']}/").json()
        self.assertEqual({ph["id"] for ph in e1_detail["photos"]}, {p1["id"], p2["id"]})

        # 6) 归属按发生时间而非录入时间：刚刚才录入，但发生在 200 天前 → 甲公司
        p5 = self.upload_photo("scene_c_bins", 121.4760, 31.2360, t - timedelta(days=200),
                               "历史遗留照片补录")
        self.assertEqual(self.client.get("/api/candidates/?photo={}".format(p5["id"])).json()["count"], 0)
        e4 = self.create_event(p5["id"], description="补录的老问题")
        self.assertEqual(e4["contractor_name"], "甲保洁公司")
        self.assertEqual(e4["contract"], e4["penalty"]["contract"])

        # 7) 无生效合同 → 422，不允许产生无归属扣分
        p6 = self.upload_photo("scene_c_bins", 10.0, 10.0, t - timedelta(hours=1), "海里")
        resp = self.client.post(f"/api/photos/{p6['id']}/create_event/",
                                {"category": "litter"}, format="json")
        self.assertEqual(resp.status_code, 422)

        # 8) 证据保全：照片不可改删；处罚/版本只读
        self.assertEqual(self.client.delete(f"/api/photos/{p1['id']}/").status_code, 405)
        self.assertEqual(self.client.patch(f"/api/penalties/{e1['penalty']['id']}/",
                                           {"points": 0}, format="json").status_code, 405)
        self.assertEqual(self.client.delete("/api/penalty-versions/1/").status_code, 405)

        # 9) 追溯：每个处罚单号唯一，都能追到事件/承包商/版本/证据
        penalties = self.client.get("/api/penalties/").json()["results"]
        numbers = [p["penalty_no"] for p in penalties]
        self.assertEqual(len(numbers), len(set(numbers)))
        p1_full = next(p for p in penalties if p["penalty_no"] == pn1)
        self.assertEqual(p1_full["contractor_name"], "乙保洁公司")
        self.assertEqual(p1_full["event"]["event_no"], e1["event_no"])
        evidence_ids = {ph["id"] for ph in
                        self.client.get(f"/api/events/{e1['id']}/").json()["photos"]}
        self.assertEqual(evidence_ids, {p1["id"], p2["id"]})
        # 同一问题只扣一次：P1 始终只有初版
        self.assertEqual(len(p1_full["versions"]), 1)

    def test_escalation_with_injected_clock_and_locked_versions(self):
        t = self.now
        # 事件发生在 base 时刻（SLA 24h）
        photo = self.upload_photo("scene_c_bins", 121.4770, 31.2370, t, "逾期测试")
        event = self.create_event(photo["id"])
        penalty_id = event["penalty"]["id"]

        def run(now):
            return self.client.post("/api/escalations/run/", {"now": now.isoformat()}, format="json")

        # 注入时钟：SLA 内不升级
        self.assertEqual(run(t + timedelta(hours=10)).json()["created_count"], 0)
        # 逾期 25h → L1
        r = run(t + timedelta(hours=25))
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["created_count"], 1)
        # 同一注入时刻重放 → 幂等
        self.assertEqual(run(t + timedelta(hours=25)).json()["created_count"], 0)
        # 逾期 50h → L2（不是再加两级，只升到当前应处等级）
        self.assertEqual(run(t + timedelta(hours=50)).json()["created_count"], 1)

        penalty = self.client.get(f"/api/penalties/{penalty_id}/").json()
        self.assertEqual(penalty["escalation_level"], 2)
        self.assertEqual([v["kind"] for v in penalty["versions"]],
                         ["initial", "escalation", "escalation"])
        self.assertEqual([float(v["points"]) for v in penalty["versions"]], [2.0, 3.0, 4.0])
        self.assertEqual(penalty["status"], "draft")

        # 复核通过 → 锁定 v3
        r = self.client.post(f"/api/penalties/{penalty_id}/review/",
                             {"approved": True, "actor": "复核员-李"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "locked")
        self.assertEqual(r.json()["locked_version_no"], 3)
        # 重复复核同一锁定版本 → 409
        r = self.client.post(f"/api/penalties/{penalty_id}/review/",
                             {"approved": True, "actor": "复核员-李"}, format="json")
        self.assertEqual(r.status_code, 409)

        # 更正只能追加：v4 出现，处罚回到待复核，但锁定指针仍指向 v3
        r = self.client.post(f"/api/penalties/{penalty_id}/correct/",
                             {"points": "2.5", "reason": "类别应为散落垃圾，核减",
                              "actor": "监督员-王"}, format="json")
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertEqual(len(body["versions"]), 4)
        self.assertEqual(body["versions"][-1]["kind"], "correction")
        self.assertEqual(body["status"], "draft")
        self.assertEqual(body["locked_version_no"], 3)
        self.assertEqual(float(body["points"]), 2.5)
        # 历史版本行不可变
        self.assertEqual(float(PenaltyVersion.objects.get(
            penalty_id=penalty_id, version_no=2).points), 3.0)
        # 再次复核 → 锁定 v4
        r = self.client.post(f"/api/penalties/{penalty_id}/review/",
                             {"approved": True, "actor": "复核员-李"}, format="json")
        self.assertEqual(r.json()["locked_version_no"], 4)
        self.assertEqual(r.json()["status"], "locked")

        # 已整改后再跑升级：不会新增扣分
        self.client.post(f"/api/events/{event['id']}/rectify/",
                         {"now": (t + timedelta(hours=80)).isoformat()}, format="json")
        self.assertEqual(run(t + timedelta(hours=100)).json()["created_count"], 0)

    def test_overlapping_contracts_are_rejected(self):
        t = self.now
        CleaningContract.objects.create(
            code="D-DUP", grid=self.grid1, contractor_name="丁保洁公司",
            valid_from=t - timedelta(days=1), valid_to=t + timedelta(days=1),
        )
        photo = self.upload_photo("scene_c_bins", 121.4770, 31.2370, t, "重叠区间")
        resp = self.client.post(f"/api/photos/{photo['id']}/create_event/",
                                {"category": "litter"}, format="json")
        self.assertEqual(resp.status_code, 409)

    def test_openapi_schema(self):
        resp = self.client.get("/api/schema/", HTTP_ACCEPT="application/vnd.oai.openapi+json")
        self.assertEqual(resp.status_code, 200)
        schema = json.loads(resp.content)
        self.assertEqual(schema["openapi"].split(".")[0], "3")
        for path in ["/api/photos/", "/api/events/", "/api/penalties/",
                     "/api/candidates/{id}/decide/", "/api/escalations/run/",
                     "/api/penalties/{id}/seal/", "/api/events/{id}/seal/",
                     "/api/packages/", "/api/packages/{id}/supplement/",
                     "/api/packages/{id}/replace/", "/api/packages/{id}/verify/",
                     "/api/packages/{id}/export/", "/api/packages/verify_offline/"]:
            self.assertIn(path, schema["paths"], path)


def compute_phash_hex_bytes(data: bytes) -> str:
    from django.core.files.base import ContentFile
    return compute_phash_hex(ContentFile(data, name="x.png"))
