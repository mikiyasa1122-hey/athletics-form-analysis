"""解析パイプラインと不変スナップショット（§7.1, §12.4, FR-TIME-005/006/014, NFR-TIME-002, ARCH-011/014）。

`analyze()` は再生倍率を引数に取らない。再生倍率はプレイヤーのUI状態であり、解析の入力ではない。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from . import ALGORITHM_VERSION, SNAPSHOT_SCHEMA_VERSION
from .events import Correction, DetectorConfig, EventLayers, detect_events
from .metrics import (PLAUSIBLE_MIN_CADENCE, CaptureConditions, MetricResult, apparent_cadence, cadence,
                      cap_for_preview, contact_times, knee_angles, trunk_leans)
from .pose import PoseAdapter, PoseFrame
from .timebase import FrameIndex, downgrade_capture_time

ALL_SOURCE_FRAMES = "all_source_frames"
PREVIEW_EVERY_NTH = "preview_every_nth"


class AnalysisError(ValueError):
    pass


@dataclass
class SamplingPolicy:
    mode: str = ALL_SOURCE_FRAMES
    every_nth: int = 1
    reason: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"mode": self.mode, "every_nth": self.every_nth, "reason": self.reason}


@dataclass
class AnalysisInput:
    source_video_hash: str
    index: FrameIndex
    pose: PoseAdapter
    detector: DetectorConfig
    conditions: CaptureConditions
    sampling: SamplingPolicy = field(default_factory=SamplingPolicy)


def _select_frames(inp: AnalysisInput) -> list[int]:
    ids = [f.source_frame_id for f in inp.index.frames]
    if inp.sampling.mode == ALL_SOURCE_FRAMES:
        return ids
    if inp.sampling.mode == PREVIEW_EVERY_NTH:
        if inp.sampling.every_nth < 2:
            raise AnalysisError("preview_every_nth は every_nth >= 2 を指定する")
        if not inp.sampling.reason.strip():
            raise AnalysisError("間引く場合は理由を記録する（FR-TIME-005）")
        return ids[::inp.sampling.every_nth]
    raise AnalysisError(f"不明なサンプリング方式: {inp.sampling.mode}")


def _subindex(index: FrameIndex, ids: list[int]) -> FrameIndex:
    keep = set(ids)
    return FrameIndex(frames=[f for f in index.frames if f.source_frame_id in keep], time_base=index.time_base,
                      nominal_fps=index.nominal_fps, capture_fps=index.capture_fps,
                      capture_time_status=index.capture_time_status,
                      capture_time_notes=list(index.capture_time_notes), decode_failures=list(index.decode_failures))


def run_pose(inp: AnalysisInput) -> tuple[dict[int, PoseFrame], dict[str, Any]]:
    selected = _select_frames(inp)
    known = getattr(inp.pose, "frame_ids", None)
    if callable(known):
        foreign = sorted(set(known()) - {f.source_frame_id for f in inp.index.frames})
        if foreign:
            raise AnalysisError(f"原本フレーム索引にないフレームの姿勢がある: {foreign[:10]}（FR-TIME-007）")
    poses: dict[int, PoseFrame] = {}
    failed: list[int] = []
    for fid in selected:
        result = inp.pose.infer(fid)
        if result is None:
            failed.append(fid)
            continue
        if result.source_frame_id != fid:
            raise AnalysisError(f"アダプタが別フレームの結果を返した: 要求 {fid}, 返却 {result.source_frame_id}")
        poses[fid] = result
    total = len(inp.index.frames)
    completeness = {
        "source_frame_count": total + len(inp.index.decode_failures),
        "decoded_frame_count": total,
        "decode_failed_presentation_indexes": list(inp.index.decode_failures),
        "selected_frame_count": len(selected),
        "analyzed_frame_count": len(poses),
        "pose_failed_frame_ids": failed,
        "skipped_by_sampling": total - len(selected),
    }
    return poses, completeness


def analyze(inp: AnalysisInput, corrections: list[Correction] | None = None) -> dict[str, Any]:
    poses, completeness = run_pose(inp)
    # 入力の索引は変更しない（同じ入力で何度でも同じ結果: FR-TIME-014）
    work_index = _subindex(inp.index, [f.source_frame_id for f in inp.index.frames])
    timing_index = work_index if inp.sampling.mode == ALL_SOURCE_FRAMES else _subindex(work_index, list(poses))
    auto_events = detect_events(timing_index, poses, inp.detector)
    layers = EventLayers(auto_events, list(corrections or []))
    effective = layers.effective(timing_index)

    # 種目として不自然に遅い動き → スロー書き出しを疑い、実時間指標を出さない（AC-015）
    floor = PLAUSIBLE_MIN_CADENCE.get(inp.conditions.discipline)
    seen = apparent_cadence(effective)
    if floor is not None and seen is not None and seen < floor:
        downgrade_capture_time(timing_index,
                               f"動画時間上のピッチ {seen:.2f} 歩/秒 が {inp.conditions.discipline} として"
                               f"不自然に低い（下限 {floor}）。スロー書き出しの疑い")
        if timing_index is not work_index:
            downgrade_capture_time(work_index, timing_index.capture_time_notes[-1])

    metrics: list[MetricResult] = []
    metrics += contact_times(timing_index, effective)
    metrics.append(cadence(timing_index, effective))
    metrics += knee_angles(effective, poses, inp.conditions, inp.detector.min_confidence)
    metrics += trunk_leans(effective, poses, inp.conditions, inp.detector.min_confidence)
    if inp.sampling.mode != ALL_SOURCE_FRAMES:
        cap_for_preview(metrics)

    body = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "source_video_hash": inp.source_video_hash,
        "model": inp.pose.model.to_json(),
        "detector": inp.detector.to_json(),
        "capture_conditions": inp.conditions.to_json(),
        "analysis_sampling_policy": inp.sampling.to_json(),
        "preview": inp.sampling.mode != ALL_SOURCE_FRAMES,
        "frame_completeness": completeness,
        "capture_quality": work_index.quality_report(),
        "frames": [f.to_json() for f in work_index.frames],
        "pose_raw": [poses[fid].to_json() for fid in sorted(poses)],
        "temporal_filter": {"method": "none", "note": "生座標をそのまま使用。平滑化は別レイヤー（未実装）"},
        "events": {
            "auto": [e.to_json() for e in auto_events],
            "corrections": [c.to_json() for c in layers.corrections],
            "effective": [e.to_json() for e in effective],
        },
        "metrics": [m.to_json() for m in metrics],
    }
    body["analysis_id"] = snapshot_hash(body)
    return body


def snapshot_hash(body: dict[str, Any]) -> str:
    data = {k: v for k, v in body.items() if k not in ("analysis_id", "created_at")}
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
