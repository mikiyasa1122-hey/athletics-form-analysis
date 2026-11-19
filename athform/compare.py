"""動画内・動画間の比較（FR-INTRA-001〜007, FR-INTER-001〜005, §7.2, §7.3, §10.2, AC-005）。

- 比べる前に互換性を確かめる。重大な不一致（種目・撮影角度・時間軸の確度・モデル）は数値の差を出さない。
- 比べるのは同じ定義の指標だけ（metric_id と metric_version）。valid / estimated の値だけを使う。
- 差は「量」と「不確実性」で示し、良し悪し（改善・悪化）は判定しない（FR-INTRA-007, §10.2）。
"""

from __future__ import annotations

from statistics import median
from typing import Any

USABLE = ("valid", "estimated")
REAL_TIME_OK = ("verified", "container_only")


def _select(snap: dict[str, Any], frame_range: tuple[int, int] | None) -> list[dict[str, Any]]:
    out = []
    for m in snap["metrics"]:
        ids = m["evidence_refs"]["frame_ids"]
        if frame_range is not None and (not ids or not all(frame_range[0] <= i <= frame_range[1] for i in ids)):
            continue
        out.append(m)
    return out


def compatibility(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    blocking: list[str] = []
    warnings: list[str] = []
    da, db = a["capture_conditions"], b["capture_conditions"]
    if da["discipline"] != db["discipline"]:
        blocking.append(f"種目が違う（{da['discipline']} / {db['discipline']}）")
    if da.get("camera_view") != db.get("camera_view"):
        blocking.append(f"撮影角度が違う（{da.get('camera_view')} / {db.get('camera_view')}）。角度の差は比べられない")
    if a["model"] != b["model"]:
        blocking.append("姿勢推定のモデル・重み・設定が違う。同じモデルで再解析してから比べる")
    if a["algorithm_version"] != b["algorithm_version"]:
        blocking.append(f"算出ロジックの版が違う（{a['algorithm_version']} / {b['algorithm_version']}）")
    qa, qb = a["capture_quality"], b["capture_quality"]
    time_ok = qa["capture_time_status"] in REAL_TIME_OK and qb["capture_time_status"] in REAL_TIME_OK
    if not time_ok:
        warnings.append("どちらかの撮影時間軸を確認できないため、時間の指標（接地時間・ピッチ）は比べない")
    ia, ib = qa["interval_s"]["median"], qb["interval_s"]["median"]
    if ia and ib and max(ia, ib) / min(ia, ib) > 1.5:
        warnings.append(f"フレーム間隔が大きく違う（{ia * 1000:.1f} ms / {ib * 1000:.1f} ms）。時間の指標の精度が揃わない")
    if a.get("preview") or b.get("preview"):
        warnings.append("どちらかがプレビュー（軽量モデル・間引き）の解析")
    if da.get("direction") != db.get("direction"):
        warnings.append("進行方向が違う。体幹前傾は進行方向基準なので比べられるが、映り方は違う")
    return {"comparable": not blocking, "blocking": blocking, "warnings": warnings, "time_metrics_ok": time_ok}


def _summary(ms: list[dict[str, Any]]) -> dict[str, Any] | None:
    vals = [m for m in ms if m["status"] in USABLE and m["value"] is not None]
    if not vals:
        return None
    halves = [(m["interval"]["upper"] - m["interval"]["lower"]) / 2 for m in vals if m.get("interval")]
    return {"n": len(vals), "excluded": len(ms) - len(vals), "median": median(m["value"] for m in vals),
            "half_width": median(halves) if halves else None, "unit": vals[0]["unit"]}


def compare(a: dict[str, Any], b: dict[str, Any], range_a: tuple[int, int] | None = None,
            range_b: tuple[int, int] | None = None) -> dict[str, Any]:
    compat = compatibility(a, b) if a is not b else {"comparable": True, "blocking": [], "warnings": [],
                                                     "time_metrics_ok": a["capture_quality"]["capture_time_status"]
                                                     in REAL_TIME_OK}
    rows = []
    ma, mb = _select(a, range_a), _select(b, range_b)
    ids = sorted({(m["metric_id"], m["metric_version"]) for m in ma} | {(m["metric_id"], m["metric_version"]) for m in mb})
    for mid, ver in ids:
        sa = _summary([m for m in ma if m["metric_id"] == mid and m["metric_version"] == ver])
        sb = _summary([m for m in mb if m["metric_id"] == mid and m["metric_version"] == ver])
        row: dict[str, Any] = {"metric_id": mid, "metric_version": ver, "a": sa, "b": sb, "diff": None, "note": ""}
        is_time = mid in ("contact_time", "cadence")
        if not compat["comparable"]:
            row["note"] = "互換性がないため比べない"
        elif is_time and not compat["time_metrics_ok"]:
            row["note"] = "撮影時間軸を確認できないため比べない"
        elif sa is None or sb is None:
            row["note"] = "どちらかに使える値がない（判定不可・要確認を除外）"
        else:
            diff = sb["median"] - sa["median"]
            row["diff"] = diff
            if sa["half_width"] is not None and sb["half_width"] is not None:
                tol = sa["half_width"] + sb["half_width"]
                row["note"] = ("差はフレーム間隔による不確実性の範囲内（変化があるとは言えない）"
                               if abs(diff) <= tol else "差は不確実性の範囲を超える（原因や良し悪しは判定しない）")
            else:
                row["note"] = "画像平面の2D角度。測定誤差は未検証なので、差の意味は判断しない"
        rows.append(row)
    return {"compatibility": compat, "rows": rows,
            "a": {"analysis_id": a["analysis_id"], "range": range_a}, "b": {"analysis_id": b["analysis_id"], "range": range_b},
            "policy": "差の量と不確実性だけを示す。改善・悪化の判定や原因の推定はしない"}
