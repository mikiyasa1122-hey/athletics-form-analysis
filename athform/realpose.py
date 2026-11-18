"""実動画の姿勢推定（RTMPose / rtmlib）、対象人物の追跡、地面の推定（FR-POSE-001〜006, FR-TIME-005）。

このモジュールだけが numpy / opencv / rtmlib を使う（.venv にインストール）。他の athform は標準ライブラリのみ。

- フレームは ffmpeg で提示順に全て復号する（-fps_mode passthrough。間引き・複製をしない）。
  復号できた枚数が原本フレーム索引と一致しなければ、ずれた対応を作らないよう処理を止める。
- 重みは OpenMMLab 配布の RTMPose（body7 で学習、Halpe26 の点）。学習データに研究・非商用限定のものが
  含まれうるので、個人利用を前提とする（README 参照）。
"""

from __future__ import annotations

import math
import os
import struct
import subprocess
from dataclasses import dataclass, field
from statistics import median
from typing import Any, Callable, Iterator

from .events import DetectorConfig
from .pose import KEYPOINTS, Keypoint, ModelInfo, PoseFrame
from .timebase import FrameIndex

# Halpe26 の番号 → 共通スキーマ（足先は母趾を使う）
HALPE26 = {
    0: "nose", 5: "left_shoulder", 6: "right_shoulder", 7: "left_elbow", 8: "right_elbow",
    9: "left_wrist", 10: "right_wrist", 11: "left_hip", 12: "right_hip", 13: "left_knee", 14: "right_knee",
    15: "left_ankle", 16: "right_ankle", 20: "left_toe", 21: "right_toe", 24: "left_heel", 25: "right_heel",
}
MODES = {
    "performance": "YOLOX-x + RTMPose-x 384x288（精度優先）",
    "balanced": "YOLOX-m + RTMPose-m 256x192（プレビュー向け）",
}
VIEW_MAX_WIDTH = 1280   # 表示用 JPEG の最大幅（座標は原寸のまま。表示側で縮尺を掛ける）


class PoseRunError(RuntimeError):
    pass


class Cancelled(Exception):
    pass


@dataclass
class TrackState:
    center: tuple[float, float] | None = None
    size: float | None = None
    lost: int = 0


@dataclass
class PoseRunResult:
    model: ModelInfo
    frames: dict[int, PoseFrame]
    image_size: tuple[int, int]
    view_scale: float
    failed: list[int] = field(default_factory=list)

    @property
    def model_info(self) -> ModelInfo:
        return self.model


class StoredPoseAdapter:
    """保存済みの推定結果を PoseAdapter として渡す（訂正後の再計算で再推論しない）。"""

    def __init__(self, model: ModelInfo, frames: dict[int, PoseFrame]):
        self._model, self._frames = model, frames

    @property
    def model(self) -> ModelInfo:
        return self._model

    def frame_ids(self) -> set[int]:
        return set(self._frames)

    def infer(self, source_frame_id: int) -> PoseFrame | None:
        return self._frames.get(source_frame_id)


def iter_bmp_frames(ffmpeg: str, video: str) -> Iterator[bytes]:
    """ffmpeg で全フレームを提示順に BMP として取り出す。BMP はヘッダーにファイルサイズがあるので確実に区切れる。"""
    cmd = [ffmpeg, "-v", "error", "-i", video, "-map", "0:V:0", "-fps_mode", "passthrough",
           "-f", "image2pipe", "-c:v", "bmp", "-"]
    # stderr は読まない（壊れた動画で大量に出るとパイプが詰まって止まる）。復号できなかった分は枚数照合で検出する
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert proc.stdout is not None
    try:
        while True:
            head = proc.stdout.read(6)
            if not head:
                break
            if len(head) < 6 or head[:2] != b"BM":
                raise PoseRunError("ffmpeg の出力が BMP として読めない")
            size = struct.unpack("<I", head[2:6])[0]
            rest = proc.stdout.read(size - 6)
            if len(rest) != size - 6:
                raise PoseRunError("ffmpeg の出力が途中で切れた")
            yield head + rest
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()   # 途中で止めた（取消・エラー）ときに ffmpeg を残さない
        proc.wait()


