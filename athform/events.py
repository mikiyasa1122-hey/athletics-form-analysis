"""接地・離地イベントと時刻区間（FR-TIME-008, FR-SINGLE-002, §6.2, §7.4）。

イベントは「直前に旧状態が観測された原本フレーム」と「新状態が初めて観測された原本フレーム」で挟み、
その2つの元動画時刻を区間の下限・上限とする。区間より細かい精度を装わない。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import Any

from .pose import PoseFrame
from .timebase import FrameIndex

FOOT_CONTACT = "foot_contact"
TOE_OFF = "toe_off"
SIDES = ("left", "right")
UNOBSERVABLE_FLAGS = {"occluded", "tracking_lost"}
DEGRADING_FLAGS = {"motion_blur"}


class CorrectionError(ValueError):
    pass


@dataclass(frozen=True)
class MotionEvent:
    event_id: str
    event_type: str
    side: str
    before_frame_id: int
    after_frame_id: int
    lower: Fraction
    upper: Fraction
    source: str = "auto"                 # auto / manual
    quality_status: str = "valid"        # valid / review_required
    quality_reasons: tuple[str, ...] = ()
    model_score: float | None = None     # 検出時の点の信頼度。時刻誤差ではない

    @property
    def time(self) -> Fraction:
        return (self.lower + self.upper) / 2

    def to_json(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "side": self.side,
            "source_frame_ids": [self.before_frame_id, self.after_frame_id],
            "evidence_frame_id": self.after_frame_id,
            "event_time_sec": float(self.time),
            "time_interval_sec": {"lower": float(self.lower), "upper": float(self.upper)},
            "time_basis": "source_pts",
            "estimation_method": "frame_bracket" if self.source == "auto" else "manual_frame",
            "source": self.source,
            "quality_status": self.quality_status,
            "quality_reasons": list(self.quality_reasons),
            "model_score": self.model_score,
        }


@dataclass(frozen=True)
class DetectorConfig:
    ground_y: float              # 画像上の地面の高さ（px、下向き正）。撮影条件として与える
    contact_band_px: float = 6.0
    min_confidence: float = 0.5

    def to_json(self) -> dict[str, Any]:
        return {"ground_y": self.ground_y, "contact_band_px": self.contact_band_px,
                "min_confidence": self.min_confidence}


def foot_state(pose: PoseFrame | None, side: str, cfg: DetectorConfig) -> tuple[bool | None, float | None]:
    """(接地しているか, 使った点の最小信頼度)。観測できなければ (None, None)。"""
    if pose is None or UNOBSERVABLE_FLAGS & set(pose.flags):
        return None, None
    pts = [pose.get(f"{side}_{p}", cfg.min_confidence) for p in ("heel", "toe", "ankle")]
    visible = [p for p in pts if p is not None]
    if len(visible) < 2:   # 足先・踵のどちらも見えないなら判定しない
        return None, None
    lowest = max(p.y for p in visible)
    return lowest >= cfg.ground_y - cfg.contact_band_px, min(p.confidence for p in visible)


def detect_events(index: FrameIndex, poses: dict[int, PoseFrame], cfg: DetectorConfig) -> list[MotionEvent]:
    events: list[MotionEvent] = []
    for side in SIDES:
        last_state: bool | None = None
        last_frame = None
        unobserved_between = False
        for frame in index.frames:
            if not frame.usable_for_timing:
                unobserved_between = True
                continue
            pose = poses.get(frame.source_frame_id)
            state, score = foot_state(pose, side, cfg)
            if state is None:
                if last_state is not None:
                    unobserved_between = True
                continue
            if last_state is not None and last_frame is not None and state != last_state:
                reasons: list[str] = []
                if unobserved_between:
                    reasons.append("遷移区間に観測できないフレームがある")
                if "after_gap" in frame.flags:
                    reasons.append("フレーム欠落を挟む")
                prev_pose = poses.get(last_frame.source_frame_id)
                if (pose and DEGRADING_FLAGS & set(pose.flags)) or (prev_pose and DEGRADING_FLAGS & set(prev_pose.flags)):
                    reasons.append("イベント前後のフレームにモーションブラー")
                etype = FOOT_CONTACT if state else TOE_OFF
                events.append(MotionEvent(
                    event_id=f"{side}-{etype}-{frame.source_frame_id}",
                    event_type=etype, side=side,
                    before_frame_id=last_frame.source_frame_id, after_frame_id=frame.source_frame_id,
                    lower=last_frame.timestamp, upper=frame.timestamp,
                    quality_status="review_required" if reasons else "valid",
                    quality_reasons=tuple(reasons), model_score=score,
                ))
            last_state, last_frame, unobserved_between = state, frame, False
    return sort_events(events)


def sort_events(events: list[MotionEvent]) -> list[MotionEvent]:
    return sorted(events, key=lambda e: (e.upper, e.side, e.event_type))


@dataclass(frozen=True)
class Correction:
    """手動訂正。自動検出結果は上書きせず、別レイヤーとして保存する（§7.4）。"""
    action: str                 # move / add / delete
    corrected_by: str
    corrected_at: str
    reason: str
    event_id: str | None = None
    event_type: str | None = None
    side: str | None = None
    after_frame_id: int | None = None   # 新状態が初めて見える原本フレーム

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}

    @staticmethod
    def from_json(d: dict[str, Any]) -> "Correction":
        allowed = {"action", "corrected_by", "corrected_at", "reason", "event_id", "event_type", "side", "after_frame_id"}
        extra = set(d) - allowed
        if extra:
            raise CorrectionError(f"不明な項目: {sorted(extra)}")
        try:
            return Correction(**d)
        except TypeError as exc:
            raise CorrectionError(str(exc)) from exc


@dataclass
class EventLayers:
    auto: list[MotionEvent]
    corrections: list[Correction] = field(default_factory=list)

    def effective(self, index: FrameIndex) -> list[MotionEvent]:
        current = {e.event_id: e for e in self.auto}
        for c in self.corrections:
            current = _apply(current, c, index)
        return sort_events(list(current.values()))


def _bracket(index: FrameIndex, after_frame_id: int) -> tuple[int, Fraction, Fraction]:
    if not isinstance(after_frame_id, int) or isinstance(after_frame_id, bool) or not index.has(after_frame_id):
        raise CorrectionError(f"原本フレーム {after_frame_id!r} は存在しない")
    timed = [f for f in index.frames if f.usable_for_timing]
    pos = next((i for i, f in enumerate(timed) if f.source_frame_id == after_frame_id), None)
    if pos is None:
        raise CorrectionError(f"原本フレーム {after_frame_id} は時刻が確定できない（重複PTS等）")
    if pos == 0:
        raise CorrectionError("先頭フレームの前は観測がないため、イベント区間を作れない")
    before = timed[pos - 1]
    return before.source_frame_id, before.timestamp, timed[pos].timestamp


def _apply(current: dict[str, MotionEvent], c: Correction, index: FrameIndex) -> dict[str, MotionEvent]:
    for field_name in ("corrected_by", "corrected_at", "reason"):
        if not str(getattr(c, field_name) or "").strip():
            raise CorrectionError(f"訂正には {field_name} が必要")
    out = dict(current)
    if c.action == "delete":
        if c.event_id not in out:
            raise CorrectionError(f"イベント {c.event_id} がない")
        del out[c.event_id]
        return out
    if c.action not in ("move", "add"):
        raise CorrectionError(f"不明な操作: {c.action}")
    if c.after_frame_id is None:
        raise CorrectionError("after_frame_id が必要")
    before_id, lower, upper = _bracket(index, c.after_frame_id)
    if c.action == "move":
        if c.event_id not in out:
            raise CorrectionError(f"イベント {c.event_id} がない")
        base = out.pop(c.event_id)
        etype, side = base.event_type, base.side
    else:
        if c.event_type not in (FOOT_CONTACT, TOE_OFF) or c.side not in SIDES:
            raise CorrectionError("追加には event_type と side が必要")
        etype, side = c.event_type, c.side
    new_id = f"{side}-{etype}-{c.after_frame_id}-manual"
    out[new_id] = MotionEvent(new_id, etype, side, before_id, c.after_frame_id, lower, upper,
                              source="manual", quality_status="valid", quality_reasons=(), model_score=None)
    return out


def with_status(event: MotionEvent, status: str, reason: str) -> MotionEvent:
    return replace(event, quality_status=status, quality_reasons=event.quality_reasons + (reason,))
