import math
import re
import unittest

from athform.metrics import format_with_resolution
from athform.synth import CaptureSpec

from .helpers import MODEL, metrics, run

REQUIRED_KEYS = {"metric_id", "metric_version", "unit", "definition", "coordinate_system", "data_sources",
                 "required_capture_conditions", "quality", "status", "evidence_refs", "algorithm_version"}


def decimals(display):
    m = re.match(r"(-?\d+)(?:\.(\d+))?", display)
    return len(m.group(2) or "") if m else None


class ResolutionGateTest(unittest.TestCase):
    def test_30fps_contact_time_not_precise(self):
        snap = run(CaptureSpec(capture_fps=30))
        cts = [m for m in metrics(snap, "contact_time") if m["value"] is not None]
        self.assertTrue(cts)
        for m in cts:
            self.assertEqual(m["status"], "review_required")
            self.assertTrue(any("不確実性" in r for r in m["reasons"]))
            self.assertLessEqual(decimals(m["display"]), 2)
            self.assertLessEqual(m["interval"]["lower"], MODEL.contact_time)
            self.assertGreaterEqual(m["interval"]["upper"], MODEL.contact_time)

    def test_240fps_verified_contact_time_valid(self):
        snap = run(CaptureSpec(capture_fps=240, write_capture_fps_tag=True))
        cts = [m for m in metrics(snap, "contact_time") if m["value"] is not None]
        self.assertTrue(cts)
        for m in cts:
            self.assertEqual(m["status"], "valid")
            self.assertLessEqual(m["quality"]["relative_uncertainty"], m["quality"]["max_relative_uncertainty"])
            self.assertAlmostEqual(m["value"], MODEL.contact_time, delta=1 / 240)
            self.assertEqual(decimals(m["display"]), 3)

    def test_format_with_resolution(self):
        self.assertEqual(format_with_resolution(0.1, 0.0667, 0.1333, "s"), "0.10 s（範囲 0.07〜0.13）")
        self.assertEqual(format_with_resolution(0.1, 0.0958, 0.1042, "s"), "0.100 s（範囲 0.096〜0.104）")
        self.assertEqual(format_with_resolution(4.5, 3.0, 6.0, "steps/s"), "4 steps/s（範囲 3〜6）")


class PerMetricGateTest(unittest.TestCase):
    def test_angle_valid_while_contact_time_not(self):
        snap = run(CaptureSpec(capture_fps=30))
        self.assertTrue(all(m["status"] == "valid" for m in metrics(snap, "knee_angle_at_contact")))
        self.assertTrue(all(m["status"] != "valid" for m in metrics(snap, "contact_time")))

    def test_confidence_does_not_change_time_uncertainty(self):
        def lower_conf(pose):
            for f in pose["frames"]:
                for k, v in f["keypoints"].items():
                    if v is not None:
                        f["keypoints"][k] = [v[0], v[1], 0.6]
        a = run(CaptureSpec(capture_fps=240))
        b = run(CaptureSpec(capture_fps=240), pose_edit=lower_conf)
        ia = [m["interval"] for m in metrics(a, "contact_time")]
        ib = [m["interval"] for m in metrics(b, "contact_time")]
        self.assertEqual(ia, ib)


class AngleGateTest(unittest.TestCase):
    def test_front_view_unavailable(self):
        snap = run(CaptureSpec(capture_fps=240), view="front")
        for k in ("knee_angle_at_contact", "trunk_lean_at_contact"):
            for m in metrics(snap, k):
                self.assertEqual(m["status"], "unavailable")
                self.assertIsNone(m["value"])
                self.assertTrue(any("側面" in r for r in m["reasons"]))

    def test_missing_or_nan_keypoint(self):
        def drop_knee(pose):
            for f in pose["frames"]:
                f["keypoints"]["right_knee"] = None
                f["keypoints"]["left_knee"] = [math.nan, 1.0, 0.9]
        snap = run(CaptureSpec(capture_fps=240), pose_edit=drop_knee)
        for m in metrics(snap, "knee_angle_at_contact"):
            self.assertEqual(m["status"], "unavailable")
            self.assertTrue(m["reasons"])

    def test_direction_required_for_lean(self):
        snap = run(CaptureSpec(capture_fps=240), direction=None)
        for m in metrics(snap, "trunk_lean_at_contact"):
            self.assertEqual(m["status"], "unavailable")

    def test_lean_sign_follows_direction(self):
        a = metrics(run(CaptureSpec(capture_fps=60)), "trunk_lean_at_contact")[0]["value"]
        b = metrics(run(CaptureSpec(capture_fps=60), direction="right_to_left"), "trunk_lean_at_contact")[0]["value"]
        self.assertGreater(a, 0)
        self.assertAlmostEqual(a, -b)

    def test_knee_angle_matches_geometry(self):
        snap = run(CaptureSpec(capture_fps=240))
        m = metrics(snap, "knee_angle_at_contact")[0]
        fid = m["evidence_refs"]["frame_ids"][0]
        pose = next(p for p in snap["pose_raw"] if p["source_frame_id"] == fid)["keypoints"]
        s = m["side"]
        h, k, a = pose[f"{s}_hip"], pose[f"{s}_knee"], pose[f"{s}_ankle"]
        v1, v2 = (h[0] - k[0], h[1] - k[1]), (a[0] - k[0], a[1] - k[1])
        expected = math.degrees(math.acos((v1[0] * v2[0] + v1[1] * v2[1]) / (math.hypot(*v1) * math.hypot(*v2))))
        self.assertAlmostEqual(m["value"], expected, places=1)


class MetadataCompletenessTest(unittest.TestCase):
    def test_all_metrics_have_metadata_and_reasons(self):
        for spec, kw in ((CaptureSpec(capture_fps=30), {}), (CaptureSpec(capture_fps=240), {"view": "front"}),
                         (CaptureSpec(capture_fps=240, container_time_scale=8), {})):
            snap = run(spec, **kw)
            self.assertTrue(snap["metrics"])
            for m in snap["metrics"]:
                self.assertTrue(REQUIRED_KEYS <= set(m), REQUIRED_KEYS - set(m))
                self.assertIn(m["status"], {"valid", "estimated", "review_required", "unavailable"})
                self.assertTrue(m["evidence_refs"]["frame_ids"] or m["status"] == "unavailable")
                self.assertNotIn("model_score", m["quality"])
                if m["status"] == "unavailable":
                    self.assertIsNone(m["value"])
                    self.assertTrue(m["reasons"])
                    self.assertEqual(m["display"], "判定不可")
                if m["status"] == "review_required":
                    self.assertTrue(m["reasons"])

    def test_evidence_frames_exist(self):
        snap = run(CaptureSpec(capture_fps=60))
        ids = {f["source_frame_id"] for f in snap["frames"]}
        for m in snap["metrics"]:
            self.assertTrue(set(m["evidence_refs"]["frame_ids"]) <= ids)


if __name__ == "__main__":
    unittest.main()
