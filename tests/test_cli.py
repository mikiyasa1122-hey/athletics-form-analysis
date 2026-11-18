import json
import os
import tempfile
import unittest

from athform import cli
from athform.synth import CaptureSpec, make_dataset


def save(obj, path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class FfprobeTest(unittest.TestCase):
    def test_command_reads_every_frame(self):
        cmd = cli.ffprobe_command("in.mp4")
        self.assertIn("-show_frames", cmd)
        self.assertIn("-show_packets", cmd)   # 復号失敗をパケットとの差分で見つけるため
        self.assertNotIn("best_effort_timestamp", " ".join(cmd))
        self.assertIn("v:0", cmd)
        for forbidden in ("-skip_frame", "-read_intervals", "-vf", "-r"):
            self.assertNotIn(forbidden, cmd)
        self.assertEqual(cmd[-1], "in.mp4")

    def test_probe_uses_runner(self):
        with tempfile.TemporaryDirectory() as d:
            v = os.path.join(d, "v.mp4")
            open(v, "wb").close()
            seen = []
            out = cli.probe_video(v, runner=lambda c: seen.append(c) or '{"streams": []}')
            self.assertEqual(out, {"streams": []})
            self.assertEqual(seen[0][-1], v)

    def test_probe_missing_file(self):
        with self.assertRaises(SystemExit):
            cli.probe_video("does-not-exist.mp4", runner=lambda c: "{}")


class CliRunTest(unittest.TestCase):
    def test_analyze_and_demo(self):
        probe, pose = make_dataset(CaptureSpec(capture_fps=120))
        probe["format"]["size"] = str(len(b"video-bytes"))   # ffprobe -show_format が出すファイルサイズ
        with tempfile.TemporaryDirectory() as d:
            pp, ps, out, vid = (os.path.join(d, n) for n in ("probe.json", "pose.json", "snap.json", "v.mp4"))
            save(probe, pp)
            save(pose, ps)
            with open(vid, "wb") as fh:
                fh.write(b"video-bytes")
            corr = os.path.join(d, "corr.json")
            save([{"action": "add", "event_type": "foot_contact", "side": "left", "after_frame_id": 2,
                        "corrected_by": "me", "corrected_at": "2026-11-18", "reason": "test"}],
                 corr)
            rc = cli.main(["analyze", "--probe", pp, "--pose", ps, "--video", vid, "--ground-y", "900",
                           "--view", "side", "--direction", "left_to_right", "--corrections", corr, "--out", out])
            self.assertEqual(rc, 0)
            snap = load(out)
            self.assertEqual(len(snap["source_video_hash"]), 64)
            self.assertEqual(len(snap["events"]["corrections"]), 1)

            # 別の動画の probe（サイズ不一致）や format.size のない probe は拒否する
            for size in ("999", None):
                bad = dict(probe, format=dict(probe["format"], size=size) if size else {"tags": {}})
                bp = os.path.join(d, "bad.json")
                save(bad, bp)
                with self.subTest(size=size), self.assertRaises(SystemExit):
                    cli.main(["analyze", "--probe", bp, "--pose", ps, "--video", vid, "--ground-y", "900",
                              "--out", os.path.join(d, "x.json")])

            demo = os.path.join(d, "demo")
            self.assertEqual(cli.main(["demo", "--out", demo]), 0)
            catalog = load(os.path.join(demo, "index.json"))
            self.assertEqual(len(catalog), len(cli.DEMOS))
            for item in catalog:
                self.assertTrue(os.path.isfile(os.path.join(demo, item["file"])))


if __name__ == "__main__":
    unittest.main()
