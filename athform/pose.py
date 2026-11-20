"""姿勢推定アダプタと共通キーポイントスキーマ（FR-POSE-001/003/004/005/006, ARCH-003）。

実モデル（MMPose 等）はライセンス確認（NFR-SEC-010, DEC-002）が済むまで同梱しない。
ここでは共通スキーマと、推定結果JSONを読む FixturePoseAdapter を提供する。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Protocol

KEYPOINTS = (
    "nose",
    "left_shoulder", "right_shoulder",
    "left_hip", "right_hip",
    "left_knee", "right_knee",
    "left_ankle", "right_ankle",
    "left_heel", "right_heel",
    "left_toe", "right_toe",
    "left_elbow", "right_elbow",
    "left_wrist", "right_wrist",
)


# 点が出ていても、このフラグのフレームは観測として扱わない
UNOBSERVABLE_POSE_FLAGS = frozenset({"occluded", "tracking_lost", "no_person", "ambiguous"})


class PoseInputError(ValueError):
    pass


@dataclass(frozen=True)
class Keypoint:
    x: float
    y: float
    confidence: float  # モデル由来の確からしさ。計測誤差ではない（FR-TIME-010）


@dataclass
class PoseFrame:
    source_frame_id: int
    keypoints: dict[str, Keypoint | None]   # None = 欠損・遮蔽（補完しない: FR-POSE-004）
    flags: list[str] = field(default_factory=list)  # 例: motion_blur, occluded, tracking_lost

    def get(self, name: str, min_conf: float) -> Keypoint | None:
        kp = self.keypoints.get(name)
        if kp is None or kp.confidence < min_conf:
            return None
        return kp

    def to_json(self) -> dict[str, Any]:
        return {
            "source_frame_id": self.source_frame_id,
            "keypoints": {k: (None if v is None else [v.x, v.y, v.confidence]) for k, v in self.keypoints.items()},
            "flags": list(self.flags),
        }


@dataclass(frozen=True)
class ModelInfo:
    model_id: str
    weights_version: str
    config: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {"model_id": self.model_id, "weights_version": self.weights_version, "config": self.config}


class PoseAdapter(Protocol):
    @property
    def model(self) -> ModelInfo: ...

    def infer(self, source_frame_id: int) -> PoseFrame | None:
        """1つの原本フレームを推定する。失敗時は None（失敗として記録される）。"""
        ...


class FixturePoseAdapter:
    """推定済みJSONを読むアダプタ。テスト・デモ・外部ツールの結果取り込みに使う。

    形式: {"model": {...}, "keypoint_map": {モデル点名: 共通点名}?, "frames": [{"source_frame_id", "keypoints", "flags"}]}
    """

    def __init__(self, data: dict[str, Any]):
        m = data.get("model") or {}
        if not m.get("model_id") or not m.get("weights_version"):
            raise PoseInputError("model.model_id と model.weights_version が必要（FR-POSE-006）")
        self._model = ModelInfo(str(m["model_id"]), str(m["weights_version"]), dict(m.get("config") or {}))
        mapping: dict[str, str] = dict(data.get("keypoint_map") or {k: k for k in KEYPOINTS})
        unknown = set(mapping.values()) - set(KEYPOINTS)
        if unknown:
            raise PoseInputError(f"共通スキーマにない点: {sorted(unknown)}")
        self._frames: dict[int, PoseFrame] = {}
        for raw in data.get("frames", []):
            fid = raw.get("source_frame_id")
            if not isinstance(fid, int) or isinstance(fid, bool):
                raise PoseInputError(f"source_frame_id が整数でない: {fid!r}")
            if raw.get("generated"):
                raise PoseInputError(f"frame {fid} は生成フレーム。計測の根拠にしない（FR-TIME-007）")
            if fid in self._frames:
                raise PoseInputError(f"frame {fid} が重複している")
            kps: dict[str, Keypoint | None] = {name: None for name in KEYPOINTS}
            for model_name, value in (raw.get("keypoints") or {}).items():
                common = mapping.get(model_name)
                if common is None:
                    continue
                kps[common] = _parse_kp(fid, model_name, value)
            self._frames[fid] = PoseFrame(fid, kps, [str(f) for f in raw.get("flags") or []])

    @property
    def model(self) -> ModelInfo:
        return self._model

    def frame_ids(self) -> set[int]:
        return set(self._frames)

    def infer(self, source_frame_id: int) -> PoseFrame | None:
        return self._frames.get(source_frame_id)


def _parse_kp(fid: int, name: str, value: Any) -> Keypoint | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise PoseInputError(f"frame {fid} {name}: [x, y, confidence] の形式でない")
    try:
        x, y, c = (float(v) for v in value)
    except (TypeError, ValueError) as exc:
        raise PoseInputError(f"frame {fid} {name}: 数値でない") from exc
    if not all(math.isfinite(v) for v in (x, y, c)):
        return None  # NaN/inf は欠損として扱う（値を作らない）
    if not 0.0 <= c <= 1.0:
        raise PoseInputError(f"frame {fid} {name}: confidence が0〜1の範囲外")
    return Keypoint(x, y, c)
