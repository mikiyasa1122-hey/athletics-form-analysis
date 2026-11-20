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

# 名前: (説明, 正解と期待値, ffmpeg 引数)
# 期待値（expect）はエンジンの出力から写さず、動画の作り方から決める:
#   frames = 復号されるフレーム数, gaps = 撮影時の欠落の直後のフレームID, vfr = 可変FPSか,
#   decode_failures = 復号失敗の原本ID, discarded = edit list で再生対象外のパケット数
_SRC240 = f"testsrc2=size=320x240:rate=240:duration={DUR}"
_TAG240 = ["-movflags", "use_metadata_tags", "-metadata", "com.android.capture.fps=240.000000"]
_CFR = {"gaps": [], "vfr": False, "decode_failures": [], "discarded": 0}

CLIPS: dict[str, tuple[str, dict, list[str]]] = {
    "cfr240_tagged": (
        "240fps・撮影FPSメタデータあり（Android と同じキー）",
        {"capture_time_scale": 1, "expect": {"status": "verified", "frames": 288, **_CFR}},
        ["-f", "lavfi", "-i", _SRC240, *X264, *_TAG240],
    ),
    "cfr30_bframes": (
        "30fps・Bフレームあり（パケットは復号順、DTSは負から始まる）",
        {"capture_time_scale": 1, "expect": {"status": "container_only", "frames": 36, **_CFR}},
        ["-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=30:duration={DUR}", *X264],
    ),
    "vfr_60_30": (
        "可変FPS: 前半60fps（36枚）、後半30fps（36枚）。暗所で端末がFPSを落とした想定",
        {"capture_time_scale": 1, "expect": {"status": "container_only", "frames": 72, **_CFR, "vfr": True}},
        ["-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=60:duration={DUR}",
         # 整数で計算する（0.6/TB のような小数は浮動小数の丸めで時刻が1tickずれる）
         "-vf", "setpts='if(lt(N,36),N,2*N-36)/(60*TB)'",
         "-fps_mode", "passthrough", "-video_track_timescale", "90000", *X264],
    ),
    "vfr_120_30": (
        "可変FPS: 120fps（72枚）から30fps（72枚）へ。比が4の切り替えでも欠落にしない",
        {"capture_time_scale": 1, "expect": {"status": "container_only", "frames": 144, **_CFR, "vfr": True}},
        ["-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=120:duration={DUR}",
         "-vf", "setpts='if(lt(N,72),N,4*N-216)/(120*TB)'",
         "-fps_mode", "passthrough", "-video_track_timescale", "90000", *X264],
    ),
    "drop240": (
        "240fpsで撮影中に n=40〜63（0.1秒分）が記録されなかった（PTSはそのまま飛ぶ）",
        {"capture_time_scale": 1, "dropped_source_n": [[40, 63]],
         "expect": {"status": "container_only", "frames": 264, **_CFR, "gaps": [40]}},
        ["-f", "lavfi", "-i", _SRC240, "-vf", "select='not(between(n,40,63))'", "-fps_mode", "passthrough", *X264],
    ),
    "multi_drop240": (
        "240fpsで1枚ずつの欠落が3枚おきに5回（n=40,43,46,49,52）。端末の発熱でフレームを落とす想定",
        {"capture_time_scale": 1, "dropped_source_n": [[40, 40], [43, 43], [46, 46], [49, 49], [52, 52]],
         "expect": {"status": "container_only", "frames": 283, **_CFR, "gaps": [40, 42, 44, 46, 48]}},
        ["-f", "lavfi", "-i", _SRC240,
         "-vf", "select='not(eq(n,40)+eq(n,43)+eq(n,46)+eq(n,49)+eq(n,52))'", "-fps_mode", "passthrough", *X264],
    ),
    "drop_edges240": (
        "240fpsで2枚目（n=1）と最後から2枚目（n=286）が欠落",
        {"capture_time_scale": 1, "dropped_source_n": [[1, 1], [286, 286]],
         "expect": {"status": "container_only", "frames": 286, **_CFR, "gaps": [1, 285]}},
        ["-f", "lavfi", "-i", _SRC240, "-vf", "select='not(eq(n,1)+eq(n,286))'", "-fps_mode", "passthrough", *X264],
    ),
    "vfr_drop": (
        "60fps→30fpsに切り替わった直後（30fps区間の3枚目）に1枚欠落",
        {"capture_time_scale": 1, "dropped_source_n": [[38, 38]],
         "expect": {"status": "container_only", "frames": 71, **_CFR, "vfr": True, "gaps": [38]}},
        ["-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=60:duration={DUR}",
         "-vf", "setpts='if(lt(N,36),N,2*N-36)/(60*TB)',select='not(eq(n,38))'",
         "-fps_mode", "passthrough", "-video_track_timescale", "90000", *X264],
    ),
    "vfr_30_60": (
        "可変FPS: 30fps（36枚）から60fps（36枚）へ上がる。明るい場所に出た想定。遅い区間の末尾を欠落にしない",
        {"capture_time_scale": 1, "expect": {"status": "container_only", "frames": 72, **_CFR, "vfr": True}},
        ["-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=60:duration={DUR}",
         "-vf", "setpts='if(lt(N,36),2*N,N+36)/(60*TB)'",
         "-fps_mode", "passthrough", "-video_track_timescale", "90000", *X264],
    ),
    "vfr_60_30_60": (
        "可変FPS: 60fps（30枚）→30fps（10枚）→60fps（32枚）",
        {"capture_time_scale": 1, "expect": {"status": "container_only", "frames": 72, **_CFR, "vfr": True}},
        ["-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=60:duration={DUR}",
         "-vf", "setpts='if(lt(N,30),N,if(lt(N,40),2*N-30,N+10))/(60*TB)'",
         "-fps_mode", "passthrough", "-video_track_timescale", "90000", *X264],
    ),
    "speedup_drop": (
        "30fps→60fps に上がる直前（30fps区間の n=34）に1枚欠落",
        {"capture_time_scale": 1, "dropped_source_n": [[34, 34]],
         "expect": {"status": "container_only", "frames": 71, **_CFR, "vfr": True, "gaps": [34]}},
        ["-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=60:duration={DUR}",
         "-vf", "setpts='if(lt(N,36),2*N,N+36)/(60*TB)',select='not(eq(n,34))'",
         "-fps_mode", "passthrough", "-video_track_timescale", "90000", *X264],
    ),
    "run4_drops240": (
        "【既知の限界】240fpsで n=41〜51 を3枚に1枚だけ残す（同じ長さ＝3倍の間隔が4回続く）。"
        "FPS の切り替えと区別できないので欠落として表示しない（時刻は実PTSのまま）",
        {"capture_time_scale": 1, "known_undetected_source_n": [[41, 42], [44, 45], [47, 48], [50, 51]],
         "expect": {"status": "container_only", "frames": 280, **_CFR, "vfr": True}},
        ["-f", "lavfi", "-i", _SRC240,
         "-vf", "select='not(between(n,41,52)*not(eq(mod(n-40,3),0)))'", "-fps_mode", "passthrough", *X264],
    ),
    "slowdown_drop": (
        "【既知の限界】60fps の n=34 が欠落し、その直後の n=35 から 30fps に下がる。欠落の間隔（2/60）が"
        "新しい30fpsの間隔と同じで、間に1/60の間隔が残らないので、原理的に区別できない",
        {"capture_time_scale": 1, "known_undetected_source_n": [[34, 34]],
         "expect": {"status": "container_only", "frames": 71, **_CFR, "vfr": True}},
        ["-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=60:duration={DUR}",
         "-vf", "setpts='if(lt(N,35),N,2*N-35)/(60*TB)',select='not(eq(n,34))'",
         "-fps_mode", "passthrough", "-video_track_timescale", "90000", *X264],
    ),
    "mov600_240_tagged": (
        "240fpsを time_base 1/600 の MOV に保存（間隔が 2,3 tick の交互）。撮影FPSメタデータあり",
        {"capture_time_scale": 1, "ext": "mov", "expect": {"status": "verified", "frames": 288, **_CFR}},
        ["-f", "lavfi", "-i", _SRC240, *X264, "-video_track_timescale", "600", *_TAG240],
    ),
    "coverart": (
        "先頭にカバー画像（attached_pic）のストリームがある30fps動画",
        {"capture_time_scale": 1, "expect": {"status": "container_only", "frames": 36, **_CFR}},
        ["-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=30:duration={DUR}",
         "-f", "lavfi", "-i", "color=c=red:size=64x64:duration=0.04",
         "-map", "1:v", "-map", "0:v", "-c:v:0", "png", "-disposition:v:0", "attached_pic",
         # color の duration=0.04 で1枚だけになる。-frames:v:0 は MP4 の並べ替えで映像側に効いてしまうので使わない
         "-c:v:1", "libx264", "-pix_fmt:v:1", "yuv420p", "-g", "24", "-bf", "2"],
    ),
    "slowmo8_plain": (
        "240fps撮影を8倍に引き伸ばして30fpsタイムラインに書き出したスロー（メタデータなし）",
        {"capture_time_scale": 8, "expect": {"status": "container_only", "frames": 288, **_CFR}},
        ["-f", "lavfi", "-i", _SRC240, "-vf", "setpts=8*PTS", "-r", "30", *X264],
    ),
    "slowmo8_tagged": (
        "同じスロー書き出しに撮影FPSメタデータ（240）が付いている",
        {"capture_time_scale": 8, "expect": {"status": "slowmo_suspected", "frames": 288, **_CFR}},
        ["-f", "lavfi", "-i", _SRC240, "-vf", "setpts=8*PTS", "-r", "30", *X264, *_TAG240],
    ),
}
# 2段階で作るもの（元動画を加工する）
DERIVED = {
    "cut_copy": ("cfr30_bframes",
                 "30fpsを先頭0.3秒でカット（再エンコードなし）。edit list で先頭9パケットが再生対象外（flags D）になる",
                 {"capture_time_scale": 1, "expect": {"status": "container_only", "frames": 27, **_CFR, "discarded": 9}}),
    "corrupt": ("cfr30_bframes", "30fpsの非キーフレーム2つのパケットの中身を0で潰した（デコーダがフレームを落とす）",
                # 復号失敗の原本IDは、壊したパケットのPTSから決まる（corrupted_packet_pts）
                {"capture_time_scale": 1, "expect": {"status": "container_only", "frames": 34, "gaps": [], "vfr": False,
                                                     "discarded": 0}}),
}

KEEP_ENTRY = ("type", "media_type", "codec_type", "stream_index", "pts", "dts", "pkt_dts", "flags")
KEEP_STREAM = ("index", "codec_type", "codec_name", "time_base", "avg_frame_rate", "r_frame_rate", "tags", "disposition")


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
        path = os.path.join(VIDEO_DIR, f"{name}.{truth.get('ext', 'mp4')}")
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
        # manifest（作り方と期待値）も、作り直した内容と一致すること（レビュー N8）
        with open(os.path.join(FIXTURE_DIR, "manifest.json"), encoding="utf-8") as fh:
            saved_clips = json.load(fh)["clips"]
        for name, r in results.items():
            if saved_clips.get(name) != {"description": r["description"], "truth": r["truth"]}:
                bad.append(f"manifest:{name}")
        if set(saved_clips) != set(results):
            bad.append("manifest:clip-list")
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
