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
    def test_each_clip_matches_its_exact_expectations(self):
        # 期待値は動画の作り方から決めたもの（manifest.json の expect）。全クリップで全項目を厳密に比べる（レビュー N4）
        clips = manifest()["clips"]
        self.assertGreaterEqual(len(clips), 14)
        for name, clip in clips.items():
            e = clip["truth"]["expect"]
            with self.subTest(clip=name):
                q = build_frame_index(load(name)).quality_report()
                self.assertEqual(q["capture_time_status"], e["status"])
                self.assertEqual(q["frame_count"], e["frames"])
                self.assertEqual(q["gap_frame_ids"], e["gaps"])
                self.assertEqual(q["vfr"], e["vfr"])
                self.assertEqual(q["discarded_packet_count"], e["discarded"])
                self.assertEqual(q["decode_check"], "packets")
                if "decode_failures" in e:
                    self.assertEqual(q["decode_failures"], e["decode_failures"])
                    self.assertEqual(q["after_decode_failure_frame_ids"], [])

    def test_dropped_source_frames_map_to_gap_ids_and_times(self):
        # 欠落させた撮影フレーム番号 n から、欠落直後の原本IDと時刻を計算して一致を確かめる
        for name, clip in manifest()["clips"].items():
            drops = clip["truth"].get("dropped_source_n")
            if not drops:
                continue
            with self.subTest(clip=name):
                idx = build_frame_index(load(name))
                removed = 0
                for lo, hi in drops:
                    first_after = hi + 1                      # 欠落の直後に記録された撮影フレーム n
                    removed += hi - lo + 1
                    fid = first_after - removed               # 原本ID（記録されたフレームだけで数える）
                    self.assertIn("after_gap", idx.by_id(fid).flags)
                    if name.startswith(("drop", "multi_drop")):
                        self.assertEqual(idx.by_id(fid).timestamp, Fraction(first_after, 240))

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
        # 復号失敗で空いた間隔は、撮影時の欠落とは区別する（レビュー N5）
        q = idx.quality_report()
        self.assertEqual(q["gap_frame_ids"], [])
        self.assertEqual(q["after_decode_failure_frame_ids"], [i + 1 for i in expected_ids])
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

    def test_more_real_timing_shapes(self):
        # 近い間隔の複数欠落・比4のFPS切り替え・FPS切り替え直後の欠落・1/600 の MOV・カバー画像付き
        for name in ("multi_drop240", "vfr_120_30", "vfr_drop", "mov600_240_tagged", "drop_edges240", "coverart"):
            with self.subTest(clip=name):
                snap = analyze_clip(name, 1)
                self.assert_events_contain_truth(snap)
                self.assert_values_contain_truth(snap)

    def test_multi_drop_events_flagged(self):
        # 検出そのものは実フィクスチャ（multi_drop240 の欠落ID [40,42,44,46,48]）で確かめている。
        # ここでは同じ欠落パターン（3枚おきに1枚）を合成データで右足の初回接地（0.0503s ≒ n=12）に重ね、
        # 欠落をまたぐイベントが「フレーム欠落を挟む」で要確認になることを確かめる（見逃すと valid のまま: レビュー B1）
        from athform.synth import CaptureSpec
        from .helpers import run
        snap = run(CaptureSpec(capture_fps=240, write_capture_fps_tag=True, drop_frames={6, 9, 12, 15, 18}))
        self.assertEqual(len(snap["capture_quality"]["gap_frame_ids"]), 5)
        gaps = set(snap["capture_quality"]["gap_frame_ids"])
        crossing = [e for e in snap["events"]["auto"] if e["source_frame_ids"][1] in gaps]
        self.assertTrue(crossing, "欠落をまたぐイベントがあるはず")
        for e in crossing:
            self.assertEqual(e["quality_status"], "review_required")
            self.assertIn("フレーム欠落を挟む", e["quality_reasons"])

    def test_mov600_measured_as_240(self):
        # 1/600 では間隔が 2,3 tick の交互になるが、240fps の固定FPSとして撮影メタデータと一致する（レビュー N2）
        snap = analyze_clip("mov600_240_tagged", 1)
        q = snap["capture_quality"]
        self.assertEqual(q["capture_time_status"], "verified")
        self.assertFalse(q["vfr"])
        self.assertTrue(any(m["status"] == "valid" for m in snap["metrics"] if m["metric_id"] == "contact_time"))

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
