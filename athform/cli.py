"""コマンドライン入口。

  python -m athform probe VIDEO --out probe.json        # ffprobe で原本フレーム情報を取得（ffprobe が必要）
  python -m athform analyze --probe probe.json --pose pose.json --video VIDEO --ground-y 900 \\
        --view side --direction left_to_right --out snapshot.json
  python -m athform demo --out web/demo                 # 合成データのデモ解析を作る
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any, Callable

from .analysis import AnalysisInput, SamplingPolicy, analyze, file_sha256
from .events import Correction, DetectorConfig
from .metrics import CaptureConditions
from .pose import FixturePoseAdapter
from .synth import GROUND_Y, CaptureSpec, make_dataset
from .timebase import build_frame_index
from .tools import find_tool

Runner = Callable[[list[str]], str]


def ffprobe_command(video: str) -> list[str]:
    # -show_frames は提示順に復号済みフレームを返す。間引きはしない（FR-TIME-005）
    # -show_packets はコンテナ上の全フレームのPTSと flags（D = edit list で再生対象外）。
    # 復号できなかったフレームは frames から消えるだけなので、パケットとの差分で見つける。
    # 両方を指定すると ffprobe 9.0 は packets_and_frames 形式で出力する（実機で確認済み）。
    # V:0 = カバー画像（attached_pic）を除いた最初の映像ストリーム。
    # パスは -i の値として渡し、'-' で始まるファイル名がオプションと解釈されないようにする。
    return ["ffprobe", "-v", "error", "-select_streams", "V:0", "-show_streams", "-show_format",
            "-show_entries", "frame=media_type,stream_index,pts,pkt_dts:packet=codec_type,stream_index,pts,dts,flags",
            "-show_packets", "-show_frames", "-of", "json", "-i", video]


def _default_runner(cmd: list[str]) -> str:
    exe = find_tool(cmd[0])
    if exe is None:
        raise SystemExit(f"{cmd[0]} が見つからない。FFmpeg をインストールするか ATHFORM_{cmd[0].upper()} で場所を指定する")
    return subprocess.run([exe, *cmd[1:]], check=True, capture_output=True, text=True, encoding="utf-8").stdout


def probe_video(video: str, runner: Runner = _default_runner) -> dict[str, Any]:
    if not os.path.isfile(video):
        raise SystemExit(f"動画がない: {video}")
    return json.loads(runner(ffprobe_command(video)))


def check_probe_matches_video(probe: dict[str, Any], video: str) -> None:
    """別の動画の probe を渡していないか確かめる（ffprobe の format.size とファイルサイズ）。"""
    size = (probe.get("format") or {}).get("size")
    if size is None:
        raise SystemExit("probe に format.size がない。`athform probe` で作った probe を使う")
    if int(size) != os.path.getsize(video):
        raise SystemExit(f"probe（{size} bytes）と動画（{os.path.getsize(video)} bytes）が一致しない。別の動画の probe の可能性")


def _load(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _dump(obj: Any, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=1)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


DEMOS = {
    "sprint-240fps": ("240fps・撮影メタデータあり", CaptureSpec(capture_fps=240, write_capture_fps_tag=True)),
    "sprint-30fps": ("30fps（時間分解能が粗い）", CaptureSpec(capture_fps=30)),
    "sprint-vfr": ("可変フレームレート＋一部遮蔽", CaptureSpec(capture_fps=120, vfr_jitter=True,
                                                    occluded_frames={40, 41}, blurred_frames={70})),
    "sprint-long-occlusion": ("240fps・1歩分を覆う遮蔽と復号失敗1枚",
                              CaptureSpec(capture_fps=240, write_capture_fps_tag=True,
                                          occluded_frames=set(range(84, 172)), decode_fail_frames={30})),
    "sprint-slowmo-export": ("240fps撮影を30fpsタイムラインに書き出したスロー動画（メタデータなし）",
                             CaptureSpec(capture_fps=240, container_time_scale=8.0)),
}


def build_demo(out_dir: str) -> list[str]:
    written = []
    catalog = []
    for name, (label, spec) in DEMOS.items():
        probe, pose = make_dataset(spec)
        inp = AnalysisInput(
            source_video_hash=f"synthetic:{name}",
            index=build_frame_index(probe),
            pose=FixturePoseAdapter(pose),
            detector=DetectorConfig(ground_y=GROUND_Y),
            conditions=CaptureConditions(camera_view="side", direction="left_to_right"),
        )
        snap = analyze(inp)
        snap["demo_label"] = label
        path = os.path.join(out_dir, f"{name}.json")
        _dump(snap, path)
        written.append(path)
        catalog.append({"file": f"{name}.json", "label": label})
    _dump(catalog, os.path.join(out_dir, "index.json"))
    return written


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="athform")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("probe")
    sp.add_argument("video")
    sp.add_argument("--out", required=True)

    sa = sub.add_parser("analyze")
    sa.add_argument("--probe", required=True)
    sa.add_argument("--pose", required=True)
    g = sa.add_mutually_exclusive_group(required=True)
    g.add_argument("--video", help="原本動画（ハッシュを計算する）")
    g.add_argument("--video-hash")
    sa.add_argument("--ground-y", type=float, required=True)
    sa.add_argument("--view", choices=["side", "front", "rear", "oblique"])
    sa.add_argument("--direction", choices=["left_to_right", "right_to_left"])
    sa.add_argument("--capture-fps", help="利用者申告の撮影FPS（精度の証明には使わない）")
    sa.add_argument("--slowmo", action="store_true", help="スロー書き出し動画であると申告する")
    sa.add_argument("--corrections", help="手動訂正のJSON配列")
    sa.add_argument("--preview-every", type=int, default=0, help="プレビュー用にN枚に1枚だけ解析する")
    sa.add_argument("--out", required=True)

    sd = sub.add_parser("demo")
    sd.add_argument("--out", required=True)

    args = p.parse_args(argv)
    if args.cmd == "probe":
        _dump(probe_video(args.video), args.out)
    elif args.cmd == "analyze":
        sampling = SamplingPolicy()
        if args.preview_every:
            sampling = SamplingPolicy("preview_every_nth", args.preview_every, "CLIでプレビュー指定")
        corrections = [Correction.from_json(c) for c in _load(args.corrections)] if args.corrections else []
        probe = _load(args.probe)
        if args.video:
            check_probe_matches_video(probe, args.video)
        inp = AnalysisInput(
            source_video_hash=file_sha256(args.video) if args.video else args.video_hash,
            index=build_frame_index(probe, declared_capture_fps=args.capture_fps,
                                    declared_slowmo=args.slowmo),
            pose=FixturePoseAdapter(_load(args.pose)),
            detector=DetectorConfig(ground_y=args.ground_y),
            conditions=CaptureConditions(camera_view=args.view, direction=args.direction),
            sampling=sampling,
        )
        _dump(analyze(inp, corrections), args.out)
    elif args.cmd == "demo":
        for path in build_demo(args.out):
            print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
