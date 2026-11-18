"""表示用派生動画（プロキシ）と原本フレームの対応（FR-TIME-011, FR-PLAY-007, AC-016, AC-019）。

プロキシのフレーム番号を原本フレームIDとして扱わない。元動画時刻で照合し、
対応の取れない（生成・補間された）フレームや重複フレームを明示する。
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from .timebase import FrameIndex


def map_proxy_to_source(source: FrameIndex, proxy: FrameIndex, *, offset: Fraction = Fraction(0)) -> dict[str, Any]:
    """proxy の各フレームを、元動画時刻が半フレーム間隔以内で一致する原本フレームに対応づける。"""
    src = [f for f in source.frames if f.usable_for_timing]
    if not src:
        raise ValueError("原本に時刻の確定したフレームがない")
    ivs = source.intervals()
    tol = (min(ivs) / 2) if ivs else Fraction(1, 1000)
    mapping = []
    used: dict[int, int] = {}
    j = 0
    for p in proxy.frames:
        t = p.timestamp + offset
        while j + 1 < len(src) and abs(src[j + 1].timestamp - t) <= abs(src[j].timestamp - t):
            j += 1
        cand = src[j]
        entry: dict[str, Any] = {"proxy_index": p.presentation_index, "proxy_t": float(p.timestamp)}
        if abs(cand.timestamp - t) < tol:
            entry["source_frame_id"] = cand.source_frame_id
            if cand.source_frame_id in used:
                entry["kind"] = "duplicate"
            else:
                entry["kind"] = "source"
                used[cand.source_frame_id] = p.presentation_index
        else:
            entry["source_frame_id"] = None
            entry["kind"] = "generated"   # 原本に存在しない時刻 = 補間・合成フレーム
        mapping.append(entry)
    dropped = [f.source_frame_id for f in src if f.source_frame_id not in used]
    return {
        "frames": mapping,
        "dropped_source_frame_ids": dropped,
        "frame_accurate": all(m["kind"] == "source" for m in mapping) and not dropped,
    }


def evidence_frame_for_proxy(mapping: dict[str, Any], proxy_index: int) -> int | None:
    """プロキシの1フレームを計測根拠として使えるときだけ原本フレームIDを返す。生成フレームは None。"""
    for m in mapping["frames"]:
        if m["proxy_index"] == proxy_index:
            return m["source_frame_id"] if m["kind"] in ("source", "duplicate") else None
    raise KeyError(proxy_index)
