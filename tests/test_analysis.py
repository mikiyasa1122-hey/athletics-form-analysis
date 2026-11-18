import inspect
import json
import os
import tempfile
import unittest

from athform.analysis import AnalysisError, SamplingPolicy, analyze, file_sha256
from athform.synth import CaptureSpec

from .helpers import make_input, run


class CompletenessTest(unittest.TestCase):
    def test_all_frames_analyzed(self):
        snap = run(CaptureSpec(capture_fps=240))
        c = snap["frame_completeness"]
        self.assertEqual(snap["analysis_sampling_policy"]["mode"], "all_source_frames")
        self.assertEqual(c["selected_frame_count"], c["decoded_frame_count"])
        self.assertEqual(c["analyzed_frame_count"], c["decoded_frame_count"])
        self.assertEqual(c["pose_failed_frame_ids"], [])
        self.assertEqual(c["skipped_by_sampling"], 0)

    def test_pose_failures_and_decode_failures_reported(self):
        def drop(pose):
            pose["frames"] = [f for f in pose["frames"] if f["source_frame_id"] not in (5, 6, 100)]

        # 200 はパケットはあるが復号できないフレーム（ffprobe の frames には出てこない）
        snap = run(CaptureSpec(capture_fps=240, decode_fail_frames={200}), pose_edit=drop)
        c = snap["frame_completeness"]
        self.assertEqual(c["pose_failed_frame_ids"], [5, 6, 100])
        self.assertEqual(c["decode_failed_source_frame_ids"], [200])
        self.assertEqual(c["source_frame_count"], c["decoded_frame_count"] + 1)
        self.assertEqual(c["analyzed_frame_count"], c["decoded_frame_count"] - 3)

    def test_foreign_pose_frames_rejected(self):
        def extra(pose):
            pose["frames"].append({"source_frame_id": 99999, "keypoints": {}, "flags": []})
        with self.assertRaises(AnalysisError):
            analyze(make_input(CaptureSpec(capture_fps=60), pose_edit=extra))


class PreviewTest(unittest.TestCase):
    def test_preview_never_valid(self):
        snap = run(CaptureSpec(capture_fps=240, write_capture_fps_tag=True),
                   sampling=SamplingPolicy("preview_every_nth", 4, "確認用"))
        self.assertTrue(snap["preview"])
        self.assertGreater(snap["frame_completeness"]["skipped_by_sampling"], 0)
        self.assertTrue(snap["metrics"])
        for m in snap["metrics"]:
            self.assertNotEqual(m["status"], "valid")
            self.assertTrue(any("プレビュー" in x for x in m["limitations"]))

    def test_preview_pose_failures_widen_events(self):
        # プレビューで推定に失敗したフレームは未観測として扱い、イベントを要確認にする（レビュー N4）
        def drop(pose):
            pose["frames"] = [f for f in pose["frames"] if f["source_frame_id"] not in range(8, 20)]
        snap = run(CaptureSpec(capture_fps=240, write_capture_fps_tag=True), pose_edit=drop,
                   sampling=SamplingPolicy("preview_every_nth", 2, "確認用"))
        e = next(e for e in snap["events"]["auto"] if e["side"] == "right")
        self.assertEqual(e["quality_status"], "review_required")
        self.assertIn("遷移区間に観測できないフレームがある", e["quality_reasons"])

    def test_preview_requires_reason_and_n(self):
        for pol in (SamplingPolicy("preview_every_nth", 4, ""), SamplingPolicy("preview_every_nth", 1, "x"),
                    SamplingPolicy("weird", 1, "x")):
            with self.subTest(pol=pol), self.assertRaises(AnalysisError):
                run(CaptureSpec(capture_fps=60), sampling=pol)


class PlaybackIndependenceTest(unittest.TestCase):
    def test_analyze_has_no_playback_parameter(self):
        params = set(inspect.signature(analyze).parameters)
        self.assertEqual(params, {"inp", "corrections"})
        snap = run(CaptureSpec(capture_fps=60))
        self.assertNotIn("playback", json.dumps(snap).lower())

    def test_input_not_mutated(self):
        inp = make_input(CaptureSpec(capture_fps=240, container_time_scale=8))
        before = inp.index.capture_time_status
        a = analyze(inp)
        self.assertEqual(inp.index.capture_time_status, before)
        self.assertEqual(analyze(inp)["analysis_id"], a["analysis_id"])


class ReproducibilityTest(unittest.TestCase):
    def test_same_input_same_id(self):
        a = run(CaptureSpec(capture_fps=120, vfr_jitter=True))
        b = run(CaptureSpec(capture_fps=120, vfr_jitter=True))
        self.assertEqual(a["analysis_id"], b["analysis_id"])
        self.assertEqual(a, b)

    def test_settings_change_changes_id(self):
        a = make_input(CaptureSpec(capture_fps=60))
        b = make_input(CaptureSpec(capture_fps=60))
        b.detector = type(b.detector)(ground_y=b.detector.ground_y, contact_band_px=8.0)
        self.assertNotEqual(analyze(a)["analysis_id"], analyze(b)["analysis_id"])

    def test_versions_recorded(self):
        snap = run(CaptureSpec(capture_fps=60))
        for k in ("schema_version", "algorithm_version", "source_video_hash", "model", "detector",
                  "capture_conditions", "analysis_sampling_policy"):
            self.assertIn(k, snap)
        self.assertEqual(snap["model"]["model_id"], "synthetic-fixture")
        self.assertIn("weights_version", snap["model"])

    def test_file_sha256(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "v.bin")
            with open(p, "wb") as fh:
                fh.write(b"abc")
            self.assertEqual(file_sha256(p), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


if __name__ == "__main__":
    unittest.main()