def _person_stats(kps, scores, thr: float) -> tuple[tuple[float, float], float] | None:
    pts = [(float(x), float(y)) for (x, y), s in zip(kps, scores) if s >= thr]
    if len(pts) < 4:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    hips = [kps[i] for i in (11, 12) if scores[i] >= thr]
    if hips:
        cx = sum(float(h[0]) for h in hips) / len(hips)
        cy = sum(float(h[1]) for h in hips) / len(hips)
    else:
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
    size = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
    return (cx, cy), size


def choose_target(people, state: TrackState, thr: float, reacquire_after: int = 15) -> int | None:
    """解析対象の人物を選ぶ（FR-POSE-002）。最初は最も大きく映った人。以降は前のフレームの位置に最も近い人。"""
    stats = [(_person_stats(k, s, thr), i) for i, (k, s) in enumerate(people)]
    stats = [(st, i) for st, i in stats if st is not None]
    if not stats:
        state.lost += 1
        return None
    if state.center is None or state.lost > reacquire_after:
        (center, size), idx = max(stats, key=lambda t: t[0][1])
    else:
        def dist(t):
            (c, _), _ = t
            return math.hypot(c[0] - state.center[0], c[1] - state.center[1])
        best = min(stats, key=dist)
        limit = 0.6 * (state.size or best[0][1]) * (1 + 0.5 * state.lost)
        if dist(best) > limit:
            state.lost += 1
            return None
        (center, size), idx = best
    state.center, state.size, state.lost = center, size, 0
    return idx


def to_pose_frame(fid: int, kps, scores) -> PoseFrame:
    points: dict[str, Keypoint | None] = {name: None for name in KEYPOINTS}
    for h, name in HALPE26.items():
        x, y, c = float(kps[h][0]), float(kps[h][1]), float(scores[h])
        if math.isfinite(x) and math.isfinite(y) and math.isfinite(c):
            points[name] = Keypoint(x, y, max(0.0, min(1.0, c)))   # SimCC の値は1を超えうるので丸める
    return PoseFrame(fid, points, [])


