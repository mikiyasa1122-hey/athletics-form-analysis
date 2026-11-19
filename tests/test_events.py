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

    def test_long_occlusion_or_drop_is_a_break_not_a_valid_value(self):
        # 1歩分以上（0.35〜0.72s）を覆う遮蔽・欠落。間の離地・接地が消えたまま値を出してはいけない（レビュー B1）
        span = set(range(int(0.35 * 240), int(0.72 * 240)))
        for kw in ({"occluded_frames": span}, {"drop_frames": span}):
            with self.subTest(**{k: "0.35-0.72s" for k in kw}):
                snap = run(CaptureSpec(capture_fps=240, write_capture_fps_tag=True, **kw))
                self.assertTrue(snap["events"]["observation_breaks"])
                for m in snap["metrics"]:
                    if m["metric_id"] == "contact_time" and m["value"] is not None:
                        self.assertLessEqual(m["interval"]["lower"], MODEL.contact_time)
                        self.assertGreaterEqual(m["interval"]["upper"], MODEL.contact_time)
                    if m["metric_id"] == "cadence" and m["value"] is not None:
                        self.assertLessEqual(m["interval"]["lower"], MODEL.cadence)
                        self.assertGreaterEqual(m["interval"]["upper"], MODEL.cadence)
                # 途切れをまたぐ接地時間は判定不可で、理由がある
                crossing = [m for m in snap["metrics"] if m["metric_id"] == "contact_time"
                            and any("途切れ" in r for r in m["reasons"])]
                self.assertTrue(crossing)
                self.assertTrue(all(m["status"] == "unavailable" for m in crossing))
                # 全イベント区間は真値を含む（途切れをまたいでイベントを作らない）
                truth = truth_times(1.2)
                for e in snap["events"]["auto"]:
                    lo, hi = e["time_interval_sec"]["lower"], e["time_interval_sec"]["upper"]
                    self.assertTrue(any(lo - 1e-9 <= t <= hi + 1e-9 for t in truth[(e["side"], e["event_type"])]))

    def test_short_occlusion_is_not_a_break(self):
        snap = run(CaptureSpec(capture_fps=240, occluded_frames={11, 12, 13}))
        self.assertEqual(snap["events"]["observation_breaks"], [])

    def test_reacquired_frame_breaks_observation(self):
        # レビュー B4: 対象人物を取り直したフレームでは観測を区切り、前後をつないでイベントを作らない
        from athform.events import DetectorConfig, detect_events
        from athform.synth import GROUND_Y, make_dataset
        from athform.pose import FixturePoseAdapter
        from athform.timebase import build_frame_index
        probe, pose = make_dataset(CaptureSpec(capture_fps=240))
        pose["frames"][13]["flags"].append("reacquired")   # 右足の初回接地の直後
        adapter = FixturePoseAdapter(pose)
        index = build_frame_index(probe)
        poses = {f.source_frame_id: adapter.infer(f.source_frame_id) for f in index.frames}
        det = detect_events(index, poses, DetectorConfig(ground_y=GROUND_Y))
        self.assertTrue(any("取り直した" in b.reason for b in det.breaks))
        self.assertFalse(any(e.after_frame_id == 13 for e in det.events))

    def test_reacquired_frame_without_feet_still_breaks(self):
        # 再々レビュー N1: 取り直したフレームで足が見えなくても、次に足が見えたところで区切る
        from athform.events import DetectorConfig, detect_events
        from athform.synth import GROUND_Y, make_dataset
        from athform.pose import FixturePoseAdapter
        from athform.timebase import build_frame_index
        probe, pose = make_dataset(CaptureSpec(capture_fps=240))
        f13 = pose["frames"][13]
        f13["flags"].append("reacquired")
        for k in list(f13["keypoints"]):
            if k.endswith(("heel", "toe", "ankle")):
                f13["keypoints"][k] = None
        adapter = FixturePoseAdapter(pose)
        index = build_frame_index(probe)
        poses = {f.source_frame_id: adapter.infer(f.source_frame_id) for f in index.frames}
        det = detect_events(index, poses, DetectorConfig(ground_y=GROUND_Y))
        self.assertTrue(any("取り直した" in b.reason for b in det.breaks))
        self.assertFalse(any(e.before_frame_id < 13 < e.after_frame_id or e.after_frame_id == 14 for e in det.events))

    def test_blur_flags_event(self):
        snap = run(CaptureSpec(capture_fps=240, blurred_frames={13}))
        e = next(e for e in snap["events"]["auto"] if 13 in e["source_frame_ids"])
        self.assertEqual(e["quality_status"], "review_required")


if __name__ == "__main__":
    unittest.main()
