import unittest

from athform.derivative import evidence_frame_for_proxy, map_proxy_to_source
from athform.pose import FixturePoseAdapter, PoseInputError
from athform.synth import CaptureSpec, make_dataset
from athform.timebase import build_frame_index


def index_from_pts(pts, tb="1/90000"):
    return build_frame_index({"streams": [{"index": 0, "codec_type": "video", "time_base": tb, "avg_frame_rate": "30/1"}],
                              "frames": [{"pts": p} for p in pts]})


class GeneratedFrameTest(unittest.TestCase):
    def test_generated_pose_rejected(self):
        _, pose = make_dataset(CaptureSpec(capture_fps=30))
        pose["frames"][3]["generated"] = True
        with self.assertRaises(PoseInputError):
            FixturePoseAdapter(pose)

    def test_bad_pose_shapes_rejected(self):
        _, pose = make_dataset(CaptureSpec(capture_fps=30))
        for bad in ([1, 2], "x", [1, 2, 3.5], [1, "a", 0.5]):
            p = {"model": pose["model"], "frames": [{"source_frame_id": 0, "keypoints": {"nose": bad}}]}
            with self.subTest(bad=bad), self.assertRaises(PoseInputError):
                FixturePoseAdapter(p)
        with self.assertRaises(PoseInputError):
            FixturePoseAdapter({"model": {}, "frames": []})
        with self.assertRaises(PoseInputError):
            FixturePoseAdapter({"model": pose["model"], "frames": [{"source_frame_id": "1"}]})
        with self.assertRaises(PoseInputError):
            FixturePoseAdapter({"model": pose["model"], "frames": [{"source_frame_id": 1}, {"source_frame_id": 1}]})


class ProxyMappingTest(unittest.TestCase):
    def setUp(self):
        self.src = index_from_pts([i * 3000 for i in range(10)])  # 30fps 原本

    def test_identical_proxy_is_frame_accurate(self):
        m = map_proxy_to_source(self.src, index_from_pts([i * 3000 for i in range(10)]))
        self.assertTrue(m["frame_accurate"])
        self.assertEqual([f["source_frame_id"] for f in m["frames"]], list(range(10)))

    def test_duplicated_dropped_and_interpolated(self):
        # 原本0, 原本1, 原本1の複製（同時刻）, 原本1から11ms後（時刻を振り直した複製 or 補間。原本ではない）,
        # 1.5の補間, 原本2, 原本4（3は欠落）
        proxy = index_from_pts([0, 3000, 3000, 4000, 4500, 6000, 12000], tb="1/90000")
        m = map_proxy_to_source(self.src, proxy)
        kinds = [f["kind"] for f in m["frames"]]
        self.assertEqual(kinds, ["source", "source", "duplicate", "generated", "generated", "source", "source"])
        self.assertFalse(m["frame_accurate"])
        self.assertIn(3, m["dropped_source_frame_ids"])
        self.assertIsNone(evidence_frame_for_proxy(m, 3))   # 原本と時刻が合わないフレームは根拠にしない
        self.assertIsNone(evidence_frame_for_proxy(m, 4))   # 補間フレームは根拠にならない
        self.assertEqual(evidence_frame_for_proxy(m, 2), 1)  # 同時刻の複製は原本1を指す
        self.assertEqual(evidence_frame_for_proxy(m, 6), 4)  # プロキシ番号6 ≠ 原本ID6

    def test_integer_factor_interpolation(self):
        # 30fps原本を ×2・×4 に補間したプロキシ。中間フレームはすべて generated、原本と同時刻だけが source
        for factor in (2, 4):
            with self.subTest(factor=factor):
                proxy = index_from_pts([i * 3000 // factor for i in range(10 * factor - (factor - 1))])
                m = map_proxy_to_source(self.src, proxy)
                for f in m["frames"]:
                    if f["proxy_index"] % factor == 0:
                        self.assertEqual((f["kind"], f["source_frame_id"]), ("source", f["proxy_index"] // factor))
                    else:
                        self.assertEqual(f["kind"], "generated")
                        self.assertIsNone(evidence_frame_for_proxy(m, f["proxy_index"]))
                self.assertFalse(m["frame_accurate"])
                self.assertEqual(m["dropped_source_frame_ids"], [])

    def test_timebase_rounding_tolerated(self):
        # 再エンコードで time_base が 1/15360 に変わっても、1tick 以内の丸めは原本と一致とみなす
        proxy = index_from_pts([round(i / 30 * 15360) for i in range(10)], tb="1/15360")
        m = map_proxy_to_source(self.src, proxy)
        self.assertTrue(m["frame_accurate"])

    def test_unknown_proxy_index(self):
        m = map_proxy_to_source(self.src, index_from_pts([0]))
        with self.assertRaises(KeyError):
            evidence_frame_for_proxy(m, 7)


if __name__ == "__main__":
    unittest.main()
