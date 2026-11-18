"""実際の FFmpeg 9.0 で作った試験動画の ffprobe 出力（tests/fixtures/ffprobe/）での検証。

フィクスチャは tools/make_test_videos.py が作る。値は ffprobe の出力そのもので、時間軸に関係する項目だけ残している。
姿勢推定モデルはまだないので、各原本フレームの「撮影時刻」（PTS ÷ スロー倍率）から合成モデルで姿勢を作り、
実際のコンテナの時刻で端から端まで解析できるかを確かめる。
"""

import json
import os
import unittest
from fractions import Fraction

from athform.analysis import AnalysisInput, analyze
from athform.events import DetectorConfig
from athform.metrics import CaptureConditions
from athform.pose import FixturePoseAdapter
from athform.synth import GROUND_Y, SprintModel
from athform.timebase import build_frame_index

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "ffprobe")
MODEL = SprintModel()


def load(name):
    with open(os.path.join(FIX, f"{name}.json"), encoding="utf-8") as fh:
        return json.load(fh)


def manifest():
    with open(os.path.join(FIX, "manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)


def entries(probe, kind):
    return [e for e in probe["packets_and_frames"] if e["type"] == kind]


def analyze_clip(name, scale):
    probe = load(name)
    index = build_frame_index(probe)
    frames = [{"source_frame_id": f.source_frame_id,
               "keypoints": MODEL.pose_at(float(f.timestamp / scale)), "flags": []} for f in index.frames]
    pose = {"model": {"model_id": "synthetic-fixture", "weights_version": "0"}, "frames": frames}
    return analyze(AnalysisInput(f"fixture:{name}", index, FixturePoseAdapter(pose), DetectorConfig(ground_y=GROUND_Y),
                                 CaptureConditions(camera_view="side", direction="left_to_right")))


def truth(scale):
    out = {}
    for side in ("left", "right"):
        cs = MODEL.true_contacts(side, 20)
        out[(side, "foot_contact")] = [s * scale for s, _ in cs]
        out[(side, "toe_off")] = [e * scale for _, e in cs]
    return out


class ManifestExpectationsTest(unittest.TestCase):
    def test_each_clip_matches_its_recorded_expectations(self):
        for name, clip in manifest()["clips"].items():
            t = clip["truth"]
            with self.subTest(clip=name):
                q = build_frame_index(load(name)).quality_report()
                self.assertEqual(q["capture_time_status"], t["expect_status"])
                self.assertEqual(q["decode_check"], "packets")
                if "expect_vfr" in t:
                    self.assertEqual(q["vfr"], t["expect_vfr"])
                if "expect_gaps" in t:
                    self.assertEqual(len(q["gap_frame_ids"]), t["expect_gaps"])
                if "expect_decode_failures" in t:
                    self.assertEqual(len(q["decode_failures"]), t["expect_decode_failures"])

    def test_real_output_format(self):
        # ffprobe 9.0 は -show_packets と -show_frames を併用すると packets_and_frames 形式で出す
        for name in manifest()["clips"]:
            with self.subTest(clip=name):
                p = load(name)
                self.assertIn("packets_and_frames", p)
                self.assertNotIn("frames", p)


class EditListPrerollTest(unittest.TestCase):
    """再レビュー N2: 先頭カットした動画の pre-roll パケット（flags D）を復号失敗と誤判定しない。"""

    def test_discarded_packets_not_counted(self):
        p = load("cut_copy")
        discarded = [e for e in entries(p, "packet") if "D" in e["flags"]]
        self.assertGreater(len(discarded), 0, "実際の ffprobe 出力に D フラグのパケットがあるはず")
        self.assertTrue(all(e["pts"] < 0 for e in discarded))
        idx = build_frame_index(p)
        self.assertEqual(idx.decode_failures, [])
        self.assertEqual(idx.quality_report()["discarded_packet_count"], len(discarded))
        self.assertEqual([f.source_frame_id for f in idx.frames], list(range(len(idx.frames))))
        self.assertEqual(idx.frames[0].timestamp, 0)  # 再生の先頭が元動画時刻 0


class RealDecodeFailureTest(unittest.TestCase):
    def test_corrupted_packets_detected_with_stable_ids(self):
        clip = manifest()["clips"]["corrupt"]["truth"]
        p = load("corrupt")
        idx = build_frame_index(p)
        tb = Fraction(p["streams"][0]["time_base"])
        packet_pts = sorted(e["pts"] for e in entries(p, "packet") if "D" not in e["flags"])
        expected_ids = [packet_pts.index(x) for x in clip["corrupted_packet_pts"]]
        self.assertEqual(idx.decode_failures, expected_ids)
        # 失敗したフレームの後ろのIDは詰まらない（ID = パケットのPTS順位）
        for f in idx.frames:
            self.assertEqual(f.source_frame_id, packet_pts.index(f.pts))
            self.assertEqual(f.timestamp, (f.pts - packet_pts[0]) * tb)
        self.assertEqual(len(entries(p, "frame")), len(packet_pts) - len(expected_ids))


class DecodeOrderTest(unittest.TestCase):
    def test_bframe_packets_in_decode_order_mapped_by_pts(self):
        p = load("cfr30_bframes")
        pk = [e["pts"] for e in entries(p, "packet")]
        self.assertNotEqual(pk, sorted(pk), "Bフレームのあるストリームではパケットは復号順のはず")
        self.assertTrue(any(e["dts"] < 0 for e in entries(p, "packet")))
        idx = build_frame_index(p)
        self.assertEqual([f.source_frame_id for f in idx.frames], list(range(len(idx.frames))))
        self.assertEqual(set(idx.intervals()), {Fraction(1, 30)})


class EndToEndOnRealTimingTest(unittest.TestCase):
    def assert_events_contain_truth(self, snap, scale=1):
        tr = truth(scale)
        self.assertGreaterEqual(len(snap["events"]["auto"]), 4)
        for e in snap["events"]["auto"]:
            lo, hi = e["time_interval_sec"]["lower"], e["time_interval_sec"]["upper"]
            self.assertTrue(any(lo - 1e-9 <= t <= hi + 1e-9 for t in tr[(e["side"], e["event_type"])]),
                            f"{e['event_id']} [{lo}, {hi}]")

    def assert_values_contain_truth(self, snap):
        for m in snap["metrics"]:
            if m["value"] is None:
                continue
            if m["metric_id"] == "contact_time":
                self.assertLessEqual(m["interval"]["lower"], MODEL.contact_time + 1e-9)
                self.assertGreaterEqual(m["interval"]["upper"], MODEL.contact_time - 1e-9)
            if m["metric_id"] == "cadence":
                self.assertLessEqual(m["interval"]["lower"], MODEL.cadence)
                self.assertGreaterEqual(m["interval"]["upper"], MODEL.cadence)

    def test_verified_240fps(self):
        snap = analyze_clip("cfr240_tagged", 1)
        self.assertEqual(snap["capture_quality"]["capture_time_status"], "verified")
        self.assert_events_contain_truth(snap)
        self.assert_values_contain_truth(snap)
        self.assertTrue(any(m["status"] == "valid" for m in snap["metrics"] if m["metric_id"] == "contact_time"))

    def test_vfr_uses_real_pts(self):
        snap = analyze_clip("vfr_60_30", 1)
        self.assertTrue(snap["capture_quality"]["vfr"])
        self.assert_events_contain_truth(snap)
        self.assert_values_contain_truth(snap)

    def test_dropped_frames_break_not_fabricate(self):
        snap = analyze_clip("drop240", 1)
        self.assertEqual(len(snap["capture_quality"]["gap_frame_ids"]), 1)
        self.assertTrue(snap["events"]["observation_breaks"])  # 0.1秒の欠落 ≥ 0.06秒
        self.assert_events_contain_truth(snap)
        self.assert_values_contain_truth(snap)

    def test_corrupt_clip_reports_and_stays_safe(self):
        snap = analyze_clip("corrupt", 1)
        c = snap["frame_completeness"]
        self.assertEqual(len(c["decode_failed_source_frame_ids"]), 2)
        self.assertEqual(c["source_frame_count"], c["decoded_frame_count"] + 2)
        self.assert_events_contain_truth(snap)
        self.assert_values_contain_truth(snap)

    def test_untagged_slowmo_export_not_reported_in_seconds(self):
        snap = analyze_clip("slowmo8_plain", 8)
        self.assertEqual(snap["capture_quality"]["capture_time_status"], "slowmo_suspected")
        self.assert_events_contain_truth(snap, scale=8)   # 動画時間ではイベント自体は正しく挟める
        for m in snap["metrics"]:
            if m["metric_id"] in ("contact_time", "cadence"):
                self.assertEqual(m["status"], "unavailable")
                self.assertIsNone(m["value"])
            if m["metric_id"] == "knee_angle_at_contact":
                self.assertEqual(m["status"], "valid")   # 画像平面の角度は時間軸に依存しない

    def test_tagged_slowmo_export_not_reported_in_seconds(self):
        snap = analyze_clip("slowmo8_tagged", 8)
        self.assertEqual(snap["capture_quality"]["capture_time_status"], "slowmo_suspected")
        for m in snap["metrics"]:
            if m["metric_id"] in ("contact_time", "cadence"):
                self.assertEqual(m["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
