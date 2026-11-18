"""FFmpeg で試験動画を作り、実際の ffprobe 出力をテスト用フィクスチャとして保存する。

  python tools/make_test_videos.py            # 動画を .cache/videos に作り、tests/fixtures/ffprobe/ を更新
  python tools/make_test_videos.py --check    # 作り直した ffprobe 出力が、保存済みフィクスチャと一致するか確かめる

動画の中身は FFmpeg のテストパターン（人物は映らない）。時間軸（PTS・欠落・可変FPS・スロー書き出し・
先頭カット・パケット破損）の振る舞いを、実際のエンコーダ・コンテナ・ffprobe で確かめるためのもの。
各動画の作り方と「正解」（撮影時刻への換算など）を manifest.json に残す。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from athform.cli import ffprobe_command  # noqa: E402
from athform.tools import find_tool  # noqa: E402

VIDEO_DIR = os.path.join(ROOT, ".cache", "videos")
FIXTURE_DIR = os.path.join(ROOT, "tests", "fixtures", "ffprobe")

X264 = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "24", "-bf", "2", "-preset", "veryfast"]
DUR = "1.2"   # 撮影時間（秒）。-t を出力側に置くとスロー書き出しが切り詰められるので、ソースに持たせる

# 名前: (説明, 正解, ffmpeg 引数を作る関数)
CLIPS: dict[str, tuple[str, dict, list[str]]] = {
    "cfr240_tagged": (
        "240fps・撮影FPSメタデータあり（Android と同じキー）",
        {"capture_time_scale": 1, "expect_status": "verified"},
        ["-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=240:duration={DUR}", *X264,
         "-movflags", "use_metadata_tags", "-metadata", "com.android.capture.fps=240.000000"],
    ),
    "cfr30_bframes": (
        "30fps・Bフレームあり（パケットは復号順、DTSは負から始まる）",
        {"capture_time_scale": 1, "expect_status": "container_only"},
        ["-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=30:duration={DUR}", *X264],
    ),
    "vfr_60_30": (
        "可変FPS: 前半60fps、後半30fps（暗所で端末がFPSを落とした想定）",
        {"capture_time_scale": 1, "expect_status": "container_only", "expect_vfr": True, "expect_gaps": 0},
        ["-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=60:duration={DUR}",
         # 先頭36枚は1/60秒間隔、残り36枚は1/30秒間隔に並べ直す（間引きはしない）
         "-vf", "setpts='if(lt(N,36),N,2*N-36)/(60*TB)'",   # 整数で計算（0.6/TB は浮動小数の丸めで時刻がずれる）
         "-fps_mode", "passthrough", "-video_track_timescale", "90000", *X264],
    ),
    "drop240": (
        "240fpsで撮影中に n=40〜63（0.1秒分）が記録されなかった（PTSはそのまま飛ぶ）",
        {"capture_time_scale": 1, "expect_status": "container_only", "expect_gaps": 1, "dropped_frames": [40, 63]},
        ["-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=240:duration={DUR}",
         "-vf", "select='not(between(n,40,63))'", "-fps_mode", "passthrough", *X264],
    ),
    "slowmo8_plain": (
        "240fps撮影を8倍に引き伸ばして30fpsタイムラインに書き出したスロー（メタデータなし）",
        {"capture_time_scale": 8, "expect_status": "container_only"},
        ["-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=240:duration={DUR}", "-vf", "setpts=8*PTS", "-r", "30", *X264],
    ),
    "slowmo8_tagged": (
        "同じスロー書き出しに撮影FPSメタデータ（240）が付いている",
        {"capture_time_scale": 8, "expect_status": "slowmo_suspected"},
        ["-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=240:duration={DUR}", "-vf", "setpts=8*PTS", "-r", "30", *X264,
         "-movflags", "use_metadata_tags", "-metadata", "com.android.capture.fps=240.000000"],
    ),
}
# 2段階で作るもの（元動画を加工する）
DERIVED = {
    "cut_copy": ("cfr30_bframes", "30fpsを先頭0.3秒でカット（再エンコードなし）。edit list で先頭パケットが再生対象外（flags D）になる",
                 {"capture_time_scale": 1, "expect_status": "container_only", "expect_decode_failures": 0}),
    "corrupt": ("cfr30_bframes", "30fpsの非キーフレーム2つのパケットの中身を0で潰した（デコーダがフレームを落とす）",
                {"capture_time_scale": 1, "expect_status": "container_only"}),
}

KEEP_ENTRY = ("type", "media_type", "codec_type", "stream_index", "pts", "dts", "pkt_dts", "flags")
KEEP_STREAM = ("index", "codec_type", "codec_name", "time_base", "avg_frame_rate", "r_frame_rate", "tags")


def trim(probe: dict) -> dict:
    """フィクスチャの容量を抑えるため、時間軸に関係する項目だけ残す（値は ffprobe の出力そのまま）。"""
    out = {"streams": [{k: s[k] for k in KEEP_STREAM if k in s} for s in probe.get("streams", [])],
           "format": {k: probe.get("format", {})[k] for k in ("size", "tags") if k in probe.get("format", {})}}
    for key in ("packets_and_frames", "packets", "frames"):
        if key in probe:
            out[key] = [{k: e[k] for k in KEEP_ENTRY if k in e} for e in probe[key]]
    return out


def run(cmd: list[str]) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8").stdout


def probe(ffprobe: str, path: str) -> dict:
    cmd = ffprobe_command(path)
    return json.loads(run([ffprobe, *cmd[1:]]))


def corrupt(ffprobe: str, src: str, dst: str) -> list[int]:
    shutil.copy(src, dst)
    entries = probe(ffprobe, src)["packets_and_frames"]
    full = json.loads(run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_packets", "-of", "json", src]))
    by_pts = {p["pts"]: p for p in full["packets"]}
    non_key = [e for e in entries if e.get("type") == "packet" and not str(e.get("flags", "")).startswith("K")]
    targets = [by_pts[e["pts"]] for e in non_key[8:10]]
    with open(dst, "r+b") as fh:
        for t in targets:
            fh.seek(int(t["pos"]))
            fh.write(b"\x00" * int(t["size"]))
    return sorted(t["pts"] for t in targets)


def build(ffmpeg: str, ffprobe: str) -> dict[str, dict]:
    os.makedirs(VIDEO_DIR, exist_ok=True)
    results: dict[str, dict] = {}
    for name, (desc, truth, args) in CLIPS.items():
        path = os.path.join(VIDEO_DIR, f"{name}.mp4")
        run([ffmpeg, "-v", "error", "-y", *args, path])
        results[name] = {"description": desc, "truth": dict(truth), "probe": trim(probe(ffprobe, path))}
    for name, (base, desc, truth) in DERIVED.items():
        src = os.path.join(VIDEO_DIR, f"{base}.mp4")
        path = os.path.join(VIDEO_DIR, f"{name}.mp4")
        truth = dict(truth)
        if name == "cut_copy":
            run([ffmpeg, "-v", "error", "-y", "-ss", "0.3", "-i", src, "-c", "copy", path])
        else:
            truth["corrupted_packet_pts"] = corrupt(ffprobe, src, path)
        results[name] = {"description": desc, "truth": truth, "probe": trim(probe(ffprobe, path))}
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="作り直した出力が保存済みフィクスチャと一致するか確かめる")
    args = ap.parse_args(argv)
    ffmpeg, ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
    if not ffmpeg or not ffprobe:
        print("FFmpeg が見つからない（ATHFORM_FFMPEG / ATHFORM_FFPROBE で指定できる）", file=sys.stderr)
        return 2
    version = run([ffprobe, "-v", "error", "-version"]).splitlines()[0]
    results = build(ffmpeg, ffprobe)
    if args.check:
        bad = []
        for name, r in results.items():
            path = os.path.join(FIXTURE_DIR, f"{name}.json")
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
            # format.size はエンコーダの版で変わりうるので比べない。時間軸に関係する項目を比べる
            for d in (saved, r["probe"]):
                d.get("format", {}).pop("size", None)
            if saved != r["probe"]:
                bad.append(name)
        if bad:
            print("保存済みフィクスチャと一致しない:", ", ".join(bad), f"（{version}）", file=sys.stderr)
            return 1
        print(f"一致: {len(results)} 本（{version}）")
        return 0
    os.makedirs(FIXTURE_DIR, exist_ok=True)
    manifest = {"generated_with": version, "clips": {}}
    for name, r in results.items():
        text = json.dumps(r["probe"], ensure_ascii=False, separators=(",", ":"))
        with open(os.path.join(FIXTURE_DIR, f"{name}.json"), "w", encoding="utf-8") as fh:
            fh.write(text)
        manifest["clips"][name] = {"description": r["description"], "truth": r["truth"]}
    with open(os.path.join(FIXTURE_DIR, "manifest.json"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(manifest, ensure_ascii=False, indent=1))
    print(f"{len(results)} 本のフィクスチャを保存（{version}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
