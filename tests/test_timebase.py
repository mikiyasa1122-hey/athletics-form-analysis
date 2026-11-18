import unittest
from fractions import Fraction

from athform.synth import CaptureSpec, make_dataset
from athform.timebase import TimebaseError, build_frame_index

from .helpers import MODEL, metrics, run


def probe_with(pts_list, time_base="1/90000", fps="30/1"):
    return {
        "streams": [{"index": 0, "codec_type": "video", "time_base": time_base, "avg_frame_rate": fps}],
        "frames": [{"media_type": "video", "stream_index": 0, "pts": p} for p in pts_list],
    }


class VFRTest(unittest.TestCase):
    def test_vfr_detected_and_times_from_pts(self):
        probe, _ = make_dataset(CaptureSpec(capture_fps=120, vfr_jitter=True))
        self.assertTrue(build_frame_index(probe).quality_report()["vfr"])

    def test_fixed_fps_assumption_would_drift(self):
        # 前半60fps・後半30fps（暗所で端末がFPSを落とした想定）。公称は平均の45fps
        pts = [i * 1500 for i in range(30)] + [29 * 1500 + (i + 1) * 3000 for i in range(30)]
        index = build_frame_index(probe_with(pts, fps="45/1"))
        self.assertTrue(index.quality_report()["vfr"])
        last = index.frames[-1]
        self.assertEqual(last.timestamp, Fraction(29 * 1500 + 30 * 3000, 90000))
        fixed = Fraction(last.presentation_index) / index.nominal_fps
        self.assertGreater(abs(fixed - last.timestamp), Fraction(1, 30))  # 固定FPS換算なら1フレーム以上ずれる

    def test_cfr_not_vfr(self):
        probe, _ = make_dataset(CaptureSpec(capture_fps=60))
        self.assertFalse(build_frame_index(probe).quality_report()["vfr"])

    def test_vfr_metrics_contain_truth(self):
        snap = run(CaptureSpec(capture_fps=120, vfr_jitter=True))
        cts = [m for m in metrics(snap, "contact_time") if m["value"] is not None]
        self.assertGreaterEqual(len(cts), 4)
        for m in cts:
            self.assertLessEqual(m["interval"]["lower"], MODEL.contact_time + 1e-9)
            self.assertGreaterEqual(m["interval"]["upper"], MODEL.contact_time - 1e-9)
        cad = metrics(snap, "cadence")[0]
        self.assertLessEqual(cad["interval"]["lower"], MODEL.cadence)
        self.assertGreaterEqual(cad["interval"]["upper"], MODEL.cadence)


class AnomalyTest(unittest.TestCase):
    def test_duplicate_gap_non_monotonic_flags(self):
        idx = build_frame_index(probe_with([0, 3000, 3000, 6000, 9000, 18000, 21000, 15000]))
        flags = {f.source_frame_id: f.flags for f in idx.frames}
        self.assertIn("duplicate_pts", flags[2])
        self.assertIn("after_gap", flags[5])
        self.assertIn("non_monotonic", flags[7])
        self.assertFalse(idx.time_axis_valid)
        self.assertEqual(idx.capture_time_status, "unknown")
        rep = idx.quality_report()
        self.assertEqual(rep["duplicate_pts_frame_ids"], [2])
        self.assertEqual(rep["gap_frame_ids"], [5])

    def test_duplicate_frames_excluded_from_intervals(self):
        idx = build_frame_index(probe_with([0, 3000, 3000, 6000]))
        self.assertEqual(idx.intervals(), [Fraction(1, 30), Fraction(1, 30)])
        self.assertTrue(idx.time_axis_valid)

    def test_invalid_time_axis_blocks_time_metrics(self):
        def scramble(probe):
            probe["frames"][50]["pts"], probe["frames"][51]["pts"] = probe["frames"][51]["pts"], probe["frames"][50]["pts"]
        snap = run(CaptureSpec(capture_fps=240), probe_edit=scramble)
        for m in metrics(snap, "contact_time") + metrics(snap, "cadence"):
            self.assertEqual(m["status"], "unavailable")
            self.assertIsNone(m["value"])
            self.assertTrue(m["reasons"])

    def test_dropped_frames_widen_interval(self):
        # 撮影時に記録されなかったフレームがあっても、実PTSで区間が広がり真値を含む
        snap = run(CaptureSpec(capture_fps=240, drop_frames=set(range(10, 16))))
        self.assertTrue(snap["capture_quality"]["gap_frame_ids"])
        ev = [e for e in snap["events"]["auto"] if e["source_frame_ids"][0] < 10 <= e["source_frame_ids"][1]]
        self.assertTrue(ev, "欠落区間をまたぐイベントがあるはず（右足の初回接地 0.0503s）")
        for e in ev:
            self.assertLessEqual(e["time_interval_sec"]["lower"], MODEL.first_contact)
            self.assertGreaterEqual(e["time_interval_sec"]["upper"], MODEL.first_contact)
            self.assertGreater(e["time_interval_sec"]["upper"] - e["time_interval_sec"]["lower"], 1 / 240 * 2)
            self.assertEqual(e["quality_status"], "review_required")


class InvalidInputTest(unittest.TestCase):
    def test_missing_pts(self):
        p = probe_with([0, 3000, 6000])
        del p["frames"][1]["pts"]
        with self.assertRaises(TimebaseError):
            build_frame_index(p)

    def test_na_pts(self):
        with self.assertRaises(TimebaseError):
            build_frame_index(probe_with([0, "N/A", 6000]))

    def test_non_integer_pts(self):
        with self.assertRaises(TimebaseError):
            build_frame_index(probe_with([0, "abc"]))

    def test_missing_or_bad_time_base(self):
        for tb in (None, "0/1", "x", "-1/90000"):
            with self.subTest(tb=tb), self.assertRaises(TimebaseError):
                build_frame_index(probe_with([0, 3000], time_base=tb))

    def test_no_video_or_no_frames(self):
        with self.assertRaises(TimebaseError):
            build_frame_index({"streams": [{"codec_type": "audio", "time_base": "1/48000"}], "frames": []})
        with self.assertRaises(TimebaseError):
            build_frame_index(probe_with([]))

    def test_best_effort_timestamp_fallback(self):
        p = probe_with([0, 3000])
        p["frames"][1] = {"media_type": "video", "stream_index": 0, "best_effort_timestamp": 3000}
        self.assertEqual(build_frame_index(p).frames[1].timestamp, Fraction(1, 30))

    def test_decode_failure_recorded_ids_stable(self):
        p = probe_with([0, 3000, 6000])
        p["frames"][1]["decode_error"] = True
        idx = build_frame_index(p)
        self.assertEqual(idx.decode_failures, [1])
        self.assertEqual([f.source_frame_id for f in idx.frames], [0, 2])


class ExactTimeTest(unittest.TestCase):
    def test_exact_fractions_long_clip(self):
        n = 100000
        idx = build_frame_index(probe_with([i * 375 for i in range(n)], fps="240/1"))
        self.assertEqual(idx.frames[-1].timestamp, Fraction((n - 1) * 375, 90000))
        self.assertEqual(idx.frames[3].timestamp, Fraction(1, 80))
        self.assertEqual(set(idx.intervals()), {Fraction(1, 240)})

    def test_first_pts_offset(self):
        idx = build_frame_index(probe_with([90000, 93000]))
        self.assertEqual(idx.frames[0].timestamp, 0)
        self.assertEqual(idx.frames[1].pts, 93000)


if __name__ == "__main__":
    unittest.main()
