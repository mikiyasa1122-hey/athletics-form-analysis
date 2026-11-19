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
        self.assertEqual(choose_target(people, st, 0.3), (1, False))

    def test_follows_nearest_not_largest(self):
        st = TrackState()
        choose_target([person(400, 300, 100)], st, 0.3)
        # 次のフレームで、もっと大きい人が遠くに現れても、近くの同じ人を追う
        self.assertEqual(choose_target([person(900, 300, 300), person(420, 305, 100)], st, 0.3), (1, False))

    def test_jump_is_lost_not_switched(self):
        st = TrackState()
        choose_target([person(400, 300, 100)], st, 0.3)
        self.assertEqual(choose_target([person(1200, 300, 100)], st, 0.3), (None, False))   # 別人に乗り移らない
        self.assertEqual(st.lost, 1)

    def test_low_confidence_ignored(self):
        st = TrackState()
        self.assertEqual(choose_target([person(400, 300, 100, score=0.1)], st, 0.3), (None, False))

    def test_gate_does_not_widen_while_lost(self):
        # レビュー B4: 見失っている間に受け入れる範囲を広げない。300px 先の別人を8フレーム後に選ばない
        st = TrackState()
        choose_target([person(400, 300, 100)], st, 0.3)
        for _ in range(8):
            self.assertEqual(choose_target([], st, 0.3), (None, False))
        self.assertEqual(choose_target([person(700, 300, 100)], st, 0.3), (None, False))

    def test_different_size_neighbor_rejected(self):
        # 本人が隠れた1フレームに、近くの大きさの違う人（手前のレーン）がいても乗り移らない
        st = TrackState()
        choose_target([person(400, 300, 100)], st, 0.3)
        self.assertEqual(choose_target([person(430, 330, 180)], st, 0.3), (None, False))

    def test_reacquire_is_marked(self):
        # 長く見失った後に選び直したら「取り直し」と印を付ける（解析ではそこで区切る）
        st = TrackState()
        choose_target([person(400, 300, 100)], st, 0.3)
        for _ in range(15):
            choose_target([], st, 0.3)
        self.assertEqual(choose_target([person(900, 300, 120)], st, 0.3), (0, True))

    def test_recovery_after_short_loss_is_marked(self):
        # 再レビュー B2(a): 数フレーム見失った後、元の位置の近くの同じ大きさの人を選んでも、別人の可能性があるので印を付ける
        st = TrackState()
        choose_target([person(400, 300, 100)], st, 0.3)
        for _ in range(10):
            choose_target([], st, 0.3)
        self.assertEqual(choose_target([person(410, 300, 105)], st, 0.3), (0, True))

    def test_neighbor_not_taken_at_high_fps(self):
        # 再レビュー B2(b): 240fps で本人が検出されなかったフレームに、少し離れた隣の選手がいても乗り移らない
        st = TrackState()
        choose_target([person(400, 300, 100)], st, 0.3, dt=1 / 240)
        self.assertEqual(choose_target([person(430, 305, 120)], st, 0.3, dt=1 / 240), (None, False))

    def test_clearly_nearest_of_two_is_chosen(self):
        # 再々レビュー N2: 範囲内に2人いても、一番近い人が二番目より2倍以上近ければ選ぶ（実映像のフレーム335の形）
        st = TrackState()
        choose_target([person(467, 149, 120)], st, 0.3)
        self.assertEqual(choose_target([person(503, 149, 120), person(470, 149, 120)], st, 0.3), (1, False))

    def test_two_candidates_is_ambiguous(self):
        st = TrackState()
        choose_target([person(400, 300, 100)], st, 0.3)
        self.assertEqual(choose_target([person(405, 300, 100), person(395, 302, 100)], st, 0.3), (None, False))
        self.assertTrue(st.ambiguous)


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
        # 画面の左端から右端まで、地面の線が真の地面から3px以内（遊脚の点に引っ張られて傾かない）
        for x in (250, 500, 750, 1000):
            self.assertAlmostEqual(cfg.ground_y + cfg.ground_slope * (x - cfg.ground_x0), GROUND_Y, delta=3)
        self.assertLess(diag["contact_points_used"], diag["envelope_points"])
        self.assertGreater(cfg.contact_band_px, 4)

    def test_tilted_ground_found(self):
        cfg, _ = estimate_ground(self.frames(slope=0.05))
        self.assertAlmostEqual(cfg.ground_slope, 0.05, delta=0.01)
        for x in (250, 1000):
            self.assertAlmostEqual(cfg.ground_y + cfg.ground_slope * (x - cfg.ground_x0), GROUND_Y + 0.05 * x, delta=4)

    def test_quality_problems_reported(self):
        # レビュー B5: 自動推定の地面を信用できない理由（カメラの動き・小さい人物）を返す
        ok_cfg, ok = estimate_ground(self.frames())
        self.assertEqual(ok["problems"], [])
        # カメラが選手を追う（パン）: 選手は画面のほぼ同じ位置にいて、地面の高さだけが時間とともに変わる
        drifting = {}
        for i, pf in self.frames().items():
            dx = 600.0 * i / 240          # 合成モデルの横移動を打ち消す（カメラが追いかけている）
            shift = 0.4 * i
            drifting[i] = PoseFrame(i, {k: (None if v is None else Keypoint(v.x - dx, v.y + shift, v.confidence))
                                         for k, v in pf.keypoints.items()}, [])
        _, diag = estimate_ground(drifting)
        self.assertTrue(any("カメラが動いている" in p for p in diag["problems"]))
        small = {i: PoseFrame(i, {k: (None if v is None else Keypoint(v.x * 0.15, v.y * 0.15, v.confidence))
                                  for k, v in pf.keypoints.items()}, []) for i, pf in self.frames().items()}
        _, diag = estimate_ground(small)
        self.assertTrue(any("人物が小さい" in p for p in diag["problems"]))

    def test_fixed_tilted_camera_is_not_camera_motion(self):
        # 再レビュー B1: 固定カメラで地面が少し傾いて写る（0.02〜0.05）だけでは「カメラが動いている」と言わない
        for slope in (0.02, 0.05):
            with self.subTest(slope=slope):
                cfg, diag = estimate_ground(self.frames(slope=slope))
                self.assertFalse(any("カメラが動いている" in p for p in diag["problems"]), diag["problems"])
                self.assertAlmostEqual(cfg.ground_slope, slope, delta=0.01)

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
