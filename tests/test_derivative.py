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
        # 0,1,1(複製),1.5(補間),2,4（3が欠落）… のプロキシ
        proxy = index_from_pts([0, 3000, 4000, 4500, 6000, 12000], tb="1/90000")
        # 複製フレームはPTSが異なる（再エンコードで時刻が振り直された）想定: 4000 は 1/90000*4000=0.0444s → 原本1(0.0333)に近い
        m = map_proxy_to_source(self.src, proxy)
        kinds = [f["kind"] for f in m["frames"]]
        self.assertEqual(kinds, ["source", "source", "duplicate", "generated", "source", "source"])
        self.assertFalse(m["frame_accurate"])
        self.assertIn(3, m["dropped_source_frame_ids"])
        self.assertIsNone(evidence_frame_for_proxy(m, 3))   # 補間フレームは根拠にならない
        self.assertEqual(evidence_frame_for_proxy(m, 2), 1)  # 複製は原本1を指す
        self.assertEqual(evidence_frame_for_proxy(m, 5), 4)  # プロキシ番号5 ≠ 原本ID5

    def test_unknown_proxy_index(self):
        m = map_proxy_to_source(self.src, index_from_pts([0]))
        with self.assertRaises(KeyError):
            evidence_frame_for_proxy(m, 7)


if __name__ == "__main__":
    unittest.main()
