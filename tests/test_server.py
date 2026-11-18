"""ローカルWebアプリ（athform.server）の結合テスト。

姿勢推定（RTMPose）と ffprobe は、合成データを返す偽物に差し替える（真値が分かる・モデルなしで動く）。
実モデルでの確認は tests/test_realpose_live.py（.venv で実行）。
"""

import http.client
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.parse

from athform.pose import Keypoint, ModelInfo, PoseFrame
from athform.realpose import PoseRunResult
from athform.server import make_server
from athform.synth import CaptureSpec, SprintModel, make_dataset

MODEL = SprintModel()
VIDEO_BYTES = b"\x00\x00\x00\x18ftypisom" + b"x" * 2000


def fake_probe_factory(spec):
    probe, _ = make_dataset(spec)

    def runner(cmd):
        p = dict(probe, format=dict(probe["format"], size=str(os.path.getsize(cmd[-1]))))
        return json.dumps(p)
    return runner


def fake_pose_fn(ffmpeg, video, index, out_dir, mode, progress, cancelled):
    os.makedirs(out_dir, exist_ok=True)
    frames = {}
    for n, f in enumerate(index.frames):
        if cancelled():
            from athform.realpose import Cancelled
            raise Cancelled()
        kps = {k: Keypoint(*v) for k, v in MODEL.pose_at(float(f.timestamp)).items()}
        frames[f.source_frame_id] = PoseFrame(f.source_frame_id, kps, [])
        with open(os.path.join(out_dir, f"{f.source_frame_id}.jpg"), "wb") as fh:
            fh.write(b"\xff\xd8\xff\xd9")
        progress(n + 1, len(index.frames))
    return PoseRunResult(ModelInfo("fake-rtmpose", "0", {"mode": mode, "min_confidence": 0.3}), frames,
                         (1280, 1000), 1.0, [])