def run_pose(ffmpeg: str, video: str, index: FrameIndex, out_dir: str, mode: str = "performance",
             progress: Callable[[int, int], None] | None = None,
             cancelled: Callable[[], bool] | None = None, min_confidence: float = 0.3) -> PoseRunResult:
    import cv2  # noqa: PLC0415  （.venv の外では読み込まない）
    import numpy as np  # noqa: PLC0415
    from rtmlib import BodyWithFeet  # noqa: PLC0415

    if mode not in MODES:
        raise PoseRunError(f"不明なモード: {mode}")
    model = BodyWithFeet(mode=mode, backend="onnxruntime", device="cpu")
    cfg = BodyWithFeet.MODE[mode]
    info = ModelInfo(
        model_id="rtmlib.BodyWithFeet(halpe26)",
        weights_version=f"{os.path.basename(cfg['det'])} + {os.path.basename(cfg['pose'])}",
        config={"mode": mode, "backend": "onnxruntime", "device": "cpu", "min_confidence": min_confidence,
                "tracking": "largest-first, nearest-center"},
    )
    os.makedirs(out_dir, exist_ok=True)
    total = len(index.frames)
    frames: dict[int, PoseFrame] = {}
    failed: list[int] = []
    state = TrackState()
    size = (0, 0)
    scale = 1.0
    n = 0
    for n, bmp in enumerate(iter_bmp_frames(ffmpeg, video)):
        if cancelled and cancelled():
            raise Cancelled()
        if n >= total:
            raise PoseRunError(f"復号したフレームが原本フレーム索引（{total}枚）より多い。対応がずれるので止める")
        img = cv2.imdecode(np.frombuffer(bmp, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise PoseRunError(f"{n} 枚目のフレームを読めない")
        h, w = img.shape[:2]
        size = (w, h)
        fid = index.frames[n].source_frame_id
        scale = min(1.0, VIEW_MAX_WIDTH / w)
        view = cv2.resize(img, (round(w * scale), round(h * scale))) if scale < 1 else img
        ok, jpg = cv2.imencode(".jpg", view, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if ok:
            with open(os.path.join(out_dir, f"{fid}.jpg"), "wb") as fh:
                fh.write(jpg.tobytes())
        kps, scores = model(img)
        people = list(zip(kps, scores)) if len(kps) else []
        target = choose_target(people, state, min_confidence)
        if target is None:
            pf = PoseFrame(fid, {name: None for name in KEYPOINTS}, ["tracking_lost"] if people else ["no_person"])
            failed.append(fid)
        else:
            pf = to_pose_frame(fid, *people[target])
        frames[fid] = pf
        if progress:
            progress(n + 1, total)
    if n + 1 != total:
        raise PoseRunError(f"復号したフレーム数（{n + 1}）が原本フレーム索引（{total}）と一致しない。対応がずれるので止める")
    return PoseRunResult(info, frames, size, scale, failed)


def estimate_ground(frames: dict[int, PoseFrame], min_conf: float = 0.3) -> tuple[DetectorConfig, dict[str, Any]]:
    """足の最下点から地面の線（高さと傾き）と接地とみなす幅を推定する。

    走っている人の足が最も下に来るのは接地中。画面の各位置で最も下にある足の点を集め、直線で近似する。
    カメラが大きく動く（パン・ズーム）動画では成り立たないので、推定の根拠を返し、画面で上書きできるようにする。
    """
    pts: list[tuple[float, float]] = []
    heights: list[float] = []
    for pf in frames.values():
        for side in ("left", "right"):
            feet = [pf.get(f"{side}_{p}", min_conf) for p in ("heel", "toe", "ankle")]
            feet = [p for p in feet if p is not None]
            if feet:
                low = max(feet, key=lambda p: p.y)
                pts.append((low.x, low.y))
        sh = [pf.get(n, min_conf) for n in ("left_shoulder", "right_shoulder")]
        an = [pf.get(n, min_conf) for n in ("left_ankle", "right_ankle")]
        sh, an = [p for p in sh if p], [p for p in an if p]
        if sh and an:
            heights.append(max(p.y for p in an) - min(p.y for p in sh))
    if len(pts) < 10:
        raise PoseRunError("足が見えるフレームが少なすぎて、地面の位置を推定できない")
    body = median(heights) if heights else 0.0
    band = max(4.0, 0.03 * body)
    # 下側の包絡: x を区切り、各区間で最も下の点を集める
    xs = sorted(p[0] for p in pts)
    x_lo, x_hi = xs[0], xs[-1]
    bins = 12
    width = max(1e-6, (x_hi - x_lo) / bins)
    lows: dict[int, tuple[float, float]] = {}
    for x, y in pts:
        b = min(bins - 1, int((x - x_lo) / width))
        if b not in lows or y > lows[b][1]:
            lows[b] = (x, y)
    env = list(lows.values())
    # 区間によっては足が浮いている（遊脚）点しかない。直線を当てて、線より明らかに上（画面で小さい y）の点を
    # 除いて当て直すことを繰り返し、接地している点だけで地面を決める
    slope, x0 = 0.0, sum(x for x, _ in env) / len(env)
    use = env
    for _ in range(4):
        x0 = sum(x for x, _ in use) / len(use)
        if len(use) >= 3 and (x_hi - x_lo) > body > 0:
            my = sum(y for _, y in use) / len(use)
            den = sum((x - x0) ** 2 for x, _ in use)
            slope = sum((x - x0) * (y - my) for x, y in use) / den if den > 0 else 0.0
            slope = max(-0.2, min(0.2, slope))
        y0 = max(y - slope * (x - x0) for x, y in use)   # 下側の包絡なので、最も下の点を基準にする
        kept = [(x, y) for x, y in use if y - slope * (x - x0) >= y0 - band]   # 接地幅より上は遊脚とみなす
        if len(kept) == len(use) or len(kept) < 2:
            break
        use = kept
    # 線の高さは、接地とみなした点の中央値（外れ値に強い）
    y0 = median(y - slope * (x - x0) for x, y in use)
    cfg = DetectorConfig(ground_y=y0, contact_band_px=band, min_confidence=min_conf, ground_slope=slope, ground_x0=x0)
    diag = {"method": "lower-envelope line fit", "envelope_points": len(env), "contact_points_used": len(use), "foot_points": len(pts),
            "median_body_height_px": round(body, 1), "slope_clamped_to": 0.2}
    return cfg, diag
