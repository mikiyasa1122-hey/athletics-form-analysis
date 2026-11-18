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
import shutil
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


GATE = 0.6          # 前のフレームの位置から、体の大きさ×この倍率以内の人だけを同じ人とみなす
SIZE_RATIO = 1.5    # 体の大きさがこの比を超えて変わる人は別人とみなす


def choose_target(people, state: TrackState, thr: float, reacquire_after: int = 15) -> tuple[int | None, bool]:
    """解析対象の人物を選ぶ（FR-POSE-002）。(選んだ人の番号, 取り直したか) を返す。

    - 最初は最も大きく映った人。以降は前の位置に近く、大きさも近い人。範囲は見失っても広げない（レビュー B4）
    - reacquire_after フレーム続けて見失ったら、最も大きい人を選び直す。別人かもしれないので「取り直し」と印を付け、
      解析ではそこで観測を区切る（同じ人として接地や指標をつながない）
    """
    stats = [(_person_stats(k, s, thr), i) for i, (k, s) in enumerate(people)]
    stats = [(st, i) for st, i in stats if st is not None]
    if not stats:
        state.lost += 1
        return None, False
    if state.center is None:
        (center, size), idx = max(stats, key=lambda t: t[0][1])
        state.center, state.size, state.lost = center, size, 0
        return idx, False
    prev_c, prev_s = state.center, state.size or 1.0

    def same_person(t) -> bool:
        (c, sz), _ = t
        close = math.hypot(c[0] - prev_c[0], c[1] - prev_c[1]) <= GATE * prev_s
        return close and 1 / SIZE_RATIO <= sz / prev_s <= SIZE_RATIO
    candidates = [t for t in stats if same_person(t)]
    if candidates:
        (center, size), idx = min(candidates, key=lambda t: math.hypot(t[0][0][0] - prev_c[0], t[0][0][1] - prev_c[1]))
        state.center, state.size, state.lost = center, size, 0
        return idx, False
    state.lost += 1
    if state.lost > reacquire_after:
        (center, size), idx = max(stats, key=lambda t: t[0][1])
        state.center, state.size, state.lost = center, size, 0
        return idx, True
    return None, False


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
                "tracking": f"largest-first, nearest within {GATE}x body, size ratio <= {SIZE_RATIO}, reacquire marked"},
    )
    # 画像は一時フォルダに書き、枚数の照合が通ってから入れ替える。途中で失敗・取消しても、
    # 既存の版の画像（原本フレームIDで対応）を書き換えない（レビュー N5）
    parent = os.path.dirname(out_dir)
    if not os.path.isdir(parent):
        raise PoseRunError("動画のフォルダがない（削除された可能性）")
    tmp_dir = out_dir + ".partial"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir)
    total = len(index.frames)
    frames: dict[int, PoseFrame] = {}
    failed: list[int] = []
    state = TrackState()
    size = (0, 0)
    scale = 1.0
    count = 0   # 実際に取り出した枚数（0枚でも正しく数える）
    try:
        for bmp in iter_bmp_frames(ffmpeg, video):
            if cancelled and cancelled():
                raise Cancelled()
            if count >= total:
                raise PoseRunError(f"復号したフレームが原本フレーム索引（{total}枚）より多い。対応がずれるので止める")
            img = cv2.imdecode(np.frombuffer(bmp, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                raise PoseRunError(f"{count} 枚目のフレームを読めない")
            h, w = img.shape[:2]
            size = (w, h)
            fid = index.frames[count].source_frame_id
            scale = min(1.0, VIEW_MAX_WIDTH / w)
            view = cv2.resize(img, (round(w * scale), round(h * scale))) if scale < 1 else img
            ok, jpg = cv2.imencode(".jpg", view, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                with open(os.path.join(tmp_dir, f"{fid}.jpg"), "wb") as fh:
                    fh.write(jpg.tobytes())
            kps, scores = model(img)
            people = list(zip(kps, scores)) if len(kps) else []
            target, reacquired = choose_target(people, state, min_confidence)
            if target is None:
                pf = PoseFrame(fid, {name: None for name in KEYPOINTS}, ["tracking_lost"] if people else ["no_person"])
                failed.append(fid)
            else:
                pf = to_pose_frame(fid, *people[target])
                if reacquired:
                    pf.flags.append("reacquired")
            frames[fid] = pf
            count += 1
            if progress:
                progress(count, total)
        if count != total:
            raise PoseRunError(f"復号したフレーム数（{count}）が原本フレーム索引（{total}）と一致しない。対応がずれるので止める")
        if not os.path.isdir(parent):
            raise PoseRunError("動画のフォルダがない（解析中に削除された可能性）")
        shutil.rmtree(out_dir, ignore_errors=True)
        os.replace(tmp_dir, out_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return PoseRunResult(info, frames, size, scale, failed)


def estimate_ground(frames: dict[int, PoseFrame], min_conf: float = 0.3) -> tuple[DetectorConfig, dict[str, Any]]:
    """足の最下点から地面の線（高さと傾き）と接地とみなす幅を推定する。

    走っている人の足が最も下に来るのは接地中。画面の各位置で最も下にある足の点を集め、直線で近似する。
    カメラが大きく動く（パン・ズーム）動画では成り立たないので、推定の根拠を返し、画面で上書きできるようにする。
    """
    pts: list[tuple[float, float]] = []
    lows_by_frame: list[tuple[int, float]] = []   # （原本フレームID, そのフレームで最も下の足）: 時間方向の変化を見る
    heights: list[float] = []
    for pf in frames.values():
        for side in ("left", "right"):
            feet = [pf.get(f"{side}_{p}", min_conf) for p in ("heel", "toe", "ankle")]
            feet = [p for p in feet if p is not None]
            if feet:
                low = max(feet, key=lambda p: p.y)
                pts.append((low.x, low.y))
                lows_by_frame.append((pf.source_frame_id, low.y))
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
    problems = ground_problems(lows_by_frame, len(use), slope, band, body)
    diag = {"method": "lower-envelope line fit", "envelope_points": len(env), "contact_points_used": len(use), "foot_points": len(pts),
            "median_body_height_px": round(body, 1), "slope_clamped_to": 0.2}
    diag["problems"] = problems
    return cfg, diag


MIN_CONTACT_POINTS = 4
MIN_BODY_PX = 120.0


def ground_problems(lows_by_frame: list[tuple[int, float]], used: int, slope: float, band: float,
                    body: float) -> list[str]:
    """自動推定した地面の線を信用できない理由（レビュー B5）。空なら問題なし。"""
    problems = []
    if used < MIN_CONTACT_POINTS:
        problems.append(f"接地とみなせる点が少ない（{used}点）。地面の位置が定まらない")
    if abs(slope) >= 0.2 - 1e-9:
        problems.append("傾きが上限に達した。カメラの傾き・動きが大きい可能性")
    if 0 < body < MIN_BODY_PX:
        problems.append(f"人物が小さい（体の高さ {body:.0f}px）。点の位置の誤差が大きくなる")
    ordered = [y for _, y in sorted(lows_by_frame)]
    if len(ordered) >= 20:
        half = len(ordered) // 2

        def low_level(ys: list[float]) -> float:
            ys = sorted(ys)
            return ys[int(len(ys) * 0.9)]   # 最も下の足のさらに下側（接地中の高さ）
        drift = abs(low_level(ordered[:half]) - low_level(ordered[half:]))
        if drift > 2 * band:
            problems.append(f"前半と後半で地面の高さが {drift:.0f}px 違う。カメラが動いている可能性")
    return problems
