"""テスト・デモ用の合成データ。実在の選手データではない。

撮影時刻 t（秒）で定義した単純な走動作から、ffprobe 形式のフレーム情報と姿勢推定JSONを作る。
真値（接地時刻・接地時間・ピッチ）が分かっているので、時間軸処理の検証に使える。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

GROUND_Y = 900.0
TIME_BASE_DEN = 90000


@dataclass
class SprintModel:
    step_period: float = 0.22      # 片脚接地から反対脚接地まで（ピッチ ≒ 4.545 歩/秒）
    contact_time: float = 0.10
    first_contact: float = 0.0503  # 右足の最初の接地時刻（フレーム境界と一致させない）
    speed_px: float = 600.0

    @property
    def cadence(self) -> float:
        return 1.0 / self.step_period

    def contact_start(self, side: str, t: float) -> float | None:
        """t が接地中なら、その接地の開始時刻。"""
        offset = self.first_contact + (0 if side == "right" else self.step_period)
        k = math.floor((t - offset) / (2 * self.step_period))
        start = offset + k * 2 * self.step_period
        return start if 0 <= t - start < self.contact_time and start >= 0 else None

    def true_contacts(self, side: str, until: float) -> list[tuple[float, float]]:
        offset = self.first_contact + (0 if side == "right" else self.step_period)
        out = []
        k = 0
        while offset + k * 2 * self.step_period < until:
            s = offset + k * 2 * self.step_period
            out.append((s, s + self.contact_time))
            k += 1
        return out

    def pose_at(self, t: float) -> dict[str, list[float]]:
        hip_x = 250 + self.speed_px * t
        kps: dict[str, list[float]] = {}
        lean_dx = 60.0   # 進行方向（右）へ前傾
        for side, dx in (("left", -4.0), ("right", 4.0)):
            start = self.contact_start(side, t)
            if start is not None:
                u = (t - start) / self.contact_time
                ankle_x, ankle_y = hip_x + 60 - 120 * u, GROUND_Y - 12
                knee = (hip_x + 45 - 30 * u, 760.0)
            else:
                phase = ((t - self.first_contact) / (2 * self.step_period)) % 1.0
                lift = 110 * abs(math.sin(math.pi * phase))
                ankle_x, ankle_y = hip_x - 40 + 80 * math.cos(math.pi * phase), GROUND_Y - 30 - lift
                knee = (hip_x + 50 * math.sin(math.pi * phase), 740.0)
            hip = (hip_x + dx, 600.0)
            kps[f"{side}_hip"] = [hip[0], hip[1], 0.9]
            kps[f"{side}_knee"] = [knee[0] + dx, knee[1], 0.9]
            kps[f"{side}_ankle"] = [ankle_x + dx, ankle_y, 0.9]
            kps[f"{side}_heel"] = [ankle_x + dx - 15, ankle_y + 10, 0.85]
            kps[f"{side}_toe"] = [ankle_x + dx + 25, ankle_y + 12, 0.85]
            kps[f"{side}_shoulder"] = [hip_x + lean_dx + dx, 380.0, 0.9]
        kps["nose"] = [hip_x + lean_dx + 30, 300.0, 0.9]
        return kps


@dataclass
class CaptureSpec:
    capture_fps: float = 240.0
    duration: float = 1.2
    container_time_scale: float = 1.0   # >1 でスロー書き出し（元の1秒が scale 秒に引き延ばされる）
    write_capture_fps_tag: bool = False
    vfr_jitter: bool = False
    occluded_frames: set[int] = field(default_factory=set)
    blurred_frames: set[int] = field(default_factory=set)
    drop_frames: set[int] = field(default_factory=set)   # 撮影時に記録されなかったフレーム（欠落）


def capture_times(spec: CaptureSpec) -> list[float]:
    n = int(spec.duration * spec.capture_fps)
    times = []
    t = 0.0
    base = 1.0 / spec.capture_fps
    for i in range(n):
        if i not in spec.drop_frames:
            times.append(t)
        # 決定的な揺らぎ（-20%〜+20%）で可変フレームレートを再現
        t += base * (1 + 0.2 * math.sin(i * 1.7)) if spec.vfr_jitter else base
    return times


def make_dataset(spec: CaptureSpec, model: SprintModel | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    model = model or SprintModel()
    times = capture_times(spec)
    frames = []
    pose_frames = []
    for i, t in enumerate(times):
        pts = round(t * spec.container_time_scale * TIME_BASE_DEN)
        frames.append({"media_type": "video", "stream_index": 0, "pts": pts, "pkt_dts": pts})
        flags = []
        kps: dict[str, list[float] | None] = dict(model.pose_at(t))  # type: ignore[arg-type]
        if i in spec.occluded_frames:
            flags.append("occluded")
            for k in list(kps):
                if k.endswith(("heel", "toe", "ankle")):
                    kps[k] = None
        if i in spec.blurred_frames:
            flags.append("motion_blur")
        pose_frames.append({"source_frame_id": i, "keypoints": kps, "flags": flags})
    out_fps = spec.capture_fps / spec.container_time_scale
    stream: dict[str, Any] = {
        "index": 0, "codec_type": "video", "codec_name": "h264", "width": 1280, "height": 1000,
        "time_base": f"1/{TIME_BASE_DEN}", "avg_frame_rate": f"{round(out_fps * 1000)}/1000",
        "r_frame_rate": f"{round(out_fps * 1000)}/1000", "tags": {},
    }
    if spec.write_capture_fps_tag:
        stream["tags"]["com.android.capture.fps"] = f"{spec.capture_fps:.6f}"
    probe = {"streams": [stream], "frames": frames, "format": {"tags": {}}}
    pose = {"model": {"model_id": "synthetic-fixture", "weights_version": "0", "config": {"note": "合成データ"}},
            "frames": pose_frames}
    return probe, pose
