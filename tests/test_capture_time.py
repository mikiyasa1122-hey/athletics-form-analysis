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
        self.assertTrue(any("スロー書き出しの疑い" in n for n in snap["capture_quality"]["capture_time_notes"]))
        for m in time_metrics(snap):
            self.assertEqual(m["status"], "unavailable")
        # 画像平面の角度は時間軸に依存しないので算出できる
        knees = metrics(snap, "knee_angle_at_contact")
        self.assertTrue(knees and all(m["status"] == "valid" for m in knees))

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