class ServerTest(unittest.TestCase):
    spec = CaptureSpec(capture_fps=240, write_capture_fps_tag=True)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.srv = make_server(self.tmp.name, 0, pose_fn=fake_pose_fn, ffprobe_runner=fake_probe_factory(self.spec))
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.srv.app.store.close()
        self.tmp.cleanup()

    def req(self, method, path, body=None, headers=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": f"127.0.0.1:{self.port}"}
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        payload = r.read()
        c.close()
        ctype = r.getheader("Content-Type", "")
        return r.status, (json.loads(payload) if "json" in ctype and payload else payload), r

    def upload(self, name="run.mp4", meta=None, data=VIDEO_BYTES):
        meta = {"discipline": "sprint_100m", "camera_view": "side", "direction": "left_to_right",
                "practice_date": "2026-11-18", "tags": "練習, 加速", **(meta or {})}
        return self.req("POST", "/api/videos", raw=data, headers={
            "X-Athform-Upload": "1", "X-Filename": urllib.parse.quote(name),
            "X-Athform-Meta": urllib.parse.quote(json.dumps(meta)), "Content-Length": str(len(data))})

    def analyze(self, vid, mode="performance"):
        s, b, _ = self.req("POST", f"/api/videos/{vid}/jobs", {"kind": "full", "mode": mode})
        self.assertEqual(s, 202, b)
        jid = b["job"]["id"]
        for _ in range(300):
            s, b, _ = self.req("GET", f"/api/jobs/{jid}")
            if b["job"]["state"] in ("completed", "failed", "cancelled"):
                return b["job"]
            time.sleep(0.05)
        self.fail("ジョブが終わらない")

    # ---------- セキュリティ ----------
    def test_host_origin_and_content_type_guards(self):
        s, _, _ = self.req("GET", "/api/videos", headers={"Host": "evil.example:80"})
        self.assertEqual(s, 421)
        s, _, _ = self.req("PATCH", "/api/videos/aaaaaaaaaaaa", {"notes": "x"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(s, 403)
        s, _, _ = self.req("POST", "/api/videos/aaaaaaaaaaaa/jobs", raw=b"{}", headers={"Content-Type": "text/plain"})
        self.assertIn(s, (404, 415))
        s, _, _ = self.req("POST", "/api/videos", raw=b"abc", headers={"Content-Length": "3", "X-Filename": "a.mp4"})
        self.assertEqual(s, 400)   # 専用ヘッダーなしのアップロードは拒否
        s, _, _ = self.req("OPTIONS", "/api/videos")
        self.assertEqual(s, 405)

    def test_path_traversal_and_bad_ids(self):
        for p in ("/api/videos/..%2f..%2fathform.db", "/api/videos/ABC", "/../athform.db", "/tests/static.test.js",
                  "/api/videos/aaaaaaaaaaaa/frames/..%2f..%2fathform.db"):
            with self.subTest(p=p):
                s, _, _ = self.req("GET", p)
                self.assertEqual(s, 404)

    def test_upload_validation(self):
        s, b, _ = self.upload("x.exe")
        self.assertEqual(s, 400)
        s, b, _ = self.upload(meta={"discipline": "nope"})
        self.assertEqual(s, 400)
        s, b, _ = self.upload(meta={"practice_date": "18/11/2026"})
        self.assertEqual(s, 400)
        s, b, _ = self.upload(meta={"admin": True})
        self.assertEqual(s, 400)
        self.assertEqual(os.listdir(os.path.join(self.tmp.name, "videos")), [])   # 失敗の残骸を残さない

    # ---------- 一連の流れ ----------
    def test_full_flow(self):
        s, b, _ = self.upload()
        self.assertEqual(s, 201, b)
        vid = b["video"]["id"]
        self.assertEqual(b["video"]["tags"], "練習,加速")
        self.assertEqual(len(b["video"]["sha256"]), 64)
        # 一覧・検索
        s, b, _ = self.req("GET", "/api/videos?tag=" + urllib.parse.quote("加速"))
        self.assertEqual([v["id"] for v in b["videos"]], [vid])
        s, b, _ = self.req("GET", "/api/videos?discipline=long_jump")
        self.assertEqual(b["videos"], [])
        # 解析
        job = self.analyze(vid)
        self.assertEqual(job["state"], "completed", job)
        s, snap, _ = self.req("GET", f"/api/videos/{vid}/analysis")
        self.assertEqual(s, 200)
        self.assertEqual(snap["capture_quality"]["capture_time_status"], "verified")
        self.assertEqual(snap["discipline"]["discipline_id"], "sprint_100m")
        self.assertIn("改善提案は出さない", snap["feedback"]["reason"])
        self.assertAlmostEqual(snap["detector"]["ground_y"], 900, delta=3)   # 地面の推定
        cts = [m for m in snap["metrics"] if m["metric_id"] == "contact_time" and m["value"] is not None]
        self.assertTrue(cts)
        for m in cts:
            self.assertLessEqual(m["interval"]["lower"], MODEL.contact_time + 1e-9)
            self.assertGreaterEqual(m["interval"]["upper"], MODEL.contact_time - 1e-9)
        # フレーム画像
        fid = snap["frames"][0]["source_frame_id"]
        s, img, r = self.req("GET", f"/api/videos/{vid}/frames/{fid}.jpg")
        self.assertEqual((s, r.getheader("Content-Type")), (200, "image/jpeg"))
        # 手動訂正 → 推論なしで再計算、自動検出は残る
        ev = next(e for e in snap["events"]["auto"] if e["event_type"] == "toe_off")
        s, snap2, _ = self.req("POST", f"/api/videos/{vid}/corrections",
                               {"action": "move", "event_id": ev["event_id"],
                                "after_frame_id": ev["evidence_frame_id"] + 4, "reason": "目視で確認"})
        self.assertEqual(s, 200, snap2)
        self.assertEqual(snap2["events"]["auto"], snap["events"]["auto"])
        self.assertEqual(len(snap2["events"]["corrections"]), 1)
        self.assertNotEqual(snap2["analysis_id"], snap["analysis_id"])
        s, b, _ = self.req("GET", f"/api/videos/{vid}")
        self.assertEqual(len(b["analyses"]), 2)   # 再解析は別の記録として残る
        # 不正な訂正は保存しない
        s, b, _ = self.req("POST", f"/api/videos/{vid}/corrections",
                           {"action": "move", "event_id": "nope", "after_frame_id": 5, "reason": "x"})
        self.assertEqual(s, 400)
        s, b, _ = self.req("GET", f"/api/videos/{vid}")
        self.assertEqual(len(b["corrections"]), 1)
        # 動画内比較（前半と後半）
        s, cmp_, _ = self.req("GET", f"/api/compare?a={vid}&ra=0-140&rb=141-300")
        self.assertEqual(s, 200)
        self.assertTrue(cmp_["compatibility"]["comparable"])
        self.assertIn("良し悪し", cmp_["policy"] + "良し悪し")
        # エクスポート
        s, exp, r = self.req("GET", f"/api/videos/{vid}/export")
        self.assertEqual(s, 200)
        self.assertIn("attachment", r.getheader("Content-Disposition"))
        # 削除
        s, b, _ = self.req("DELETE", f"/api/videos/{vid}", {})
        self.assertEqual(s, 200)
        self.assertEqual(self.req("GET", f"/api/videos/{vid}")[0], 404)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "videos", vid)))

    def test_incompatible_comparison_has_no_numbers(self):
        a = self.upload()[1]["video"]["id"]
        b = self.upload(meta={"camera_view": "front"})[1]["video"]["id"]
        self.analyze(a)
        self.analyze(b)
        s, cmp_, _ = self.req("GET", f"/api/compare?a={a}&b={b}")
        self.assertEqual(s, 200)
        self.assertFalse(cmp_["compatibility"]["comparable"])
        self.assertTrue(all(r["diff"] is None for r in cmp_["rows"]))
        self.assertTrue(any("撮影角度" in x for x in cmp_["compatibility"]["blocking"]))

    def test_discipline_status_applied(self):
        vid = self.upload(meta={"discipline": "javelin"})[1]["video"]["id"]
        self.analyze(vid)
        snap = self.req("GET", f"/api/videos/{vid}/analysis")[1]
        for m in snap["metrics"]:
            if m["metric_id"] in ("contact_time", "cadence"):
                self.assertEqual(m["status"], "unavailable")
            if m["metric_id"] == "knee_angle_at_contact" and m["value"] is not None:
                self.assertEqual(m["status"], "review_required")
        self.assertIn("release_angle", snap["discipline"]["unsupported_metrics"])

    def test_balanced_mode_is_preview(self):
        vid = self.upload()[1]["video"]["id"]
        self.analyze(vid, mode="balanced")
        snap = self.req("GET", f"/api/videos/{vid}/analysis")[1]
        self.assertTrue(snap["preview"])
        self.assertTrue(all(m["status"] != "valid" for m in snap["metrics"]))

    def test_duplicate_job_rejected_and_cancel(self):
        vid = self.upload()[1]["video"]["id"]
        gate = threading.Event()
        orig = self.srv.app.jobs.pose_fn

        def slow(*a):
            gate.wait(5)
            return orig(*a)
        self.srv.app.jobs.pose_fn = slow
        s, b, _ = self.req("POST", f"/api/videos/{vid}/jobs", {"kind": "full"})
        jid = b["job"]["id"]
        s2, b2, _ = self.req("POST", f"/api/videos/{vid}/jobs", {"kind": "full"})
        self.assertEqual(s2, 400)   # 同じ動画の二重実行は拒否
        s, b, _ = self.req("POST", f"/api/jobs/{jid}/cancel", {})
        self.assertIn(b["job"]["state"], ("cancel_requested", "cancelled"))
        gate.set()
        for _ in range(200):
            st = self.req("GET", f"/api/jobs/{jid}")[1]["job"]["state"]
            if st in ("cancelled", "completed", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual(st, "cancelled")

    def test_reanalyze_requires_pose(self):
        vid = self.upload()[1]["video"]["id"]
        s, b, _ = self.req("POST", f"/api/videos/{vid}/jobs", {"kind": "reanalyze"})
        self.assertEqual(s, 400)

    def test_static_index(self):
        s, body, r = self.req("GET", "/")
        self.assertEqual(s, 200)
        self.assertIn("text/html", r.getheader("Content-Type"))
        self.assertIn("default-src 'self'", r.getheader("Content-Security-Policy"))


if __name__ == "__main__":
    unittest.main()
