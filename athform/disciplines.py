"""種目カタログと、種目×指標の公開状態（REQ-ATH-001 §5, §9.4, R-009）。

全種目を登録するが、精度を保証できない指標は数値を出さないか、状態を下げて出す。
  supported         … 品質ゲートを通れば valid まで出す
  experimental      … 値は出すが最高でも estimated（「試験的」と明記）
  manual_review     … 値は出すが最高でも review_required（手動確認が前提）
  requires_calibration … 距離校正が必要。校正機能が未実装なので数値は出さない
  unsupported       … この版では算出しない（理由を表示）
閾値（妥当範囲）は暫定値。DEC-016 の実測ベンチマークで決め直す。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SUPPORTED = "supported"
EXPERIMENTAL = "experimental"
MANUAL_REVIEW = "manual_review"
REQUIRES_CALIBRATION = "requires_calibration"
UNSUPPORTED = "unsupported"

CORE_METRICS = ("contact_time", "cadence", "knee_angle_at_contact", "trunk_lean_at_contact")


@dataclass(frozen=True)
class Discipline:
    discipline_id: str
    name: str
    category: str
    phases: tuple[str, ...]
    metric_status: dict[str, str]
    plausibility: dict[str, float] | None      # 動画時間で見た動きの速さの妥当範囲（スロー書き出しの検出）
    unsupported_metrics: dict[str, str] = field(default_factory=dict)   # 未実装の種目固有指標と理由
    notes: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "discipline_id": self.discipline_id, "name": self.name, "category": self.category,
            "phases": list(self.phases), "metric_status": dict(self.metric_status),
            "plausibility": self.plausibility, "unsupported_metrics": dict(self.unsupported_metrics),
            "notes": list(self.notes),
        }


SPRINT_PLAUSIBILITY = {"min_cadence": 3.0, "max_contact_time": 0.25}
DISTANCE_PLAUSIBILITY = {"min_cadence": 2.2, "max_contact_time": 0.40}
WALK_PLAUSIBILITY = {"min_cadence": 2.0, "max_contact_time": 0.60}

RUN_SUPPORTED = {m: SUPPORTED for m in CORE_METRICS}
RUN_EXPERIMENTAL = {m: EXPERIMENTAL for m in CORE_METRICS}
ANGLES_ONLY_REVIEW = {"contact_time": UNSUPPORTED, "cadence": UNSUPPORTED,
                      "knee_angle_at_contact": MANUAL_REVIEW, "trunk_lean_at_contact": MANUAL_REVIEW}

_NEEDS_OBJECT = "用具・障害物の検出が未実装"
_NEEDS_CALIB = "距離校正が未実装"
_NEEDS_3D = "単眼2Dでは算出を保証できない"


def _d(did, name, cat, phases, metrics, plaus, unsupported=None, notes=()):
    return Discipline(did, name, cat, tuple(phases), dict(metrics), plaus, dict(unsupported or {}), tuple(notes))


CATALOG: dict[str, Discipline] = {d.discipline_id: d for d in [
    # 短距離（F3 の正式対象）
    _d("sprint_100m", "100m", "sprint", ["スタート", "加速", "最高速度", "維持"], RUN_SUPPORTED, SPRINT_PLAUSIBILITY,
       {"stride_length": _NEEDS_CALIB, "running_speed": _NEEDS_CALIB}),
    _d("sprint_200m", "200m", "sprint", ["スタート", "カーブ", "直線"], RUN_SUPPORTED, SPRINT_PLAUSIBILITY,
       {"stride_length": _NEEDS_CALIB, "running_speed": _NEEDS_CALIB}, ["カーブ区間は投影の影響を受ける"]),
    _d("sprint_400m", "400m", "sprint", ["スタート", "カーブ", "直線", "終盤"], RUN_SUPPORTED, SPRINT_PLAUSIBILITY,
       {"stride_length": _NEEDS_CALIB, "running_speed": _NEEDS_CALIB}, ["カーブ区間は投影の影響を受ける"]),
    # 中長距離（走動作は共通。閾値の検証が短距離より少ないので試験的）
    _d("middle_800m", "800m", "middle_distance", ["巡航", "ペース変化", "スパート"], RUN_EXPERIMENTAL,
       DISTANCE_PLAUSIBILITY, {"pace_segment": "ラップ・GPS 連携が未実装（ペースは推測しない）"}),
    _d("middle_1500m", "1500m", "middle_distance", ["巡航", "ペース変化", "スパート"], RUN_EXPERIMENTAL,
       DISTANCE_PLAUSIBILITY, {"pace_segment": "ラップ・GPS 連携が未実装（ペースは推測しない）"}),
    _d("long_5000m", "3000m / 5000m / 10000m", "long_distance", ["巡航", "変化点", "終盤"], RUN_EXPERIMENTAL,
       DISTANCE_PLAUSIBILITY, {"pace_segment": "ラップ・GPS 連携が未実装（ペースは推測しない）"}),
    _d("road_marathon", "ロード・マラソン", "long_distance", ["巡航", "終盤"], RUN_EXPERIMENTAL, DISTANCE_PLAUSIBILITY,
       {"pace_segment": "ラップ・GPS 連携が未実装（ペースは推測しない）"}),
    # ハードル・障害
    _d("hurdles_short", "100mH / 110mH", "hurdles", ["アプローチ", "踏切", "越え", "着地", "再加速"], RUN_EXPERIMENTAL,
       SPRINT_PLAUSIBILITY, {"hurdle_clearance": _NEEDS_OBJECT, "lead_trail_leg": _NEEDS_OBJECT},
       ["ハードル越えの局面は接地イベントとして扱わない（越えをまたぐ接地時間は要確認）"]),
    _d("hurdles_400m", "400mH", "hurdles", ["アプローチ", "踏切", "越え", "着地"], RUN_EXPERIMENTAL, SPRINT_PLAUSIBILITY,
       {"hurdle_clearance": _NEEDS_OBJECT, "lead_trail_leg": _NEEDS_OBJECT}),
    _d("steeplechase", "3000m障害", "steeplechase", ["接近", "越え", "水濠", "着地"], RUN_EXPERIMENTAL,
       DISTANCE_PLAUSIBILITY, {"barrier_clearance": _NEEDS_OBJECT, "water_jump": _NEEDS_OBJECT}),
    # 跳躍（助走の走動作だけ試験的に。踏切・空中の指標は未実装）
    _d("long_jump", "走幅跳", "horizontal_jump", ["助走", "踏切準備", "踏切", "空中", "着地"], RUN_EXPERIMENTAL,
       SPRINT_PLAUSIBILITY, {"takeoff_angle": f"{_NEEDS_CALIB}・身体重心の推定が未実装（体幹角度で代用しない）",
                             "takeoff_board": _NEEDS_OBJECT},
       ["助走の接地・ピッチのみ。踏切の最後の接地は接地時間として出すが、踏切時間としての検証はしていない"]),
    _d("triple_jump", "三段跳", "horizontal_jump", ["助走", "ホップ", "ステップ", "ジャンプ"], RUN_EXPERIMENTAL,
       SPRINT_PLAUSIBILITY, {"phase_split": "hop/step/jump の自動分割が未実装", "takeoff_angle": _NEEDS_CALIB}),
    _d("high_jump", "走高跳", "vertical_jump", ["助走", "踏切", "クリアランス"], RUN_EXPERIMENTAL, SPRINT_PLAUSIBILITY,
       {"bar_clearance": _NEEDS_OBJECT, "approach_curve": _NEEDS_3D}, ["曲線助走は単眼2Dでは投影の影響が大きい"]),
    _d("pole_vault", "棒高跳", "vertical_jump", ["助走", "踏切", "空中"], RUN_EXPERIMENTAL, SPRINT_PLAUSIBILITY,
       {"pole_tracking": _NEEDS_OBJECT, "bar_clearance": _NEEDS_OBJECT}),
    # 投擲（走動作の指標は当てはまらない。接地時の角度だけ手動確認前提）
    _d("javelin", "やり投", "throw", ["助走", "クロスステップ", "ブロック", "リリース"], ANGLES_ONLY_REVIEW, None,
       {"release_angle": _NEEDS_OBJECT, "release_speed": f"{_NEEDS_OBJECT}・{_NEEDS_CALIB}"}),
    _d("shot_put", "砲丸投", "throw", ["準備", "グライド/回転", "支持", "リリース"], ANGLES_ONLY_REVIEW, None,
       {"release_angle": _NEEDS_OBJECT, "technique_type": "グライド/回転の自動分類が未実装"}),
    _d("discus", "円盤投", "throw", ["準備", "ターン", "支持", "リリース"], ANGLES_ONLY_REVIEW, None,
       {"release_angle": _NEEDS_OBJECT, "rotation": _NEEDS_3D}),
    _d("hammer", "ハンマー投", "throw", ["準備", "回転", "支持", "リリース"], ANGLES_ONLY_REVIEW, None,
       {"implement_path": _NEEDS_OBJECT, "rotation": _NEEDS_3D}),
    # 競歩・リレー・混成
    _d("race_walk", "競歩", "race_walk", ["接地", "支持", "離地"], RUN_EXPERIMENTAL, WALK_PLAUSIBILITY,
       {"rule_compliance": "競技規則の適法・違反は判定しない"},
       ["両足支持期があるため、接地時間は走動作と意味が違う"]),
    _d("relay_4x100", "4×100mリレー", "relay", ["助走", "受け渡し", "加速"], RUN_EXPERIMENTAL, SPRINT_PLAUSIBILITY,
       {"baton_exchange": f"{_NEEDS_OBJECT}・複数選手の追跡が未実装"}, ["解析対象は1人（最初に大きく映った人）"]),
    _d("relay_4x400", "4×400mリレー", "relay", ["助走", "受け渡し"], RUN_EXPERIMENTAL, SPRINT_PLAUSIBILITY,
       {"baton_exchange": f"{_NEEDS_OBJECT}・複数選手の追跡が未実装"}),
]}

COMBINED_EVENTS = {
    "decathlon": ("十種競技", ["sprint_100m", "long_jump", "shot_put", "high_jump", "sprint_400m", "hurdles_short",
                              "discus", "pole_vault", "javelin", "middle_1500m"]),
    "heptathlon": ("七種競技", ["hurdles_short", "high_jump", "shot_put", "sprint_200m", "long_jump", "javelin",
                               "middle_800m"]),
}


def get(discipline_id: str) -> Discipline:
    if discipline_id not in CATALOG:
        raise KeyError(f"未登録の種目: {discipline_id}")
    return CATALOG[discipline_id]


def catalog_json() -> dict[str, Any]:
    return {
        "disciplines": [d.to_json() for d in CATALOG.values()],
        "combined_events": {k: {"name": n, "components": c} for k, (n, c) in COMBINED_EVENTS.items()},
        "status_labels": {SUPPORTED: "対応", EXPERIMENTAL: "試験的", MANUAL_REVIEW: "手動確認が前提",
                          REQUIRES_CALIBRATION: "要校正", UNSUPPORTED: "未対応"},
    }
