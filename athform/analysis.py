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
from . import disciplines
from .metrics import (CaptureConditions, MetricResult, apply_discipline_status, cadence, cap_for_preview, contact_times,
                      knee_angles, plausibility_findings, trunk_leans)
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
    preview_reason: str = ""   # 軽量モデルなど、正式な精密指標として出さない理由（ARCH-014）
    strict_corrections: bool = True   # False: 当てられない保存済みの訂正を「未適用」として記録して続ける
    # 地面の線の出どころと問題点。自動推定なら最高でも estimated、問題があれば review_required（レビュー B5）
    ground_quality: dict[str, Any] = field(default_factory=lambda: {"method": "given", "problems": []})


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
                      capture_time_notes=list(index.capture_time_notes), decode_failures=list(index.decode_failures),
                      decode_check=index.decode_check, discarded_packets=index.discarded_packets)


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
        # パケットと突き合わせられないときは原本の総数が分からないので None（推測しない）
        "source_frame_count": (total + len(inp.index.decode_failures)) if inp.index.decode_check == "packets" else None,
        "decoded_frame_count": total,
        "decode_failed_source_frame_ids": list(inp.index.decode_failures),
        "decode_check": inp.index.decode_check,
        "selected_frame_count": len(selected),
        "analyzed_frame_count": len(poses),
        "pose_failed_frame_ids": failed,
        # 推定はしたが、対象の人物を検出できなかった・見失った・取り直したフレーム（レビュー N3）
        "unobservable_frame_ids": [fid for fid, p in sorted(poses.items())
                                   if {"no_person", "tracking_lost", "occluded"} & set(p.flags)],
        "reacquired_frame_ids": [fid for fid, p in sorted(poses.items()) if "reacquired" in p.flags],
        "skipped_by_sampling": total - len(selected),
    }
    return poses, completeness


def analyze(inp: AnalysisInput, corrections: list[Correction] | None = None) -> dict[str, Any]:
    if inp.conditions.discipline not in disciplines.CATALOG:
        raise AnalysisError(f"未登録の種目: {inp.conditions.discipline}")
    poses, completeness = run_pose(inp)
    # 入力の索引は変更しない（同じ入力で何度でも同じ結果: FR-TIME-014）
    work_index = _subindex(inp.index, [f.source_frame_id for f in inp.index.frames])
    # プレビューでも「選んだが推定に失敗したフレーム」は索引に残し、未観測として区間を広げる
    timing_index = (work_index if inp.sampling.mode == ALL_SOURCE_FRAMES
                    else _subindex(work_index, _select_frames(inp)))
    detection = detect_events(timing_index, poses, inp.detector)
    layers = EventLayers(detection.events, list(corrections or []))
    effective = layers.effective(timing_index, detection.breaks, strict=inp.strict_corrections)

    # 動画時間で見た動きの速さが種目として不自然 → スロー書き出しを疑い、実時間指標を出さない（AC-015）
    # 接地時間かピッチのどちらかが出せる素材なら、必ずこのチェックを通る（値が出るのに未確認、はない）
    _, problems = plausibility_findings(effective, detection.breaks, inp.conditions.discipline)
    for problem in problems:
        downgrade_capture_time(timing_index, problem)
        if timing_index is not work_index:
            downgrade_capture_time(work_index, problem)

    time_metrics: list[MetricResult] = contact_times(timing_index, effective, detection.breaks)
    time_metrics.append(cadence(timing_index, effective, detection.breaks))
    metrics = time_metrics
    metrics += knee_angles(effective, poses, inp.conditions, inp.detector.min_confidence)
    metrics += trunk_leans(effective, poses, inp.conditions, inp.detector.min_confidence)
    metrics = apply_discipline_status(metrics, inp.conditions.discipline)
    if inp.sampling.mode != ALL_SOURCE_FRAMES:
        cap_for_preview(metrics)
    if inp.preview_reason:
        cap_for_preview(metrics, inp.preview_reason)
    apply_ground_quality(metrics, inp.ground_quality)
    if inp.index.decode_check != "packets":
        for m in metrics:
            m.limitations.append("パケット情報がなく、復号できなかったフレームの有無を確認できない")
    auto_events = detection.events

    body = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "source_video_hash": inp.source_video_hash,
        "model": inp.pose.model.to_json(),
        "detector": inp.detector.to_json(),
        "capture_conditions": inp.conditions.to_json(),
        "ground_quality": inp.ground_quality,
        "discipline": disciplines.get(inp.conditions.discipline).to_json(),
        "feedback": {"findings": [], "reason": "評価ルールが専門家に承認されていないため、改善提案は出さない（FR-FB-001, §8.3）"},
        "analysis_sampling_policy": inp.sampling.to_json(),
        "preview": inp.sampling.mode != ALL_SOURCE_FRAMES or bool(inp.preview_reason),
        "preview_reason": inp.preview_reason,
        "frame_completeness": completeness,
        "capture_quality": work_index.quality_report(),
        "frames": [f.to_json() for f in work_index.frames],
        "pose_raw": [poses[fid].to_json() for fid in sorted(poses)],
        "temporal_filter": {"method": "none", "note": "生座標をそのまま使用。平滑化は別レイヤー（未実装）"},
        "events": {
            "auto": [e.to_json() for e in auto_events],
            "corrections": [c.to_json() for c in layers.corrections],
            "unapplied_corrections": layers.unapplied,
            "effective": [e.to_json() for e in effective],
            "observation_breaks": [b.to_json() for b in detection.breaks],
        },
        "metrics": [m.to_json() for m in metrics],
    }
    body["analysis_id"] = snapshot_hash(body)
    return body


def apply_ground_quality(metrics: list[MetricResult], gq: dict[str, Any]) -> None:
    """接地の判定は地面の線に依存する。全指標が接地イベントを根拠にするので、地面の質で状態の上限を決める。"""
    from .metrics import ESTIMATED, REVIEW, UNAVAILABLE, VALID, worse  # noqa: PLC0415
    method = gq.get("method")
    problems = list(gq.get("problems") or [])
    for m in metrics:
        if m.status == UNAVAILABLE:
            continue
        if problems:
            m.status = worse(m.status, REVIEW)
            m.reasons += [f"地面の線: {p}" for p in problems]
        elif method == "auto":
            if m.status == VALID:
                m.status = ESTIMATED
            m.limitations.append("地面の線は自動推定（画面で確かめて指定すると、確かな値として扱える）")


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
