import unittest

from athform.synth import CaptureSpec

from .helpers import MODEL, run


def truth_times(duration):
    out = {}
    for side in ("left", "right"):
        cs = MODEL.true_contacts(side, duration)
        out[(side, "foot_contact")] = [s for s, _ in cs]
        out[(side, "toe_off")] = [e for _, e in cs]
    return out


class IntervalContainsTruthTest(unittest.TestCase):
    SPECS = [CaptureSpec(capture_fps=f) for f in (240, 120, 60, 30)] + [CaptureSpec(capture_fps=120, vfr_jitter=True)]

    def test_every_event_interval_contains_a_true_transition(self):
        for spec in self.SPECS:
            with self.subTest(fps=spec.capture_fps, vfr=spec.vfr_jitter):
                snap = run(spec)
                truth = truth_times(spec.duration)
                events = snap["events"]["auto"]
                self.assertGreaterEqual(len(events), 8)
                for e in events:
                    lo, hi = e["time_interval_sec"]["lower"], e["time_interval_sec"]["upper"]
                    cands = truth[(e["side"], e["event_type"])]
                    self.assertTrue(any(lo - 1e-9 <= t <= hi + 1e-9 for t in cands),
                                    f"{e['event_id']} [{lo}, {hi}] が真値 {cands} を含まない")
                    self.assertLessEqual(lo, e["event_time_sec"])
                    self.assertLessEqual(e["event_time_sec"], hi)

    def test_interval_width_is_one_frame_interval(self):
        snap = run(CaptureSpec(capture_fps=240))
        for e in snap["events"]["auto"]:
            w = e["time_interval_sec"]["upper"] - e["time_interval_sec"]["lower"]
            self.assertAlmostEqual(w, 1 / 240, places=9)
            a, b = e["source_frame_ids"]
            self.assertEqual(b, a + 1)
            self.assertEqual(e["time_basis"], "source_pts")


class UnobservedTest(unittest.TestCase):
    def test_occlusion_widens_and_flags(self):
        # 右足の初回接地 0.0503s ≒ 240fps のフレーム12〜13。前後を遮蔽する
        snap = run(CaptureSpec(capture_fps=240, occluded_frames={11, 12, 13}))
        e = next(e for e in snap["events"]["auto"] if e["side"] == "right" and e["event_type"] == "foot_contact")
        self.assertEqual(e["quality_status"], "review_required")
        self.assertIn("遷移区間に観測できないフレームがある", e["quality_reasons"])
        w = e["time_interval_sec"]["upper"] - e["time_interval_sec"]["lower"]
        self.assertGreater(w, 3 / 240)
        self.assertLessEqual(e["time_interval_sec"]["lower"], MODEL.first_contact)
        self.assertGreaterEqual(e["time_interval_sec"]["upper"], MODEL.first_contact)
        ct = next(m for m in snap["metrics"] if m["metric_id"] == "contact_time" and e["event_id"] in m["evidence_refs"]["event_ids"])
        self.assertNotEqual(ct["status"], "valid")

    def test_blur_flags_event(self):
        snap = run(CaptureSpec(capture_fps=240, blurred_frames={13}))
        e = next(e for e in snap["events"]["auto"] if 13 in e["source_frame_ids"])
        self.assertEqual(e["quality_status"], "review_required")


if __name__ == "__main__":
    unittest.main()
