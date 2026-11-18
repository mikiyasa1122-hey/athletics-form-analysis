"""元動画の時間軸（FR-TIME-002/003/004, NFR-TIME-001, §11.4）。

- 時刻は整数PTS × 分数time_baseで保持し、浮動小数で累積させない。
- 公称FPSから時間差を計算しない。隣り合う実フレームのPTS差を使う。
- PTSが欠けた入力は、FPSを仮定して成功扱いにしない（TimebaseError）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from fractions import Fraction
from statistics import median
from typing import Any

# 撮影時間軸の確度（§11.4 capture_time_status）
VERIFIED = "verified"            # 撮影メタデータとコンテナ時刻が一致
CONTAINER_ONLY = "container_only"  # 撮影メタデータなし。コンテナPTSを実時間とみなす（仮定）
DECLARED = "declared"            # 利用者申告のみ。精度の証明には使わない
SLOWMO_SUSPECTED = "slowmo_suspected"  # 撮影FPSと出力時間軸が食い違う／不自然に遅い
UNKNOWN = "unknown"

# 実時間指標（秒）を出してよい確度
REAL_TIME_OK = {VERIFIED, CONTAINER_ONLY}

# Android等が書く撮影FPSのメタデータキー
CAPTURE_FPS_TAGS = ("com.android.capture.fps",)

GAP_FACTOR = Fraction(3, 2)      # 中央値間隔の1.5倍を超えたら欠落の疑い
VFR_TOLERANCE = Fraction(2, 100)  # 間隔のばらつきが中央値の2%を超えたらVFR
FPS_MATCH_TOLERANCE = Fraction(5, 100)
CONTAINER_ASSUMPTION_NOTE = "撮影メタデータなし。コンテナPTSを実時間とみなしている（仮定）"


class TimebaseError(ValueError):
    """時刻基準が復元できない入力。誤った実測値を出さないために解析を止める（NFR-TIME-004）。"""


@dataclass(frozen=True)
class SourceFrame:
    source_frame_id: int        # 原本の提示順に振る安定ID（派生動画の連番は使わない）
    presentation_index: int
    pts: int
    time_base: Fraction
    timestamp: Fraction         # 先頭フレーム基準の元動画秒（正確な分数）
    flags: tuple[str, ...] = ()

    @property
    def usable_for_timing(self) -> bool:
        # 原本パケットに裏付けのないフレームの時刻は、計測の根拠に使わない
        return not ({"duplicate_pts", "non_monotonic", "no_matching_packet"} & set(self.flags))

    def to_json(self) -> dict[str, Any]:
        return {
            "source_frame_id": self.source_frame_id,
            "presentation_index": self.presentation_index,
            "pts": self.pts,
            "time_base": f"{self.time_base.numerator}/{self.time_base.denominator}",
            "t": float(self.timestamp),
            "t_exact": f"{self.timestamp.numerator}/{self.timestamp.denominator}",
            "flags": list(self.flags),
        }


@dataclass
class FrameIndex:
    frames: list[SourceFrame]
    time_base: Fraction
    nominal_fps: Fraction | None
    capture_fps: Fraction | None
    capture_time_status: str
    capture_time_notes: list[str] = field(default_factory=list)
    decode_failures: list[int] = field(default_factory=list)  # 復号できなかった原本フレームID（パケット基準）
    decode_check: str = "unavailable"   # packets = パケットと突き合わせ済み / unavailable = 検出できない
    discarded_packets: int = 0          # edit list で再生対象外（flags に D）のパケット。原本フレームに数えない

    def by_id(self, source_frame_id: int) -> SourceFrame:
        frame = self._lookup().get(source_frame_id)
        if frame is None:
            raise KeyError(f"source_frame_id {source_frame_id} は原本フレーム索引にない")
        return frame

    def has(self, source_frame_id: int) -> bool:
        return source_frame_id in self._lookup()

    def _lookup(self) -> dict[int, SourceFrame]:
        cache: dict[int, SourceFrame] | None = getattr(self, "_cache", None)
        if cache is None:
            cache = {f.source_frame_id: f for f in self.frames}
            object.__setattr__(self, "_cache", cache)
        return cache

    def intervals(self, *, exclude_gaps: bool = False) -> list[Fraction]:
        timed = [f for f in self.frames if f.usable_for_timing]
        return [b.timestamp - a.timestamp for a, b in zip(timed, timed[1:])
                if not (exclude_gaps and {"after_gap", "after_decode_failure"} & set(b.flags))]

    @property
    def time_axis_valid(self) -> bool:
        return not any("non_monotonic" in f.flags for f in self.frames)

    def quality_report(self) -> dict[str, Any]:
        ivs = self.intervals()
        med = median(ivs) if ivs else None
        gaps = [f.source_frame_id for f in self.frames if "after_gap" in f.flags]
        decode_gaps = [f.source_frame_id for f in self.frames if "after_decode_failure" in f.flags]
        dups = [f.source_frame_id for f in self.frames if "duplicate_pts" in f.flags]
        return {
            "frame_count": len(self.frames),
            "nominal_fps": _frac_json(self.nominal_fps),
            "capture_fps": _frac_json(self.capture_fps),
            "capture_time_status": self.capture_time_status,
            "capture_time_notes": list(self.capture_time_notes),
            "interval_s": {
                "min": float(min(ivs)) if ivs else None,
                "median": float(med) if med is not None else None,
                "max": float(max(ivs)) if ivs else None,
            },
            "vfr": is_vfr(self.intervals(exclude_gaps=True), self.time_base),
            "gap_frame_ids": gaps,                      # 撮影時の欠落の疑い
            "after_decode_failure_frame_ids": decode_gaps,  # 復号失敗で間隔が空いたフレーム（欠落とは区別）
            "discarded_packet_count": self.discarded_packets,
            "duplicate_pts_frame_ids": dups,
            "decode_failures": list(self.decode_failures),
            "decode_check": self.decode_check,
            "time_axis_valid": self.time_axis_valid,
        }


def _frac_json(v: Fraction | None) -> float | None:
    return None if v is None else round(float(v), 6)


def parse_fraction(text: str | None) -> Fraction | None:
    if not text:
        return None
    try:
        value = Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None
    return value if value > 0 else None


def is_vfr(intervals: list[Fraction], tick: Fraction | None = None) -> bool:
    """欠落を除いたフレーム間隔が、中央値の2%を超えてばらつくか。

    time_base の丸め（±1tick）によるばらつきは無視する。240fps を 1/600 で表すと間隔は 2,3 tick の交互になるが、
    これは固定FPS（レビュー N2）。
    """
    if len(intervals) < 2:
        return False
    med = median(intervals)
    spread = max(intervals) - min(intervals)
    if tick is not None and spread <= tick:
        return False
    return spread > med * VFR_TOLERANCE


def build_frame_index(probe: dict[str, Any], *, declared_capture_fps: str | None = None,
                      declared_slowmo: bool = False) -> FrameIndex:
    """ffprobe の `-show_streams -show_frames -of json` 出力から原本フレーム索引を作る。

    frames は ffprobe が返す出力順（＝提示順）で並んでいる前提。順序が逆転していれば
    non_monotonic として記録し、時間軸を無効にする。
    """
    # カバー画像（disposition.attached_pic）は映像ではないので選ばない（レビュー N7）
    streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"
               and not (s.get("disposition") or {}).get("attached_pic")]
    if not streams:
        raise TimebaseError("映像ストリームがない")
    stream = streams[0]
    time_base = parse_fraction(stream.get("time_base"))
    if time_base is None:
        raise TimebaseError("time_base が取得できない")
    stream_index = stream.get("index", 0)

    raw_frames, raw_packets = _split_entries(probe, stream_index)
    if not raw_frames:
        raise TimebaseError("復号されたフレームがない")

    # パケット（コンテナに記録された原本フレーム）があれば、そのPTS順で原本フレームIDを振る。
    # 復号に失敗したフレームは ffprobe の frames から消えるだけなので、パケットとの差分で検出する。
    # edit list で捨てられるパケット（先頭カットした動画の pre-roll など）は flags に D が付き、
    # 再生されないので原本フレームにも復号失敗にも数えない（実機の ffprobe 9.0 で確認）
    packet_rank: dict[int, int] | None = None
    discarded = [p for p in raw_packets if "D" in str(p.get("flags", ""))]
    kept = [p for p in raw_packets if "D" not in str(p.get("flags", ""))]
    if kept:
        packet_pts = sorted({_require_pts(p, f"パケット {i}") for i, p in enumerate(kept)})
        packet_rank = {pts: rank for rank, pts in enumerate(packet_pts)}

    frames: list[SourceFrame] = []
    seen_pts: set[int] = set()
    first_pts: int | None = min(packet_rank) if packet_rank else None
    last_pts: int | None = None
    for i, rf in enumerate(raw_frames):
        pts = _require_pts(rf, f"提示順 {i} のフレーム")
        if first_pts is None:
            first_pts = pts
        flags: list[str] = []
        if pts in seen_pts:
            flags.append("duplicate_pts")
        elif last_pts is not None and pts < last_pts:
            flags.append("non_monotonic")
        seen_pts.add(pts)
        if "duplicate_pts" not in flags and "non_monotonic" not in flags:
            last_pts = pts
        if packet_rank is not None and pts not in packet_rank:
            flags.append("no_matching_packet")
        source_id = packet_rank[pts] if packet_rank is not None and pts in packet_rank and "duplicate_pts" not in flags else None
        frames.append(SourceFrame(
            source_frame_id=-1 if source_id is None else source_id,
            presentation_index=i,
            pts=pts,
            time_base=time_base,
            timestamp=(pts - first_pts) * time_base,
            flags=tuple(flags),
        ))

    if packet_rank is None:
        frames = [replace(f, source_frame_id=f.presentation_index) for f in frames]
        decode_failures: list[int] = []
        decode_check = "unavailable" if not raw_packets else "all_packets_discarded"
    else:
        # パケットに対応しないフレーム（重複PTS等）には、パケット数の後ろから衝突しないIDを振る
        extra = len(packet_rank)
        fixed = []
        for f in frames:
            if f.source_frame_id < 0:
                f = replace(f, source_frame_id=extra)
                extra += 1
            fixed.append(f)
        frames = fixed
        decoded = {f.pts for f in frames}
        decode_failures = [rank for pts, rank in sorted(packet_rank.items(), key=lambda kv: kv[1]) if pts not in decoded]
        decode_check = "packets"

    failed_pts = {pts for pts, rank in packet_rank.items() if rank in set(decode_failures)} if packet_rank else set()
    frames = _mark_gaps(frames, failed_pts)
    nominal = parse_fraction(stream.get("avg_frame_rate")) or parse_fraction(stream.get("r_frame_rate"))
    index = FrameIndex(frames=frames, time_base=time_base, nominal_fps=nominal, capture_fps=None,
                       capture_time_status=UNKNOWN, decode_failures=decode_failures, decode_check=decode_check,
                       discarded_packets=len(discarded))
    _resolve_capture_time(index, probe, stream, declared_capture_fps, declared_slowmo)
    return index


def _split_entries(probe: dict[str, Any], stream_index: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """ffprobe の frames / packets（または -show_packets -show_frames 併用時の packets_and_frames）を分ける。"""
    frames = list(probe.get("frames", []))
    packets = list(probe.get("packets", []))
    for entry in probe.get("packets_and_frames", []):
        (packets if entry.get("type") == "packet" else frames).append(entry)

    def mine(e: dict[str, Any]) -> bool:
        return e.get("media_type", "video") == "video" and e.get("stream_index", stream_index) == stream_index

    return [f for f in frames if mine(f)], [p for p in packets if mine(p) or ("codec_type" in p and p["codec_type"] == "video")]


def _require_pts(entry: dict[str, Any], label: str) -> int:
    # best_effort_timestamp は ffprobe が仮定FPSなどから推測した値になりうるので使わない（NFR-TIME-004）
    pts_val = entry.get("pts")
    if pts_val is None or pts_val == "N/A":
        raise TimebaseError(f"{label}にPTSがない。推測した時刻や固定FPSを仮定して続行しない")
    try:
        return int(pts_val)
    except (TypeError, ValueError) as exc:
        raise TimebaseError(f"{label}のPTSが整数でない: {pts_val!r}") from exc


GAP_WINDOW = 4   # 欠落の判定で比べる前後の間隔の数


RATE_RUN_MIN = 3   # ほぼ同じ長さの間隔がこの数以上続けば「その間隔で撮影している区間」とみなす


def _same_spacing(a: Fraction, b: Fraction, tick: Fraction) -> bool:
    return abs(a - b) <= max(2 * tick, b / 10)


def _rate_support(ivs: list[Fraction], i: int, tick: Fraction) -> int:
    """i 番目の間隔と同じ長さの間隔が、前後にいくつ続くか。

    より長い間隔（欠落）は飛び越えて数え、より短い間隔に当たったらそこで止める。
    - 「30fps に切り替わった直後に欠落」でも、欠落の向こうの 30fps 区間まで数えられる。
    - 「1枚おきに欠落」（64,128,64,128…）では、128 の両隣がすぐ短い間隔なので 1 しか数えない。
    """
    iv = ivs[i]
    count = 1
    for step in (-1, 1):
        j = i + step
        while 0 <= j < len(ivs) and count < RATE_RUN_MIN:
            if ivs[j] < iv and not _same_spacing(ivs[j], iv, tick):
                break   # より短い間隔 → ここで撮影間隔が変わっている
            if _same_spacing(ivs[j], iv, tick):
                count += 1
            j += step
    return count


def _mark_gaps(frames: list[SourceFrame], decode_failed_pts: set[int] | None = None) -> list[SourceFrame]:
    """欠落の疑いを付ける。

    1. ほぼ同じ長さ（±2tick または ±10%）の間隔が、より短い間隔を挟まずに RATE_RUN_MIN 個以上続く区間は、
       その間隔で撮影している区間（可変FPSの一部）とみなし、欠落にしない（_rate_support）。
       FPS が上がる切り替えでも下がる切り替えでも同じに扱う。
    2. それ以外の間隔は、前後 GAP_WINDOW 個の最短間隔の1.5倍を超えれば欠落とする。

    これまでの方式とその失敗（F-0013）:
    - 全体の中央値: 可変FPSの遅い区間がすべて欠落になる。
    - 局所中央値: 近い間隔の複数欠落で中央値が引き上げられ、見逃す。
    - 最短間隔＋「後ろが同じ長さで続くか」: FPS が上がる切り替えの手前を欠落と誤判定する。
    限界: 同じ長さの欠落が RATE_RUN_MIN 回以上続くと、FPS の切り替えと区別できない（欠落と表示しない。時刻は実PTSのまま）。
    原本パケットはあるのに復号できなかったフレームが原因なら、撮影時の欠落とは区別して after_decode_failure とする。
    """
    timed = [f for f in frames if f.usable_for_timing]
    ivs = [b.timestamp - a.timestamp for a, b in zip(timed, timed[1:])]
    if not ivs:
        return frames
    tick = timed[0].time_base
    failed = decode_failed_pts or set()
    marks: dict[int, str] = {}
    for i, iv in enumerate(ivs):
        if _rate_support(ivs, i, tick) >= RATE_RUN_MIN:
            continue   # その間隔で撮影している区間（可変FPSの一部）
        neighbors = ivs[max(0, i - GAP_WINDOW):i] + ivs[i + 1:i + 1 + GAP_WINDOW]
        if not neighbors or iv <= min(neighbors) * GAP_FACTOR:
            continue
        a, b = timed[i], timed[i + 1]
        explained = any(a.pts < p < b.pts for p in failed)
        marks[b.source_frame_id] = "after_decode_failure" if explained else "after_gap"
    out = []
    for f in frames:
        if f.source_frame_id in marks:
            f = replace(f, flags=f.flags + (marks[f.source_frame_id],))
        out.append(f)
    return out


def _regular_intervals(index: "FrameIndex") -> list[Fraction]:
    timed = [f for f in index.frames if f.usable_for_timing]
    return [b.timestamp - a.timestamp for a, b in zip(timed, timed[1:])
            if not ({"after_gap", "after_decode_failure"} & set(b.flags))]


def _measured_fps(index: FrameIndex) -> Fraction | None:
    """欠落を除いた間隔の平均から求める。time_base で割り切れない間隔（例: 240fps を 1/600 で表すと 2,3 tick の交互）
    でも、中央値でなく平均を使えば正しい値になる（レビュー N2）。"""
    ivs = _regular_intervals(index)
    if not ivs:
        return None
    mean = sum(ivs, Fraction(0)) / len(ivs)
    return 1 / mean if mean > 0 else None


def _close(a: Fraction, b: Fraction) -> bool:
    return abs(a - b) <= b * FPS_MATCH_TOLERANCE


def _resolve_capture_time(index: FrameIndex, probe: dict[str, Any], stream: dict[str, Any],
                          declared_capture_fps: str | None, declared_slowmo: bool) -> None:
    tags: dict[str, Any] = {}
    tags.update(probe.get("format", {}).get("tags", {}) or {})
    tags.update(stream.get("tags", {}) or {})
    measured = _measured_fps(index)
    notes = index.capture_time_notes

    meta_fps = None
    for key in CAPTURE_FPS_TAGS:
        meta_fps = parse_fraction(str(tags[key])) if key in tags else None
        if meta_fps:
            notes.append(f"撮影FPSメタデータ {key}={tags[key]}")
            break
    declared = parse_fraction(declared_capture_fps)

    if not index.time_axis_valid:
        index.capture_time_status = UNKNOWN
        notes.append("PTSの順序が逆転しているため時間軸を確定できない")
        return

    if meta_fps is not None:
        index.capture_fps = meta_fps
        if measured is not None and _close(measured, meta_fps):
            index.capture_time_status = VERIFIED
        else:
            index.capture_time_status = SLOWMO_SUSPECTED
            notes.append(f"撮影FPS {float(meta_fps):g} とフレーム間隔から求めたFPS "
                         f"{float(measured or 0):g} が一致しない（スロー書き出しの疑い）")
        return

    if declared_slowmo:
        index.capture_fps = declared
        index.capture_time_status = SLOWMO_SUSPECTED
        notes.append("利用者がスロー書き出し動画と申告。撮影時の実時間を確認できない")
        return

    if declared is not None:
        index.capture_fps = declared
        if measured is not None and _close(measured, declared):
            index.capture_time_status = CONTAINER_ONLY
            notes.append("申告FPSとフレーム間隔は一致。ただし申告は精度の証明に使わない")
        else:
            index.capture_time_status = DECLARED
            notes.append(f"申告された撮影FPS {float(declared):g} とフレーム間隔が一致しない。実時間を確定できない")
        return

    index.capture_time_status = CONTAINER_ONLY
    notes.append(CONTAINER_ASSUMPTION_NOTE)


def downgrade_capture_time(index: FrameIndex, reason: str) -> None:
    """解析中に見つかった矛盾（例: 種目として不自然に遅い動作）で確度を下げる。"""
    if index.capture_time_status in (CONTAINER_ONLY, DECLARED):
        index.capture_time_status = SLOWMO_SUSPECTED
        # 「PTSを実時間とみなす」仮定は撤回されたので、その注記を残さない
        index.capture_time_notes[:] = [n for n in index.capture_time_notes if n != CONTAINER_ASSUMPTION_NOTE]
        index.capture_time_notes.append(reason)
