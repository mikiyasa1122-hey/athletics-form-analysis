"""非同期の解析ジョブ（FR-TIME-015, FR-VID-006, ARCH-004/005, §12.2）。

状態: queued → probing → inferring → analyzing → completed / failed / cancelled（取消は cancel_requested を経由）
- 解析は1本ずつ、バックグラウンドのスレッドで実行する。実時間の期限は設けない（精度優先のオフライン解析）。
- reanalyze は保存済みの姿勢推定を使い、推論をやり直さない（訂正・撮影条件の変更の反映）。
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import traceback
from typing import Any, Callable

from . import disciplines
from .analysis import AnalysisError, AnalysisInput, analyze
from .cli import probe_video
from .events import Correction, CorrectionError, DetectorConfig
from .metrics import CaptureConditions
from .pose import Keypoint, ModelInfo, PoseFrame
from .realpose import Cancelled, PoseRunError, StoredPoseAdapter, estimate_ground
from .store import NotFound, Store
from .timebase import TimebaseError, build_frame_index

PoseFn = Callable[..., Any]
MODES = ("performance", "balanced")


def _default_pose_fn(ffmpeg: str, video: str, index, out_dir: str, mode: str, progress, cancelled):
    from .realpose import run_pose  # noqa: PLC0415  （numpy/opencv/rtmlib は実行時にだけ読む）
    return run_pose(ffmpeg, video, index, out_dir, mode=mode, progress=progress, cancelled=cancelled)


def pose_to_json(model: ModelInfo, frames: dict[int, PoseFrame], image_size, view_scale: float,
                 failed: list[int]) -> dict[str, Any]:
    return {"model": model.to_json(), "image_size": list(image_size), "view_scale": view_scale,
            "failed": failed, "frames": [frames[k].to_json() for k in sorted(frames)]}


def pose_from_json(d: dict[str, Any]) -> tuple[ModelInfo, dict[int, PoseFrame]]:
    m = d["model"]
    model = ModelInfo(m["model_id"], m["weights_version"], m.get("config") or {})
    frames = {}
    for f in d["frames"]:
        kps = {k: (None if v is None else Keypoint(*v)) for k, v in f["keypoints"].items()}
        frames[f["source_frame_id"]] = PoseFrame(f["source_frame_id"], kps, list(f.get("flags") or []))
    return model, frames


class JobRunner:
    def __init__(self, store: Store, ffmpeg: str | None, ffprobe_runner: Callable[[list[str]], str] | None = None,
                 pose_fn: PoseFn | None = None):
        self.store = store
        self.ffmpeg = ffmpeg
        self.ffprobe_runner = ffprobe_runner
        self.pose_fn = pose_fn or _default_pose_fn
        self._q: queue.Queue[str | None] = queue.Queue()
        self._cancel: set[str] = set()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, name="athform-jobs", daemon=True)
        self._thread.start()

    # ---------- 外から呼ぶ ----------
    def submit(self, video_id: str, kind: str = "full", mode: str = "performance") -> dict[str, Any]:
        if kind not in ("full", "reanalyze"):
            raise ValueError(f"不明な種類: {kind}")
        if mode not in MODES:
            raise ValueError(f"不明なモード: {mode}")
        if kind == "reanalyze" and self.store.read_json(video_id, "pose.json") is None:
            raise ValueError("姿勢推定の結果がまだない。先に解析を実行する")
        job = self.store.add_job(video_id, kind, mode)
        self._q.put(job["id"])
        return job

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if job["state"] in ("completed", "failed", "cancelled"):
            return job
        with self._lock:
            self._cancel.add(job_id)
        # 読んだ後にジョブが終わっていても上書きしないよう、状態を条件に変える（レビュー N1）
        if not self.store.transition_job(job_id, "cancelled", ("queued",), message="取り消した"):
            self.store.transition_job(job_id, "cancel_requested", ("probing", "inferring", "analyzing"),
                                      message="取り消し中")
        return self.store.get_job(job_id)

    def wait_idle(self, timeout: float = 30) -> None:
        """テスト用: キューが空になるまで待つ。"""
        done = threading.Event()
        self._q.put(None)
        self._idle = done
        done.wait(timeout)

    # ---------- 実行 ----------
    def _loop(self) -> None:
        while True:
            jid = self._q.get()
            if jid is None:
                ev = getattr(self, "_idle", None)
                if ev:
                    ev.set()
                continue
            try:
                self._run(jid)
            except Exception:   # noqa: BLE001  （想定外の例外もジョブの失敗として記録し、ワーカーは止めない）
                # 詳細（ローカルのパスを含む）は画面に出さず、サーバーのコンソールにだけ出す（レビュー N8）
                sys.stderr.write(traceback.format_exc())
                try:
                    self.store.update_job(jid, state="failed", error="想定外のエラー（詳細はサーバーのコンソール）",
                                          message="解析できなかった")
                except Exception:   # noqa: BLE001
                    pass
            finally:
                with self._lock:
                    self._cancel.discard(jid)

    def _cancelled(self, jid: str) -> bool:
        with self._lock:
            return jid in self._cancel

    def _set(self, jid: str, state: str, progress: float, message: str) -> None:
        if self._cancelled(jid):
            raise Cancelled()
        self.store.update_job(jid, state=state, progress=progress, message=message)

    def _run(self, jid: str) -> None:
        job = self.store.get_job(jid)
        if job["state"] in ("cancelled", "cancel_requested"):
            self.store.transition_job(jid, "cancelled", ("cancel_requested",), message="取り消した")
            return
        try:
            video = self.store.get_video(job["video_id"])
        except NotFound:
            self.store.update_job(jid, state="failed", error="動画が削除された")
            return
        vid = video["id"]
        try:
            self._set(jid, "probing", 0.02, "元動画の時刻を読み取り中")
            probe = self.store.read_json(vid, "probe.json")
            if probe is None:
                kwargs = {"runner": self.ffprobe_runner} if self.ffprobe_runner else {}
                probe = probe_video(self.store.original_path(video), **kwargs)
                self.store.write_json(vid, "probe.json", probe)
            index = build_frame_index(probe, declared_capture_fps=video["declared_capture_fps"] or None,
                                      declared_slowmo=bool(video["declared_slowmo"]))
            if job["kind"] == "full":
                if not self.ffmpeg:
                    raise PoseRunError("ffmpeg が見つからない")
                self._set(jid, "inferring", 0.05, f"姿勢推定 0 / {len(index.frames)}")

                def progress(done: int, total: int) -> None:
                    if done % 5 == 0 or done == total:
                        self._set(jid, "inferring", 0.05 + 0.9 * done / total, f"姿勢推定 {done} / {total}")

                res = self.pose_fn(self.ffmpeg, self.store.original_path(video), index, self.store.frames_dir(vid),
                                   job["mode"], progress, lambda: self._cancelled(jid))
                self.store.write_json(vid, "pose.json", pose_to_json(res.model, res.frames, res.image_size,
                                                                      res.view_scale, res.failed))
            self._set(jid, "analyzing", 0.96, "指標を計算中")
            self.analyze_video(vid, mode=job["mode"])
            self.store.update_job(jid, state="completed", progress=1.0, message="完了", error=None)
        except Cancelled:
            self.store.update_job(jid, state="cancelled", message="取り消した")
        except NotFound:
            self.store.update_job(jid, state="failed", error="動画が削除された", message="解析できなかった")
        except (TimebaseError, PoseRunError, AnalysisError, CorrectionError, ValueError) as exc:
            self.store.update_job(jid, state="failed", error=str(exc), message="解析できなかった")
        finally:
            with self._lock:
                self._cancel.discard(jid)

    def analyze_video(self, vid: str, mode: str = "performance", require_last_correction: bool = False) -> dict[str, Any]:
        """保存済みの probe と姿勢推定から解析し、新しい版として保存する。

        保存済みの訂正のうち、自動イベントが変わって当てられないものは失敗にせず「未適用」として版に残す（レビュー B1）。
        require_last_correction=True（訂正を追加した直後）では、追加した訂正が当てられなければエラーにする。
        """
        video = self.store.get_video(vid)
        probe = self.store.read_json(vid, "probe.json")
        pose = self.store.read_json(vid, "pose.json")
        if probe is None or pose is None:
            raise AnalysisError("probe または姿勢推定の結果がない")
        index = build_frame_index(probe, declared_capture_fps=video["declared_capture_fps"] or None,
                                  declared_slowmo=bool(video["declared_slowmo"]))
        model, frames = pose_from_json(pose)
        min_conf = float(model.config.get("min_confidence", 0.3))
        if video["ground_override"]:
            g = json.loads(video["ground_override"])
            detector = DetectorConfig(ground_y=float(g["ground_y"]), contact_band_px=float(g.get("contact_band_px", 6)),
                                      min_confidence=min_conf, ground_slope=float(g.get("ground_slope", 0)),
                                      ground_x0=float(g.get("ground_x0", 0)))
            ground_diag: dict[str, Any] = {"method": "user_override"}
            ground_quality = {"method": "user_override", "problems": []}
        else:
            try:
                detector, ground_diag = estimate_ground(frames, min_conf)
                ground_quality = {"method": "auto", "problems": list(ground_diag.get("problems", []))}
            except PoseRunError as exc:
                # 人物（足）が十分に見えない。接地は判定できないので、地面を画面の外に置いてイベントを作らない。
                # 解析自体は保存し、フレームと「なぜ出せないか」を画面で確かめられるようにする
                height = float((pose.get("image_size") or [0, 1e6])[1]) * 10 + 1e6
                detector = DetectorConfig(ground_y=height, min_confidence=min_conf)
                ground_diag = {"method": "unavailable", "reason": str(exc)}
                ground_quality = {"method": "unavailable", "problems": [str(exc)]}
        conditions = CaptureConditions(camera_view=video["camera_view"] or None,
                                       direction=video["direction"] or None,
                                       discipline=video["discipline"])
        stored = self.store.read_json(vid, "corrections.json", [])
        corrections = [Correction.from_json(c) for c in stored]
        inp = AnalysisInput(video["sha256"], index, StoredPoseAdapter(model, frames), detector, conditions,
                            strict_corrections=False, ground_quality=ground_quality)
        mode = str(model.config.get("mode", mode))   # 姿勢を推定したときのモードを使う
        if mode != "performance":
            inp.preview_reason = "軽量モデル（balanced）での解析。正式な計測には精度優先モードを使う"
        snap = analyze(inp, corrections)
        if require_last_correction and stored:
            last = len(stored) - 1
            for u in snap["events"]["unapplied_corrections"]:
                if u["index"] == last:
                    raise CorrectionError(u["reason"])
        snap["frame_images"] = {"url": f"/api/videos/{vid}/frames/", "ext": "jpg",
                                "image_size": pose.get("image_size"), "view_scale": pose.get("view_scale", 1.0)}
        snap["ground_estimate"] = ground_diag
        snap["video"] = {"id": vid, "filename": video["filename"], "practice_date": video["practice_date"],
                         "discipline_name": disciplines.get(video["discipline"]).name}
        snap["analysis_mode"] = mode
        self.store.save_analysis(vid, snap, mode)
        return snap
