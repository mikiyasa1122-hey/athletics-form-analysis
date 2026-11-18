"""realpose のうち、モデルなしで確かめられる部分（人物の選択・点の対応・地面の推定・フレームの取り出し）。"""

import os
import unittest

from athform.realpose import (HALPE26, TrackState, choose_target, estimate_ground, iter_bmp_frames, to_pose_frame,
                              PoseRunError)
from athform.synth import GROUND_Y, SprintModel
from athform.pose import KEYPOINTS, Keypoint, PoseFrame
from athform.timebase import build_frame_index
from athform.tools import find_tool

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "ffprobe")


def person(cx, cy, size, score=0.9):
    """Halpe26 形式の人物（26点）。腰の中心が (cx, cy)。"""
    kps = [[cx + (i % 5 - 2) * size / 10, cy + (i // 5 - 2) * size / 5] for i in range(26)]
    kps[11], kps[12] = [cx - 5, cy], [cx + 5, cy]
    return kps, [score] * 26


class TrackingTest(unittest.TestCase):
    def test_first_frame_picks_largest(self):
        st = TrackState()
        people = [person(100, 100, 50), person(400, 300, 200), person(700, 300, 80)]
        self.assertEqual(choose_target(people, st, 0.3), 1)

    def test_follows_nearest_not_largest(self):
        st = TrackState()
        choose_target([person(400, 300, 100)], st, 0.3)
        # 次のフレームで、もっと大きい人が遠くに現れても、近くの同じ人を追う
        idx = choose_target([person(900, 300, 300), person(420, 305, 100)], st, 0.3)
        self.assertEqual(idx, 1)

    def test_jump_is_lost_not_switched(self):
        st = TrackState()
        choose_target([person(400, 300, 100)], st, 0.3)
        self.assertIsNone(choose_target([person(1200, 300, 100)], st, 0.3))   # 別人に乗り移らない
        self.assertEqual(st.lost, 1)

    def test_low_confidence_ignored(self):
        st = TrackState()
        self.assertIsNone(choose_target([person(400, 300, 100, score=0.1)], st, 0.3))


class MappingTest(unittest.TestCase):
    def test_halpe26_mapping_and_score_clip(self):
        kps = [[float(i), float(i * 2)] for i in range(26)]
        scores = [1.4] * 26   # SimCC の値は1を超えうる
        pf = to_pose_frame(7, kps, scores)
        self.assertEqual(pf.source_frame_id, 7)
        self.assertEqual(set(pf.keypoints), set(KEYPOINTS))
        for h, name in HALPE26.items():
            kp = pf.keypoints[name]
            self.assertEqual((kp.x, kp.y), (float(h), float(h * 2)))
            self.assertLessEqual(kp.confidence, 1.0)
        self.assertEqual(pf.keypoints["left_heel"].x, 24.0)
        self.assertEqual(pf.keypoints["left_toe"].x, 20.0)   # 足先は母趾


class GroundTest(unittest.TestCase):
    def frames(self, slope=0.0):
        m = SprintModel()
        out = {}
        for i in range(288):
            raw = m.pose_at(i / 240)
            kps = {}
            for k, (x, y, c) in raw.items():
                kps[k] = Keypoint(x, y + slope * x, c)
            out[i] = PoseFrame(i, {**{k: None for k in KEYPOINTS}, **kps}, [])
        return out

    def test_flat_ground_found(self):
        cfg, diag = estimate_ground(self.frames())
        self.assertAlmostEqual(cfg.ground_y + cfg.ground_slope * (900 - cfg.ground_x0), GROUND_Y, delta=3)
        self.assertGreater(cfg.contact_band_px, 4)

    def test_tilted_ground_found(self):
        cfg, _ = estimate_ground(self.frames(slope=0.05))
        self.assertAlmostEqual(cfg.ground_slope, 0.05, delta=0.02)

    def test_no_feet_raises(self):
        empty = {i: PoseFrame(i, {k: None for k in KEYPOINTS}, ["no_person"]) for i in range(50)}
        with self.assertRaises(PoseRunError):
            estimate_ground(empty)


@unittest.skipUnless(find_tool("ffmpeg") and os.path.isdir(os.path.join(os.path.dirname(__file__), "..", ".cache", "videos")),
                     "FFmpeg と試験動画（tools/make_test_videos.py）が必要")
class DecodeTest(unittest.TestCase):
    def test_every_source_frame_decoded_in_order(self):
        # 実際の ffmpeg の出力（BMP）を区切り、原本フレーム索引と同じ枚数が取れること（可変FPS・欠落も含む）
        import json
        root = os.path.join(os.path.dirname(__file__), "..", ".cache", "videos")
        for name in ("cfr30_bframes", "vfr_60_30", "drop240", "corrupt", "cut_copy"):
            with self.subTest(clip=name):
                path = os.path.join(root, f"{name}.mp4")
                with open(os.path.join(FIX, f"{name}.json"), encoding="utf-8") as fh:
                    idx = build_frame_index(json.load(fh))
                frames = list(iter_bmp_frames(find_tool("ffmpeg"), path))
                self.assertEqual(len(frames), len(idx.frames))
                self.assertTrue(all(f[:2] == b"BM" for f in frames))


if __name__ == "__main__":
    unittest.main()
