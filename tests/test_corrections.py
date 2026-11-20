import unittest

from athform.events import Correction, CorrectionError
from athform.synth import CaptureSpec

from .helpers import metrics, run

SPEC = CaptureSpec(capture_fps=240, write_capture_fps_tag=True)
WHO = {"corrected_by": "athlete-1", "corrected_at": "2026-11-18T10:00:00+09:00", "reason": "目視で離地を確認"}


def first(snap, side, etype):
    return next(e for e in snap["events"]["auto"] if e["side"] == side and e["event_type"] == etype)


class LayerTest(unittest.TestCase):
    def test_auto_layer_kept_and_audit_saved(self):
        base = run(SPEC)
        toe = first(base, "right", "toe_off")
        c = Correction(action="move", event_id=toe["event_id"], after_frame_id=toe["evidence_frame_id"] + 5, **WHO)
        snap = run(SPEC, [c])
        self.assertEqual(snap["events"]["auto"], base["events"]["auto"])
        self.assertEqual(snap["events"]["corrections"], [c.to_json()])
        eff_ids = {e["event_id"] for e in snap["events"]["effective"]}
        self.assertNotIn(toe["event_id"], eff_ids)
        manual = [e for e in snap["events"]["effective"] if e["source"] == "manual"]
        self.assertEqual(len(manual), 1)
        self.assertEqual(manual[0]["estimation_method"], "manual_frame")
        self.assertNotEqual(snap["analysis_id"], base["analysis_id"])

    def test_audit_fields_required(self):
        for missing in ("corrected_by", "corrected_at", "reason"):
            who = dict(WHO, **{missing: "  "})
            c = Correction(action="delete", event_id="x", **who)
            with self.subTest(missing=missing), self.assertRaises(CorrectionError):
                run(SPEC, [c])


class RecomputeTest(unittest.TestCase):
    def test_contact_time_recomputed(self):
        base = run(SPEC)
        contact, toe = first(base, "right", "foot_contact"), first(base, "right", "toe_off")
        before = next(m for m in metrics(base, "contact_time") if contact["event_id"] in m["evidence_refs"]["event_ids"])
        new_frame = toe["evidence_frame_id"] + 10
        snap = run(SPEC, [Correction(action="move", event_id=toe["event_id"], after_frame_id=new_frame, **WHO)])
        after = next(m for m in metrics(snap, "contact_time") if contact["event_id"] in m["evidence_refs"]["event_ids"])
        self.assertAlmostEqual(after["value"] - before["value"], 10 / 240, places=9)
        self.assertIn(new_frame, after["evidence_refs"]["frame_ids"])
        self.assertTrue(any(i.endswith("-manual") for i in after["evidence_refs"]["event_ids"]))

    def test_delete_toe_off_makes_contact_time_unavailable(self):
        base = run(SPEC)
        contact, toe = first(base, "right", "foot_contact"), first(base, "right", "toe_off")
        snap = run(SPEC, [Correction(action="delete", event_id=toe["event_id"], **WHO)])
        m = next(m for m in metrics(snap, "contact_time") if contact["event_id"] in m["evidence_refs"]["event_ids"])
        # 離地を消すと、その接地の次は同じ足の接地なので接地時間は組めない
        self.assertEqual(m["status"], "unavailable")
        self.assertIsNone(m["value"])
        self.assertTrue(m["reasons"])

    def test_out_of_order_correction_flagged(self):
        # 飛行中に同じ足の接地を追加すると、接地/離地が交互でなくなる → 要確認（レビュー N3）
        base = run(SPEC)
        contact = first(base, "right", "foot_contact")
        snap = run(SPEC, [Correction(action="add", event_type="foot_contact", side="right",
                                     after_frame_id=contact["evidence_frame_id"] + 5, **WHO)])
        flagged = [e for e in snap["events"]["effective"] if "同じ足で接地と離地が交互になっていない" in e["quality_reasons"]]
        self.assertGreaterEqual(len(flagged), 2)
        for m in metrics(snap, "contact_time"):
            if set(m["evidence_refs"]["event_ids"]) & {e["event_id"] for e in flagged}:
                self.assertNotEqual(m["status"], "valid")
        self.assertNotEqual(metrics(snap, "cadence")[0]["status"], "valid")

    def test_angle_on_tracking_lost_frame_unavailable(self):
        base = run(SPEC)
        contact = first(base, "right", "foot_contact")
        target = contact["evidence_frame_id"] + 2

        def lost(pose):
            for f in pose["frames"]:
                if f["source_frame_id"] == target:
                    f["flags"].append("tracking_lost")
        snap = run(SPEC, [Correction(action="move", event_id=contact["event_id"], after_frame_id=target, **WHO)],
                   pose_edit=lost)
        knee = [m for m in metrics(snap, "knee_angle_at_contact") if target in m["evidence_refs"]["frame_ids"]]
        self.assertEqual(len(knee), 1)
        self.assertEqual(knee[0]["status"], "unavailable")
        self.assertIsNone(knee[0]["value"])

    def test_add_event(self):
        snap = run(SPEC, [Correction(action="add", event_type="foot_contact", side="left", after_frame_id=3, **WHO)])
        self.assertTrue(any(e["event_id"] == "left-foot_contact-3-manual" for e in snap["events"]["effective"]))


