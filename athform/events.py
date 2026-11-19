"""接地・離地イベントと時刻区間（FR-TIME-008, FR-SINGLE-002, §6.2, §7.4）。

イベントは「直前に旧状態が観測された原本フレーム」と「新状態が初めて観測された原本フレーム」で挟み、
その2つの元動画時刻を区間の下限・上限とする。区間より細かい精度を装わない。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import Any

from .pose import UNOBSERVABLE_POSE_FLAGS, PoseFrame
from .timebase import FrameIndex

FOOT_CONTACT = "foot_contact"
TOE_OFF = "toe_off"
SIDES = ("left", "right")
UNOBSERVABLE_FLAGS = UNOBSERVABLE_POSE_FLAGS
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
    ground_y: float              # 画像上の地面の高さ（px、下向き正）。x = ground_x0 での値
    contact_band_px: float = 6.0
    ground_slope: float = 0.0    # 地面の傾き（dy/dx）。カメラが傾いている・奥行き方向に走る場合
    ground_x0: float = 0.0
    min_confidence: float = 0.5
    # 観測の途切れがこれ以上続くと、接地または滞空が丸ごと隠れうる（短距離の最短局面 ≒ 0.08s より短く取る）。
    # 暫定値。DEC-016 で種目ごとに決め直す。
    max_unobserved_s: float = 0.06

    def to_json(self) -> dict[str, Any]:
        return {"ground_y": self.ground_y, "contact_band_px": self.contact_band_px,
                "ground_slope": self.ground_slope, "ground_x0": self.ground_x0,
                "min_confidence": self.min_confidence, "max_unobserved_s": self.max_unobserved_s}


@dataclass(frozen=True)
class ObservationBreak:
    """足の状態を連続して追えない区間。この区間をまたいでイベントを数えない・指標を組まない。"""
    side: str
    before_frame_id: int
    after_frame_id: int
    lower: Fraction
    upper: Fraction
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {"side": self.side, "source_frame_ids": [self.before_frame_id, self.after_frame_id],
                "time_interval_sec": {"lower": float(self.lower), "upper": float(self.upper)},
                "reason": self.reason}


def foot_state(pose: PoseFrame | None, side: str, cfg: DetectorConfig) -> tuple[bool | None, float | None]:
    """(接地しているか, 使った点の最小信頼度)。観測できなければ (None, None)。"""
    if pose is None or UNOBSERVABLE_FLAGS & set(pose.flags):
        return None, None
    pts = [pose.get(f"{side}_{p}", cfg.min_confidence) for p in ("heel", "toe", "ankle")]
    visible = [p for p in pts if p is not None]
    if len(visible) < 2:   # 足先・踵のどちらも見えないなら判定しない
        return None, None
    low = max(visible, key=lambda p: p.y)
    ground = cfg.ground_y + cfg.ground_slope * (low.x - cfg.ground_x0)
    return low.y >= ground - cfg.contact_band_px, min(p.confidence for p in visible)


@dataclass
class Detection:
    events: list[MotionEvent]
    breaks: list[ObservationBreak]


def detect_events(index: FrameIndex, poses: dict[int, PoseFrame], cfg: DetectorConfig) -> Detection:
    events: list[MotionEvent] = []
    breaks: list[ObservationBreak] = []
    limit = Fraction(cfg.max_unobserved_s)
    for side in SIDES:
        last_state: bool | None = None
        last_frame = None
        unobserved_between = False
        reacquire_pending = False   # 取り直しのフレームで足が見えなくても、次に見えたところで区切る（再々レビュー N1）
        for frame in index.frames:
            if not frame.usable_for_timing:
                unobserved_between = True
                continue
            pose = poses.get(frame.source_frame_id)
            if pose is not None and "reacquired" in pose.flags:
                reacquire_pending = True
            state, score = foot_state(pose, side, cfg)
            if state is None:
                if last_state is not None:
                    unobserved_between = True
                continue
            if last_state is not None and last_frame is not None and reacquire_pending:
                # 対象の人物を見失って取り直した。別人の可能性があるので、つながず区切る（レビュー B4）
                breaks.append(ObservationBreak(side, last_frame.source_frame_id, frame.source_frame_id,
                                               last_frame.timestamp, frame.timestamp,
                                               "対象の人物を見失い、取り直した（別人の可能性）"))
            elif last_state is not None and last_frame is not None and frame.timestamp - last_frame.timestamp >= limit:
                # 途切れが長いと、間で何回遷移したか分からない。イベントを作らず区切りとして記録する
                if "after_decode_failure" in frame.flags and not unobserved_between:
                    why = "復号できなかったフレーム"
                elif "after_gap" in frame.flags and not unobserved_between:
                    why = "フレーム欠落"
                else:
                    why = "観測できないフレームが続く"
                breaks.append(ObservationBreak(side, last_frame.source_frame_id, frame.source_frame_id,
                                               last_frame.timestamp, frame.timestamp,
                                               f"{why}（{float(frame.timestamp - last_frame.timestamp) * 1000:.0f} ms）"))
            elif last_state is not None and last_frame is not None and state != last_state:
                reasons: list[str] = []
                if unobserved_between:
                    reasons.append("遷移区間に観測できないフレームがある")
                if "after_gap" in frame.flags:
                    reasons.append("フレーム欠落を挟む")
                if "after_decode_failure" in frame.flags:
                    reasons.append("復号できなかったフレームを挟む")
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
            reacquire_pending = False
    return Detection(sort_events(events), sorted(breaks, key=lambda b: (b.lower, b.side)))


def break_between(breaks: list[ObservationBreak], side: str | None, start: Fraction, end: Fraction) -> ObservationBreak | None:
    """start〜end の間に（side が None なら左右どちらかの）区切りがあれば返す。"""
    for b in breaks:
        if (side is None or b.side == side) and b.lower < end and b.upper > start:
            return b
    return None


def validate_sequence(events: list[MotionEvent], breaks: list[ObservationBreak]) -> list[MotionEvent]:
    """同じ足で接地と離地が交互になっていないイベントを要確認にする（見落とし・重複・誤訂正の検出）。"""
    flagged: set[str] = set()
    for side in SIDES:
        seq = [e for e in events if e.side == side]
        for a, b in zip(seq, seq[1:]):
            if a.event_type == b.event_type and break_between(breaks, side, a.upper, b.lower) is None:
                flagged.update((a.event_id, b.event_id))
    return [with_status(e, "review_required", "同じ足で接地と離地が交互になっていない") if e.event_id in flagged else e
            for e in events]


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
    revoked_at: str | None = None       # 取り消した日時（記録は消さずに残す）
    revoked_reason: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}

    @staticmethod
    def from_json(d: dict[str, Any]) -> "Correction":
        allowed = {"action", "corrected_by", "corrected_at", "reason", "event_id", "event_type", "side", "after_frame_id",
                   "revoked_at", "revoked_reason"}
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
    unapplied: list[dict[str, Any]] = field(default_factory=list)

    def effective(self, index: FrameIndex, breaks: list[ObservationBreak] | None = None,
                  strict: bool = True) -> list[MotionEvent]:
        """訂正を順に当てる。strict=False（保存済みの訂正での再解析）では、当てられない訂正で失敗せず記録する。

        地面の線や推論のやり直しで自動イベントが変わると、訂正が参照していたイベントがなくなることがある。
        その訂正は「適用できなかった訂正」として版に残し、解析自体は続ける（訂正は取り消しも作り直しもできる）。
        """
        current = {e.event_id: e for e in self.auto}
        self.unapplied = []
        for i, c in enumerate(self.corrections):
            if c.revoked_at:
                continue
            try:
                current = _apply(current, c, index)
            except CorrectionError as exc:
                if strict:
                    raise
                self.unapplied.append({"index": i, "correction": c.to_json(), "reason": str(exc)})
        return validate_sequence(sort_events(list(current.values())), breaks or [])


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
