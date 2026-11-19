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


HALPE_INDEX = {"nose": 0, "left_shoulder": 5, "right_shoulder": 6, "left_hip": 11, "right_hip": 12, "left_knee": 13,
               "right_knee": 14, "left_ankle": 15, "right_ankle": 16, "left_toe": 20, "right_toe": 21, "left_heel": 24,
               "right_heel": 25}


def as_halpe(points, dy=0.0):
    k = [[0.0, 0.0] for _ in range(26)]
    s = [0.0] * 26
    for name, (x, y, c) in points.items():
        if name in HALPE_INDEX:
            k[HALPE_INDEX[name]] = [x, y + dy]
            s[HALPE_INDEX[name]] = c
    return {"k": k, "s": s}


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
    # 全員の点も保存する: 1人目は合成の走者、2人目は 600px 下を走る別の人（体の大きさは同じ）
    people = {f.source_frame_id: [as_halpe(MODEL.pose_at(float(f.timestamp))),
                                  as_halpe(MODEL.pose_at(float(f.timestamp)), dy=-560)] for f in index.frames}
    return PoseRunResult(ModelInfo("fake-rtmpose", "0", {"mode": mode, "min_confidence": 0.3}), frames,
                         (1280, 1000), 1.0, [], people)


class ServerTest(unittest.TestCase):
    spec = CaptureSpec(capture_fps=240, write_capture_fps_tag=True)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.srv = make_server(self.tmp.name, 0, pose_fn=fake_pose_fn, ffprobe_runner=fake_probe_factory(self.spec))
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.app.jobs.wait_idle(10)   # 実行中のジョブが閉じた DB に書かないように
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
        self.assertIsNone(b["videos"][0]["job"])   # まだ解析していない
        s, b, _ = self.req("GET", "/api/videos?discipline=long_jump")
        self.assertEqual(b["videos"], [])
        # 解析
        job = self.analyze(vid)
        self.assertEqual(job["state"], "completed", job)
        listed = self.req("GET", "/api/videos")[1]["videos"][0]["job"]   # 一覧のカードに出す解析の状態
        self.assertEqual((listed["state"], listed["progress"]), ("completed", 1.0))
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
        self.assertEqual(s2, 409)   # 同じ動画の二重実行は拒否
        s3, _, _ = self.req("DELETE", f"/api/videos/{vid}", {})
        self.assertEqual(s3, 409)   # 解析中の削除は拒否（レビュー B2）
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

    def test_correction_survives_ground_change_and_can_be_revoked(self):
        # レビュー B1: 訂正の後に地面を変えて再解析しても失敗せず、当てられない訂正は「未適用」として残る
        vid = self.upload()[1]["video"]["id"]
        self.analyze(vid)
        snap = self.req("GET", f"/api/videos/{vid}/analysis")[1]
        toe = next(e for e in snap["events"]["auto"] if e["event_type"] == "toe_off")
        s, snap2, _ = self.req("POST", f"/api/videos/{vid}/corrections",
                               {"action": "delete", "event_id": toe["event_id"], "reason": "誤検出"})
        self.assertEqual(s, 200)
        g = snap["detector"]
        s, _, _ = self.req("PATCH", f"/api/videos/{vid}", {"ground_override": {
            "ground_y": g["ground_y"] + 20, "contact_band_px": g["contact_band_px"]}})
        self.assertEqual(s, 200)
        s, b, _ = self.req("POST", f"/api/videos/{vid}/jobs", {"kind": "reanalyze"})
        jid = b["job"]["id"]
        for _ in range(200):
            j = self.req("GET", f"/api/jobs/{jid}")[1]["job"]
            if j["state"] in ("completed", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual(j["state"], "completed", j)
        snap3 = self.req("GET", f"/api/videos/{vid}/analysis")[1]
        self.assertEqual(len(snap3["events"]["unapplied_corrections"]), 1)
        self.assertEqual(snap3["ground_quality"]["method"], "user_override")

        # 取り消し: 記録は残り、取り消し日時と理由が付く
        s, snap4, _ = self.req("POST", f"/api/videos/{vid}/corrections/0/revoke", {"reason": "地面を直したので不要"})
        self.assertEqual(s, 200, snap4)
        self.assertEqual(snap4["events"]["unapplied_corrections"], [])
        corrs = self.req("GET", f"/api/videos/{vid}")[1]["corrections"]
        self.assertEqual(len(corrs), 1)
        self.assertTrue(corrs[0]["revoked_at"])
        # 取り消しも新しい版として残り、前の版は変わらない
        versions = [v["revision"] for v in self.req("GET", f"/api/videos/{vid}")[1]["analyses"]]
        self.assertEqual(versions[0], snap4["revision"])
        self.assertGreater(snap4["revision"], snap3["revision"])
        self.assertEqual(self.req("GET", f"/api/videos/{vid}/analysis?revision={snap3['revision']}")[1], snap3)
        self.assertEqual(self.req("POST", f"/api/videos/{vid}/corrections/0/revoke", {"reason": "x"})[0], 404)
        self.assertEqual(self.req("POST", f"/api/videos/{vid}/corrections/0/revoke", {})[0], 400)   # 理由が必要

    def test_reanalysis_never_overwrites_versions(self):
        # レビュー B3: 同じ入力での再解析でも、前の版のファイルは変わらず、版が増える
        vid = self.upload()[1]["video"]["id"]
        self.analyze(vid)
        path = os.path.join(self.tmp.name, "videos", vid, "analyses", "r1.json")
        with open(path, "rb") as fh:
            before = fh.read()
        self.req("PATCH", f"/api/videos/{vid}", {"notes": "変更", "practice_date": "2020-01-01"})
        s, b, _ = self.req("POST", f"/api/videos/{vid}/jobs", {"kind": "reanalyze"})
        for _ in range(200):
            if self.req("GET", f"/api/jobs/{b['job']['id']}")[1]["job"]["state"] == "completed":
                break
            time.sleep(0.05)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)
        versions = self.req("GET", f"/api/videos/{vid}")[1]["analyses"]
        self.assertEqual([v["revision"] for v in versions], [2, 1])
        self.assertEqual(versions[0]["analysis_id"], versions[1]["analysis_id"])   # 同じ入力なら同じ ID（再現性）
        r1 = self.req("GET", f"/api/videos/{vid}/analysis?revision=1")[1]
        self.assertEqual(r1["revision"], 1)

    def test_auto_ground_caps_valid(self):
        # レビュー B5: 地面の線が自動推定なら valid は出ない。手動で指定すると valid を出せる
        vid = self.upload()[1]["video"]["id"]
        self.analyze(vid)
        snap = self.req("GET", f"/api/videos/{vid}/analysis")[1]
        self.assertEqual(snap["ground_quality"]["method"], "auto")
        self.assertTrue(all(m["status"] != "valid" for m in snap["metrics"]))
        g = snap["detector"]
        self.req("PATCH", f"/api/videos/{vid}", {"ground_override": {"ground_y": g["ground_y"],
                                                                      "contact_band_px": g["contact_band_px"]}})
        s, b, _ = self.req("POST", f"/api/videos/{vid}/jobs", {"kind": "reanalyze"})
        for _ in range(200):
            if self.req("GET", f"/api/jobs/{b['job']['id']}")[1]["job"]["state"] == "completed":
                break
            time.sleep(0.05)
        snap2 = self.req("GET", f"/api/videos/{vid}/analysis")[1]
        self.assertTrue(any(m["status"] == "valid" for m in snap2["metrics"]))

    def test_leftover_version_file_is_skipped(self):
        # 再レビュー N1: 版のファイルだけ残っていても（登録の前に落ちた）、次の版の保存が詰まらない
        vid = self.upload()[1]["video"]["id"]
        self.analyze(vid)
        with open(os.path.join(self.tmp.name, "videos", vid, "analyses", "r2.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        s, b, _ = self.req("POST", f"/api/videos/{vid}/jobs", {"kind": "reanalyze"})
        for _ in range(200):
            j = self.req("GET", f"/api/jobs/{b['job']['id']}")[1]["job"]
            if j["state"] in ("completed", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual(j["state"], "completed", j)
        self.assertEqual(self.req("GET", f"/api/videos/{vid}/analysis")[1]["revision"], 3)

    def test_ground_problem_makes_metrics_review(self):
        # 再レビュー N10: 地面の線に問題（人物が小さい）があると、指標は通しで要確認になる
        def small_pose_fn(*args):
            res = fake_pose_fn(*args)
            for pf in res.frames.values():
                pf.keypoints = {k: (None if v is None else Keypoint(v.x * 0.15, v.y * 0.15, v.confidence))
                                for k, v in pf.keypoints.items()}
            return res
        self.srv.app.jobs.pose_fn = small_pose_fn
        vid = self.upload()[1]["video"]["id"]
        self.analyze(vid)
        snap = self.req("GET", f"/api/videos/{vid}/analysis")[1]
        self.assertTrue(any("人物が小さい" in p for p in snap["ground_quality"]["problems"]))
        for m in snap["metrics"]:
            self.assertNotIn(m["status"], ("valid", "estimated"))

    def test_input_hardening(self):
        # レビュー N6・N7・N9
        self.assertEqual(self.req("GET", "/TESTS/static.test.js")[0], 404)
        self.assertEqual(self.req("GET", "/README.md")[0], 404)
        vid = self.upload()[1]["video"]["id"]
        s, _, _ = self.req("PATCH", f"/api/videos/{vid}", raw=b"[1,2]",
                           headers={"Content-Type": "application/json", "Content-Length": "5"})
        self.assertEqual(s, 400)
        for bad in ({"ground_y": float("nan")}, {"ground_y": 10, "contact_band_px": -1},
                    {"ground_y": 10, "ground_slope": 5}):
            with self.subTest(bad=bad):
                s, _, _ = self.req("PATCH", f"/api/videos/{vid}", raw=json.dumps({"ground_override": bad}).encode(),
                                   headers={"Content-Type": "application/json"})
                self.assertEqual(s, 400)
        s, _, r = self.req("GET", "/")
        self.assertEqual(r.getheader("Cross-Origin-Resource-Policy"), "same-origin")

    def test_orphan_folders_cleaned_on_start(self):
        orphan = os.path.join(self.tmp.name, "videos", "abcdefabcdef")
        os.makedirs(orphan)
        from athform.server import make_server
        srv2 = make_server(self.tmp.name, 0, pose_fn=fake_pose_fn, ffprobe_runner=fake_probe_factory(self.spec))
        self.assertFalse(os.path.exists(orphan))
        srv2.app.jobs.wait_idle(5)
        srv2.server_close()
        srv2.app.store.close()

    def test_choose_another_person_after_analysis(self):
        vid = self.upload()[1]["video"]["id"]
        self.analyze(vid)
        snap = self.req("GET", f"/api/videos/{vid}/analysis")[1]
        self.assertTrue(snap["people_available"])
        fid = snap["frames"][100]["source_frame_id"]
        s, d, _ = self.req("GET", f"/api/videos/{vid}/people?frame={fid}")
        self.assertEqual(s, 200)
        self.assertEqual(len(d["people"]), 2)
        self.assertEqual(d["tracked"], 0)   # 自動では1人目
        # 2人目を選ぶ → 推論なしで追い直し、新しい版になる
        s, snap2, _ = self.req("POST", f"/api/videos/{vid}/target", {"frame_id": fid, "person": 1})
        self.assertEqual(s, 200, snap2)
        self.assertEqual(snap2["revision"], snap["revision"] + 1)
        self.assertEqual((snap2["target"]["frame_id"], snap2["target"]["person"]), (fid, 1))
        self.assertIn("center", snap2["target"])   # 位置と大きさも残す（順番が変わっても探せる）
        hip = next(p for p in snap2["pose_raw"] if p["source_frame_id"] == snap2["frames"][0]["source_frame_id"])
        self.assertLess(hip["keypoints"]["left_hip"][1], 300)   # 最初のコマまで遡って2人目（上の人）を追っている
        self.assertEqual(self.req("GET", f"/api/videos/{vid}/people?frame={fid}")[1]["tracked"], 1)
        # 前の版は変わらない
        self.assertEqual(self.req("GET", f"/api/videos/{vid}/analysis?revision={snap['revision']}")[1]["analysis_id"],
                         snap["analysis_id"])
        # 不正な指定
        self.assertEqual(self.req("POST", f"/api/videos/{vid}/target", {"frame_id": fid, "person": 9})[0], 400)
        self.assertEqual(self.req("POST", f"/api/videos/{vid}/target", {"frame_id": "x", "person": 1})[0], 400)
        self.assertEqual(self.req("GET", f"/api/videos/{vid}/people?frame=abc")[0], 400)
        # 推論をやり直しても、選んだ人を追い続ける
        job = self.analyze(vid)
        self.assertEqual(job["state"], "completed")
        snap3 = self.req("GET", f"/api/videos/{vid}/analysis")[1]
        hip3 = next(p for p in snap3["pose_raw"] if p["source_frame_id"] == snap3["frames"][0]["source_frame_id"])
        self.assertLess(hip3["keypoints"]["left_hip"][1], 300)

    def test_rerun_with_changed_detection_order_keeps_person(self):
        # 再レビュー B3: 推論をやり直して検出の順番が変わっても、選んだ人（上を走る人）を追う
        vid = self.upload()[1]["video"]["id"]
        self.analyze(vid)
        fid = self.req("GET", f"/api/videos/{vid}/analysis")[1]["frames"][100]["source_frame_id"]
        self.assertEqual(self.req("POST", f"/api/videos/{vid}/target", {"frame_id": fid, "person": 1})[0], 200)

        def swapped(*a):
            res = fake_pose_fn(*a)
            res.people = {k: list(reversed(v)) for k, v in res.people.items()}
            return res
        self.srv.app.jobs.pose_fn = swapped
        self.analyze(vid)
        snap = self.req("GET", f"/api/videos/{vid}/analysis")[1]
        hip = next(p for p in snap["pose_raw"] if p["source_frame_id"] == fid)
        self.assertLess(hip["keypoints"]["left_hip"][1], 300)
        self.assertFalse(snap["target"].get("lost"))

        def only_one(*a):   # 選んだ人がいなくなった → 自動に戻したことを記録する
            res = fake_pose_fn(*a)
            res.people = {k: v[:1] for k, v in res.people.items()}
            return res
        self.srv.app.jobs.pose_fn = only_one
        self.analyze(vid)
        snap = self.req("GET", f"/api/videos/{vid}/analysis")[1]
        self.assertTrue(snap["target"]["lost"])
        self.assertTrue(snap["target"]["reason"])

    def test_switching_person_resets_ground_and_corrections(self):
        # 再レビュー N1・N2: 前の人のための地面の指定と修正は、新しい人には当てない（取り消しは記録に残る）
        vid = self.upload()[1]["video"]["id"]
        self.analyze(vid)
        snap = self.req("GET", f"/api/videos/{vid}/analysis")[1]
        toe = next(e for e in snap["events"]["auto"] if e["event_type"] == "toe_off")
        self.req("POST", f"/api/videos/{vid}/corrections", {"action": "delete", "event_id": toe["event_id"], "reason": "x"})
        self.req("PATCH", f"/api/videos/{vid}", {"ground_override": {"ground_y": 899.0, "contact_band_px": 10}})
        fid = snap["frames"][100]["source_frame_id"]
        s, snap2, _ = self.req("POST", f"/api/videos/{vid}/target", {"frame_id": fid, "person": 1})
        self.assertEqual(s, 200, snap2)
        self.assertEqual(len(snap2["target_change_notes"]), 2)
        v = self.req("GET", f"/api/videos/{vid}")[1]
        self.assertIsNone(v["video"]["ground_override"])
        self.assertTrue(v["corrections"][0]["revoked_at"])
        self.assertEqual(snap2["ground_quality"]["method"], "auto")

    def test_choose_person_with_too_few_points_is_400(self):
        # 再レビュー B4: 点が少なすぎる人を選ぶと、理由付きで 400（接続が切れない）
        def with_ghost(*a):
            res = fake_pose_fn(*a)
            ghost = {"k": [[0.0, 0.0]] * 26, "s": [0.0] * 26}
            ghost["s"] = [0.9, 0.9, 0.9] + [0.0] * 23
            res.people = {k: v + [ghost] for k, v in res.people.items()}
            return res
        self.srv.app.jobs.pose_fn = with_ghost
        vid = self.upload()[1]["video"]["id"]
        self.analyze(vid)
        fid = self.req("GET", f"/api/videos/{vid}/analysis")[1]["frames"][50]["source_frame_id"]
        s, b, _ = self.req("POST", f"/api/videos/{vid}/target", {"frame_id": fid, "person": 2})
        self.assertEqual(s, 400)
        self.assertIn("点が少なすぎ", b["error"])

    def test_choose_person_needs_saved_people(self):
        def old_pose_fn(*a):
            res = fake_pose_fn(*a)
            res.people = {}
            return res
        self.srv.app.jobs.pose_fn = old_pose_fn
        vid = self.upload()[1]["video"]["id"]
        self.analyze(vid)
        snap = self.req("GET", f"/api/videos/{vid}/analysis")[1]
        self.assertFalse(snap["people_available"])
        fid = snap["frames"][0]["source_frame_id"]
        self.assertEqual(self.req("GET", f"/api/videos/{vid}/people?frame={fid}")[0], 400)
        self.assertEqual(self.req("POST", f"/api/videos/{vid}/target", {"frame_id": fid, "person": 0})[0], 400)

    def test_static_index(self):
        s, body, r = self.req("GET", "/")
        self.assertEqual(s, 200)
        self.assertIn("text/html", r.getheader("Content-Type"))
        self.assertIn("default-src 'self'", r.getheader("Content-Security-Policy"))


if __name__ == "__main__":
    unittest.main()