class UnappliedCorrectionTest(unittest.TestCase):
    def test_unapplied_correction_caps_metrics(self):
        # 再レビュー N2: 保存済みの訂正が当てられない（参照先のイベントがない）とき、解析は続けるが確かな値として出さない
        from athform.analysis import analyze
        from .helpers import make_input
        inp = make_input(SPEC)
        inp.strict_corrections = False
        snap = analyze(inp, [Correction(action="delete", event_id="right-toe_off-99999", **WHO)])
        self.assertEqual(len(snap["events"]["unapplied_corrections"]), 1)
        live = [m for m in snap["metrics"] if m["status"] != "unavailable"]
        self.assertTrue(live)
        for m in live:
            self.assertEqual(m["status"], "review_required")
            self.assertTrue(any("適用できなかった手動訂正" in r for r in m["reasons"]))

    def test_strict_mode_still_raises(self):
        with self.assertRaises(CorrectionError):
            run(SPEC, [Correction(action="delete", event_id="right-toe_off-99999", **WHO)])


class InvalidCorrectionTest(unittest.TestCase):
    def test_rejected(self):
        base = run(SPEC)
        toe = first(base, "right", "toe_off")
        bad = [
            Correction(action="move", event_id=toe["event_id"], after_frame_id=10 ** 6, **WHO),
            Correction(action="move", event_id=toe["event_id"], after_frame_id=0, **WHO),
            Correction(action="move", event_id="nope", after_frame_id=5, **WHO),
            Correction(action="move", event_id=toe["event_id"], after_frame_id=None, **WHO),
            Correction(action="explode", event_id=toe["event_id"], after_frame_id=5, **WHO),
            Correction(action="add", event_type="jump", side="left", after_frame_id=5, **WHO),
            Correction(action="delete", event_id="nope", **WHO),
        ]
        for c in bad:
            with self.subTest(c=c), self.assertRaises(CorrectionError):
                run(SPEC, [c])

    def test_duplicate_pts_frame_rejected(self):
        def dup(probe):
            # パケット情報なし（原本IDは提示順）で、提示順20のフレームが19と同じPTSを持つ
            del probe["packets"]
            probe["frames"][20]["pts"] = probe["frames"][19]["pts"]
        c = Correction(action="add", event_type="foot_contact", side="left", after_frame_id=20, **WHO)
        with self.assertRaises(CorrectionError):
            run(SPEC, [c], probe_edit=dup)

    def test_from_json_rejects_unknown_fields(self):
        with self.assertRaises(CorrectionError):
            Correction.from_json({"action": "delete", "event_id": "x", "admin": True, **WHO})
        with self.assertRaises(CorrectionError):
            Correction.from_json({"action": "delete"})


if __name__ == "__main__":
    unittest.main()
