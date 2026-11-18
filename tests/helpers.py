from __future__ import annotations

from typing import Any

from athform.analysis import AnalysisInput, SamplingPolicy, analyze
from athform.events import Correction, DetectorConfig
from athform.metrics import CaptureConditions
from athform.pose import FixturePoseAdapter
from athform.synth import GROUND_Y, CaptureSpec, SprintModel, make_dataset
from athform.timebase import build_frame_index


def make_input(spec: CaptureSpec, *, view: str | None = "side", direction: str | None = "left_to_right",
               declared_fps: str | None = None, declared_slowmo: bool = False,
               sampling: SamplingPolicy | None = None, pose_edit=None, probe_edit=None) -> AnalysisInput:
    probe, pose = make_dataset(spec)
    if probe_edit:
        probe_edit(probe)
    if pose_edit:
        pose_edit(pose)
    return AnalysisInput(
        source_video_hash="test-hash",
        index=build_frame_index(probe, declared_capture_fps=declared_fps, declared_slowmo=declared_slowmo),
        pose=FixturePoseAdapter(pose),
        detector=DetectorConfig(ground_y=GROUND_Y),
        conditions=CaptureConditions(camera_view=view, direction=direction),
        sampling=sampling or SamplingPolicy(),
    )


def run(spec: CaptureSpec, corrections: list[Correction] | None = None, **kw: Any) -> dict[str, Any]:
    return analyze(make_input(spec, **kw), corrections)


def metrics(snap: dict[str, Any], metric_id: str) -> list[dict[str, Any]]:
    return [m for m in snap["metrics"] if m["metric_id"] == metric_id]


MODEL = SprintModel()
