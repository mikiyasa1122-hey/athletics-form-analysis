"""指標と指標別品質ゲート（§6.1, §6.3, §9.2, §9.4, FR-TIME-009/010, NFR-TIME-003）。

可否はFPS一律の閾値で決めない。時間指標は「イベント時刻区間から導いた不確実性」と
「撮影時間軸の確度」で、角度指標は「点の可視性」と「撮影視点」で判定する。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from . import ALGORITHM_VERSION
from .events import FOOT_CONTACT, TOE_OFF, MotionEvent
from .pose import PoseFrame
from .timebase import CONTAINER_ONLY, REAL_TIME_OK, FrameIndex

VALID = "valid"
ESTIMATED = "estimated"
REVIEW = "review_required"
UNAVAILABLE = "unavailable"
_RANK = {VALID: 0, ESTIMATED: 1, REVIEW: 2, UNAVAILABLE: 3}


@dataclass(frozen=True)
class MetricDefinition:
    metric_id: str
    metric_version: str
    unit: str
    definition: str
    coordinate_system: str
    data_sources: tuple[str, ...]
    required_capture_conditions: tuple[str, ...]
    max_relative_uncertainty: float | None = None  # 時間指標: 区間幅/値 の上限（暫定・未検証）

    def to_json(self) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id, "metric_version": self.metric_version, "unit": self.unit,
            "definition": self.definition, "coordinate_system": self.coordinate_system,
            "data_sources": list(self.data_sources),
            "required_capture_conditions": list(self.required_capture_conditions),
            "max_relative_uncertainty": self.max_relative_uncertainty,
        }


CONTACT_TIME = MetricDefinition(
    "contact_time", "1", "s",
    "同一足の接地イベントから次の離地イベントまでの元動画時間。両イベントの時刻区間から可能範囲を求める",
    "source_video_time", ("foot_contact", "toe_off"),
    ("撮影時間軸が実時間として確認できる", "接地・離地を挟むフレームが観測できる"),
    max_relative_uncertainty=0.25,
)
CADENCE = MetricDefinition(
    "cadence", "1", "steps/s",
    "連続する接地イベント（左右）の歩数を、最初と最後の接地の時間差で割った値",
    "source_video_time", ("foot_contact",),
    ("撮影時間軸が実時間として確認できる", "3回以上の連続した接地"),
    max_relative_uncertainty=0.10,
)
KNEE_ANGLE = MetricDefinition(
    "knee_angle_at_contact", "1", "degree",
    "接地が初めて観測された原本フレームでの股関節-膝-足首の画像平面内の角度（180°=伸展）。3D関節角度ではない",
    "image_plane", ("hip", "knee", "ankle", "foot_contact"),
    ("側面からの撮影", "股関節・膝・足首が見える"),
)
TRUNK_LEAN = MetricDefinition(
    "trunk_lean_at_contact", "1", "degree",
    "接地が初めて観測された原本フレームでの、両股関節中点→両肩中点の線と画像の鉛直のなす角（進行方向へ前傾が正）",
    "image_plane", ("shoulders", "hips", "foot_contact"),
    ("側面からの撮影", "進行方向の指定", "肩・股関節が見える"),
)
DEFINITIONS = (CONTACT_TIME, CADENCE, KNEE_ANGLE, TRUNK_LEAN)

# 種目として不自然に遅い動き（スロー書き出しの疑い）の下限。暫定値（DEC-016 で検証予定）
PLAUSIBLE_MIN_CADENCE = {"sprint_100m": 2.0}


@dataclass
class CaptureConditions:
    camera_view: str | None = None          # side / front / rear / oblique
    direction: str | None = None            # left_to_right / right_to_left
    discipline: str = "sprint_100m"

    def to_json(self) -> dict[str, Any]:
        return {"camera_view": self.camera_view, "direction": self.direction, "discipline": self.discipline}


@dataclass
class MetricResult:
    definition: MetricDefinition
    status: str
    value: float | None = None
    interval: tuple[float, float] | None = None
    display: str = ""
    reasons: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    side: str | None = None
    evidence_frame_ids: list[int] = field(default_factory=list)
    evidence_event_ids: list[str] = field(default_factory=list)
    quality: dict[str, Any] = field(default_factory=dict)
    apparent_video_value: float | None = None   # 撮影実時間でない「動画上の見かけの値」（実測と混同しない）

    def to_json(self) -> dict[str, Any]:
        d = self.definition.to_json()
        d.update({
            "status": self.status,
            "value": self.value,
            "interval": None if self.interval is None else {"lower": self.interval[0], "upper": self.interval[1]},
            "display": self.display,
            "reasons": list(self.reasons),
            "limitations": list(self.limitations),
            "side": self.side,
            "evidence_refs": {"frame_ids": list(self.evidence_frame_ids), "event_ids": list(self.evidence_event_ids)},
            "quality": dict(self.quality),
            "algorithm_version": ALGORITHM_VERSION,
            "apparent_video_value": self.apparent_video_value,
        })
        return d


def worse(a: str, b: str) -> str:
    return a if _RANK[a] >= _RANK[b] else b


def format_with_resolution(value: float, lower: float, upper: float, unit: str) -> str:
    """区間幅に見合う桁数だけ表示する（NFR-TIME-003）。"""
    half = (upper - lower) / 2
    if half <= 0:
        digits = 3
    else:
        digits = max(0, min(3, -math.floor(math.log10(half))))
    fmt = f"{{:.{digits}f}}"
    return f"{fmt.format(value)} {unit}（範囲 {fmt.format(lower)}〜{fmt.format(upper)}）"


def _time_gate(index: FrameIndex, result: MetricResult) -> None:
    status = index.capture_time_status
    if not index.time_axis_valid:
        result.status = UNAVAILABLE
        result.reasons.append("元動画の時間軸が不正（PTS逆転）")
    elif status not in REAL_TIME_OK:
        result.status = UNAVAILABLE
        result.reasons.append(f"撮影時の実時間を確認できない（capture_time_status={status}）。秒単位の計測は出さない")
    elif status == CONTAINER_ONLY:
        result.status = worse(result.status, ESTIMATED)
        result.limitations.append("撮影メタデータ未確認。コンテナPTSを実時間とみなした値")


def _uncertainty_gate(result: MetricResult, value: float, lower: float, upper: float) -> None:
    limit = result.definition.max_relative_uncertainty
    rel = (upper - lower) / value if value > 0 else math.inf
    result.quality["relative_uncertainty"] = round(rel, 4)
    result.quality["max_relative_uncertainty"] = limit
    if limit is not None and rel > limit:
        result.status = worse(result.status, REVIEW)
        result.reasons.append(
            f"フレーム間隔による不確実性（範囲幅/値={rel:.0%}）が許容値 {limit:.0%} を超える。精密値として扱わない")


def _finish_time_metric(index: FrameIndex, result: MetricResult, value: Fraction, lower: Fraction, upper: Fraction) -> None:
    v, lo, hi = float(value), float(lower), float(upper)
    _time_gate(index, result)
    if result.status == UNAVAILABLE:
        result.apparent_video_value = round(v, 6) if index.time_axis_valid else None
        result.display = "判定不可"
        return
    _uncertainty_gate(result, v, lo, hi)
    result.value, result.interval = v, (lo, hi)
    result.display = format_with_resolution(v, lo, hi, result.definition.unit)


def contact_times(index: FrameIndex, events: list[MotionEvent]) -> list[MetricResult]:
    results = []
    for side in ("left", "right"):
        seq = [e for e in events if e.side == side]
        for i, ev in enumerate(seq):
            if ev.event_type != FOOT_CONTACT:
                continue
            nxt = seq[i + 1] if i + 1 < len(seq) else None
            r = MetricResult(CONTACT_TIME, VALID, side=side, evidence_event_ids=[ev.event_id],
                             evidence_frame_ids=[ev.before_frame_id, ev.after_frame_id])
            if nxt is None or nxt.event_type != TOE_OFF:
                r.status, r.display = UNAVAILABLE, "判定不可"
                r.reasons.append("対応する離地イベントが観測されていない")
                results.append(r)
                continue
            r.evidence_event_ids.append(nxt.event_id)
            r.evidence_frame_ids += [nxt.before_frame_id, nxt.after_frame_id]
            for e in (ev, nxt):
                if e.quality_status != "valid":
                    r.status = worse(r.status, REVIEW)
                    r.reasons += [f"{e.event_id}: {why}" for why in e.quality_reasons] or [f"{e.event_id} は要確認"]
            lower = max(Fraction(0), nxt.lower - ev.upper)  # 接地時間は負にならない
            _finish_time_metric(index, r, nxt.time - ev.time, lower, nxt.upper - ev.lower)
            results.append(r)
    return results


def cadence(index: FrameIndex, events: list[MotionEvent]) -> MetricResult:
    contacts = [e for e in events if e.event_type == FOOT_CONTACT]
    r = MetricResult(CADENCE, VALID, evidence_event_ids=[e.event_id for e in contacts],
                     evidence_frame_ids=[e.after_frame_id for e in contacts])
    r.quality["contact_count"] = len(contacts)
    if len(contacts) < 3:
        r.status, r.display = UNAVAILABLE, "判定不可"
        r.reasons.append(f"連続した接地が3回未満（{len(contacts)}回）")
        return r
    steps = len(contacts) - 1
    first, last = contacts[0], contacts[-1]
    for e in contacts:
        if e.quality_status != "valid":
            r.status = worse(r.status, REVIEW)
            r.reasons.append(f"{e.event_id} は要確認")
    dur, dur_lo, dur_hi = last.time - first.time, last.lower - first.upper, last.upper - first.lower
    if dur_lo <= 0:
        r.status, r.display = UNAVAILABLE, "判定不可"
        r.reasons.append("接地の時刻区間が重なり、時間差を確定できない")
        return r
    _finish_time_metric(index, r, steps / dur, steps / dur_hi, steps / dur_lo)
    return r


def apparent_cadence(events: list[MotionEvent]) -> float | None:
    contacts = [e for e in events if e.event_type == FOOT_CONTACT]
    if len(contacts) < 3:
        return None
    dur = contacts[-1].time - contacts[0].time
    return float((len(contacts) - 1) / dur) if dur > 0 else None


def _angle_deg(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float | None:
    v1 = (a[0] - b[0], a[1] - b[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    n1, n2 = math.hypot(*v1), math.hypot(*v2)
    if n1 == 0 or n2 == 0:
        return None
    cos = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
    return math.degrees(math.acos(cos))


def _view_gate(cond: CaptureConditions, r: MetricResult) -> bool:
    if cond.camera_view != "side":
        r.status, r.display = UNAVAILABLE, "判定不可"
        r.reasons.append(f"撮影角度が側面でない（camera_view={cond.camera_view}）。画像平面の角度は比較できない")
        return False
    return True


def _frame_flags_gate(pose: PoseFrame, r: MetricResult) -> None:
    if "motion_blur" in pose.flags:
        r.status = worse(r.status, REVIEW)
        r.reasons.append("該当フレームにモーションブラー")


def knee_angles(events: list[MotionEvent], poses: dict[int, PoseFrame], cond: CaptureConditions,
                min_conf: float) -> list[MetricResult]:
    out = []
    for ev in events:
        if ev.event_type != FOOT_CONTACT:
            continue
        r = MetricResult(KNEE_ANGLE, VALID, side=ev.side, evidence_frame_ids=[ev.after_frame_id],
                         evidence_event_ids=[ev.event_id])
        r.limitations.append("画像平面内の2D角度")
        out.append(r)
        if not _view_gate(cond, r):
            continue
        pose = poses.get(ev.after_frame_id)
        pts = [pose.get(f"{ev.side}_{p}", min_conf) if pose else None for p in ("hip", "knee", "ankle")]
        if pose is None or any(p is None for p in pts):
            r.status, r.display = UNAVAILABLE, "判定不可"
            r.reasons.append("股関節・膝・足首のいずれかが見えない、または信頼度不足")
            continue
        hip, knee, ankle = pts  # type: ignore[misc]
        ang = _angle_deg((hip.x, hip.y), (knee.x, knee.y), (ankle.x, ankle.y))
        if ang is None:
            r.status, r.display = UNAVAILABLE, "判定不可"
            r.reasons.append("点が重なり角度を定義できない")
            continue
        r.quality["min_keypoint_confidence"] = min(p.confidence for p in (hip, knee, ankle))
        _frame_flags_gate(pose, r)
        r.value = round(ang, 1)
        r.display = f"{ang:.0f}°"
    return out


def trunk_leans(events: list[MotionEvent], poses: dict[int, PoseFrame], cond: CaptureConditions,
                min_conf: float) -> list[MetricResult]:
    out = []
    for ev in events:
        if ev.event_type != FOOT_CONTACT:
            continue
        r = MetricResult(TRUNK_LEAN, VALID, side=ev.side, evidence_frame_ids=[ev.after_frame_id],
                         evidence_event_ids=[ev.event_id])
        r.limitations.append("画像平面内の2D角度")
        out.append(r)
        if not _view_gate(cond, r):
            continue
        if cond.direction not in ("left_to_right", "right_to_left"):
            r.status, r.display = UNAVAILABLE, "判定不可"
            r.reasons.append("進行方向が指定されていない")
            continue
        pose = poses.get(ev.after_frame_id)
        names = ("left_shoulder", "right_shoulder", "left_hip", "right_hip")
        pts = [pose.get(n, min_conf) if pose else None for n in names]
        if pose is None or any(p is None for p in pts):
            r.status, r.display = UNAVAILABLE, "判定不可"
            r.reasons.append("肩・股関節のいずれかが見えない、または信頼度不足")
            continue
        ls, rs, lh, rh = pts  # type: ignore[misc]
        sx, sy = (ls.x + rs.x) / 2, (ls.y + rs.y) / 2
        hx, hy = (lh.x + rh.x) / 2, (lh.y + rh.y) / 2
        dx, up = sx - hx, hy - sy   # 画像のyは下向き
        if up <= 0:
            r.status, r.display = UNAVAILABLE, "判定不可"
            r.reasons.append("肩が股関節より上にない（姿勢推定の誤りの可能性）")
            continue
        sign = 1 if cond.direction == "left_to_right" else -1
        lean = math.degrees(math.atan2(sign * dx, up))
        r.quality["min_keypoint_confidence"] = min(p.confidence for p in pts if p)
        _frame_flags_gate(pose, r)
        r.value = round(lean, 1)
        r.display = f"{lean:.0f}°"
    return out


def cap_for_preview(results: list[MetricResult]) -> None:
    """間引きプレビューの結果は正式な精密指標として出さない（ARCH-014）。"""
    for r in results:
        if r.status == VALID:
            r.status = ESTIMATED
        r.limitations.append("プレビュー解析（フレーム間引きあり）。正式な計測ではない")
