import unittest

from athform.synth import CaptureSpec

from .helpers import metrics, run

TIME_METRICS = ("contact_time", "cadence")


def time_metrics(snap):
    return [m for k in TIME_METRICS for m in metrics(snap, k)]


class MetadataTest(unittest.TestCase):
    def test_matching_metadata_is_verified(self):
        snap = run(CaptureSpec(capture_fps=240, write_capture_fps_tag=True))
        self.assertEqual(snap["capture_quality"]["capture_time_status"], "verified")
        self.assertTrue(any(m["status"] == "valid" for m in metrics(snap, "contact_time")))

    def test_slowmo_export_with_metadata(self):
        snap = run(CaptureSpec(capture_fps=240, container_time_scale=8.0, write_capture_fps_tag=True))
        q = snap["capture_quality"]
        self.assertEqual(q["capture_time_status"], "slowmo_suspected")
        self.assertAlmostEqual(q["capture_fps"], 240.0)
        self.assertAlmostEqual(q["nominal_fps"], 30.0)
        for m in time_metrics(snap):
            self.assertEqual(m["status"], "unavailable")
            self.assertIsNone(m["value"])
            self.assertTrue(any("実時間" in r or "対応する離地" in r for r in m["reasons"]))
        # 見かけの動画時間は別欄に出し、実測値と混同しない
        apparent = [m["apparent_video_value"] for m in metrics(snap, "contact_time") if m["apparent_video_value"]]
        self.assertTrue(apparent)
        self.assertGreater(min(apparent), 0.5)


class PlausibilityTest(unittest.TestCase):
    def test_untagged_slowmo_blocked_by_plausibility(self):
        snap = run(CaptureSpec(capture_fps=240, container_time_scale=8.0))
        self.assertEqual(snap["capture_quality"]["capture_time_status"], "slowmo_suspected")
        notes = snap["capture_quality"]["capture_time_notes"]
        self.assertTrue(any("スロー書き出しの疑い" in n for n in notes))
        self.assertFalse(any("実時間とみなしている" in n for n in notes))  # 撤回された仮定を残さない
        for m in time_metrics(snap):
            self.assertEqual(m["status"], "unavailable")
        # 画像平面の角度は時間軸に依存しないので算出できる
        knees = metrics(snap, "knee_angle_at_contact")
        self.assertTrue(knees and all(m["status"] == "valid" for m in knees))

    def test_short_untagged_slowmo_not_reported_in_seconds(self):
        # 1〜2歩だけの短いスロー書き出し（レビュー B2）。接地時間の妥当性で見抜くか、少なくとも精密値にしない
        snap = run(CaptureSpec(capture_fps=240, container_time_scale=8.0, duration=0.3))
        for m in time_metrics(snap):
            self.assertNotIn(m["status"], ("valid", "estimated"))

    def test_2x_untagged_slowmo_detected(self):
        # 2倍スローでも、短距離として不自然なピッチ・接地時間で気づく（レビュー N6）
        snap = run(CaptureSpec(capture_fps=240, container_time_scale=2.0))
        self.assertEqual(snap["capture_quality"]["capture_time_status"], "slowmo_suspected")
        for m in time_metrics(snap):
            self.assertEqual(m["status"], "unavailable")

    def test_short_normal_clip_checked_by_contact_time(self):
        # 接地1回分だけの通常速度の素材でも、接地時間で速さの妥当性を確かめられる → 推定値として出す
        snap = run(CaptureSpec(capture_fps=240, duration=0.16))
        self.assertEqual(snap["capture_quality"]["capture_time_status"], "container_only")
        vals = [m for m in metrics(snap, "contact_time") if m["value"] is not None]
        self.assertEqual(len(vals), 1)
        self.assertEqual(vals[0]["status"], "estimated")
        # ピッチは接地が足りず判定不可（妥当性の確認には使っていない）
        self.assertEqual(metrics(snap, "cadence")[0]["status"], "unavailable")

    def test_normal_speed_not_flagged(self):
        snap = run(CaptureSpec(capture_fps=60))
        self.assertEqual(snap["capture_quality"]["capture_time_status"], "container_only")


class DeclaredTest(unittest.TestCase):
    def test_declared_mismatch_blocks_seconds(self):
        snap = run(CaptureSpec(capture_fps=30), declared_fps="240")
        self.assertEqual(snap["capture_quality"]["capture_time_status"], "declared")
        for m in time_metrics(snap):
            self.assertEqual(m["status"], "unavailable")

    def test_declared_match_never_verified(self):
        snap = run(CaptureSpec(capture_fps=240), declared_fps="240")
        self.assertEqual(snap["capture_quality"]["capture_time_status"], "container_only")
        for m in time_metrics(snap):
            self.assertNotEqual(m["status"], "valid")

    def test_declared_slowmo(self):
        snap = run(CaptureSpec(capture_fps=240), declared_slowmo=True)
        self.assertEqual(snap["capture_quality"]["capture_time_status"], "slowmo_suspected")
        for m in time_metrics(snap):
            self.assertEqual(m["status"], "unavailable")

    def test_container_only_is_estimated_not_valid(self):
        snap = run(CaptureSpec(capture_fps=240))
        statuses = {m["status"] for m in time_metrics(snap) if m["value"] is not None}
        self.assertEqual(statuses, {"estimated"})


if __name__ == "__main__":
    unittest.main()
