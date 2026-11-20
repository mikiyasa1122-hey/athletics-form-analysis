"""ローカル1人用のWebアプリ（127.0.0.1 だけで待ち受ける）。

  .venv/Scripts/python -m athform.server [--port 8766] [--data data]

セキュリティ（NFR-SEC-002/003/005, S-0001）:
- 127.0.0.1 にだけバインドし、Host が 127.0.0.1/localhost:<port> 以外の要求は拒否（DNS リバインディング対策）。
- 状態を変える要求は Origin が同じであること、かつ JSON（application/json）か専用ヘッダー（アップロード）を必須にする。
  他サイトのページからは、事前確認（CORS）なしにこの形の要求を送れない。CORS は許可しない。
- ID は形式を検査してからパスに使う。アップロードは上限サイズ・拡張子・ffprobe で動画として読めるかを確かめる。
- ログに動画のパスや内容を出さない。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sys
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import disciplines
from .analysis import AnalysisError
from .cli import check_probe_matches_video, probe_video
from .compare import compare
from .events import Correction, CorrectionError
from .jobs import JobRunner
import math
import threading

from .store import ALLOWED_EXT, VIDEO_FIELDS, Busy, NotFound, Store, StoreError, new_id
from .timebase import TimebaseError, build_frame_index
from .tools import find_tool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")
MAX_UPLOAD = 4 * 1024 ** 3
MAX_JSON = 1024 ** 2
VIEWS = ("side", "front", "rear", "oblique")
STATIC_FILES = {"index.html", "app.js", "player-core.js", "style.css"}   # 配信してよいファイルだけ（レビュー N6）
DIRECTIONS = ("left_to_right", "right_to_left")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def validate_meta(meta: dict[str, Any], partial: bool) -> dict[str, Any]:
    if not isinstance(meta, dict):
        raise ApiError(400, "JSON オブジェクトで送る")
    unknown = set(meta) - set(VIDEO_FIELDS)
    if unknown:
        raise ApiError(400, f"不明な項目: {sorted(unknown)}")
    out: dict[str, Any] = {}
    for k, v in meta.items():
        if k == "declared_slowmo":
            if not isinstance(v, bool):
                raise ApiError(400, "declared_slowmo は true/false")
            out[k] = v
            continue
        if k == "ground_override":
            if v in (None, ""):
                out[k] = None
                continue
            if not isinstance(v, dict) or not isinstance(v.get("ground_y"), (int, float)):
                raise ApiError(400, "ground_override は {ground_y, contact_band_px?, ground_slope?, ground_x0?}")
            g = {kk: float(v[kk]) for kk in ("ground_y", "contact_band_px", "ground_slope", "ground_x0") if kk in v}
            # NaN・無限大・負の幅・極端な傾きは受け付けない（レビュー N9）
            if (not all(math.isfinite(x) for x in g.values()) or not 0 < g.get("contact_band_px", 6) <= 200
                    or abs(g.get("ground_slope", 0)) > 1):
                raise ApiError(400, "ground_override の値が範囲外")
            out[k] = json.dumps(g)
            continue
        if v is not None and not isinstance(v, str):
            raise ApiError(400, f"{k} は文字列")
        v = (v or "").strip()
        if len(v) > 2000 or any(ord(c) < 32 and c not in "\n\t" for c in v) or any(0xD800 <= ord(c) <= 0xDFFF for c in v):
            raise ApiError(400, f"{k} に使えない文字が含まれるか、長すぎる")
        if k == "discipline" and v not in disciplines.CATALOG:
            raise ApiError(400, "未登録の種目")
        if k == "camera_view" and v and v not in VIEWS:
            raise ApiError(400, "camera_view は side/front/rear/oblique")
        if k == "direction" and v and v not in DIRECTIONS:
            raise ApiError(400, "direction は left_to_right/right_to_left")
        if k == "practice_date" and v and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ApiError(400, "practice_date は YYYY-MM-DD")
        if k == "declared_capture_fps" and v and not re.fullmatch(r"\d{1,4}(\.\d{1,3})?", v):
            raise ApiError(400, "declared_capture_fps は数値")
        if k == "tags":
            v = ",".join(t.strip() for t in v.split(",") if t.strip())
        out[k] = v or None
    if not partial and "discipline" not in out:
        out["discipline"] = "sprint_100m"
    return out


class App:
    def __init__(self, data_dir: str, port: int, pose_fn=None, ffprobe_runner=None):
        self.store = Store(data_dir)
        self.port = port
        self.ffmpeg = find_tool("ffmpeg")
        self.ffprobe_runner = ffprobe_runner
        self.jobs = JobRunner(self.store, self.ffmpeg, ffprobe_runner=ffprobe_runner, pose_fn=pose_fn)
        self.store.cleanup_orphans()   # 強制終了したアップロードの残骸（DB に行のないフォルダ）を片付ける
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def video_lock(self, video_id: str) -> threading.Lock:
        """動画ごとの訂正の直列化（レビュー N2）。"""
        with self._locks_guard:
            return self._locks.setdefault(video_id, threading.Lock())

    def allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}


class Handler(BaseHTTPRequestHandler):
    server_version = "athform"
    timeout = 120   # 送信が途中で止まった接続がスレッドを掴み続けないように（レビュー N7）
    app: App   # サーバー起動時に設定

    # ---------- 共通 ----------
    def log_message(self, fmt: str, *args: Any) -> None:   # 動画名などをログに出さない
        sys.stderr.write(f"{self.command} {self.path.split('?')[0][:80]} {args[1] if len(args) > 1 else ''}\n")

    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")   # 他サイトに画像などを埋め込ませない
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                         "connect-src 'self'; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: Any) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _check_host(self) -> bool:
        if self.headers.get("Host", "") not in self.app.allowed_hosts():
            self._error(421, "このサーバーは 127.0.0.1 / localhost からのみ使える")
            return False
        return True

    def _check_origin(self) -> None:
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{h}" for h in self.app.allowed_hosts()}:
            raise ApiError(403, "別のサイトからの要求は受け付けない")

    def _check_write(self, need_json: bool) -> None:
        self._check_origin()
        if need_json:
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise ApiError(415, "Content-Type: application/json で送る")
        elif self.headers.get("X-Athform-Upload") != "1":
            raise ApiError(400, "アップロードには X-Athform-Upload: 1 が必要")

    def _read_json(self) -> dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Content-Length が数値でない") from None
        if n < 0 or n > MAX_JSON:
            raise ApiError(413, "大きさが範囲外")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, f"JSON として読めない: {exc}") from exc
        if not isinstance(body, dict):
            raise ApiError(400, "JSON オブジェクトで送る")
        return body

    def _dispatch(self) -> None:
        if not self._check_host():
            return
        parsed = urllib.parse.urlsplit(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        try:
            if self.command not in ("GET", "HEAD"):
                self._check_origin()   # 他サイトからの書き込みは、どのIDかを調べる前に拒否する
            if path.startswith("/api/"):
                self._api(self.command, path[5:].strip("/").split("/"), qs)
            elif self.command in ("GET", "HEAD"):
                self._static(path)
            else:
                raise ApiError(405, "許可されていない")
        except ApiError as e:
            self._error(e.status, str(e))
        except NotFound:
            self._error(404, "見つからない")
        except Busy as e:
            self._error(409, str(e))
        except (StoreError, CorrectionError, AnalysisError, TimebaseError, ValueError) as e:
            self._error(400, str(e))
        except OSError:
            sys.stderr.write("ファイル操作に失敗した（ディスクの空きなど）\n")
            self._error(500, "ファイルを保存できなかった（ディスクの空きを確かめる）")

    do_GET = do_POST = do_PATCH = do_DELETE = do_HEAD = _dispatch

    def do_OPTIONS(self) -> None:   # CORS は許可しない
        self._error(405, "CORS は許可していない")

    # ---------- 静的ファイル ----------
    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        if rel not in STATIC_FILES:
            raise ApiError(404, "見つからない")
        full = os.path.join(WEB_DIR, rel)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            self._send(200, fh.read(), ctype)

    # ---------- API ----------
    def _api(self, method: str, parts: list[str], qs: dict[str, list[str]]) -> None:
        st, app = self.app.store, self.app
        q = {k: v[0] for k, v in qs.items()}
        if parts == ["health"] and method == "GET":
            return self._json(200, {"ok": True, "ffmpeg": bool(app.ffmpeg)})
        if parts == ["disciplines"] and method == "GET":
            return self._json(200, disciplines.catalog_json())
        if parts == ["videos"]:
            if method == "GET":
                videos = st.list_videos(q.get("q", ""), q.get("discipline", ""), q.get("from", ""), q.get("to", ""),
                                        q.get("tag", ""))
                for v in videos:   # 一覧のカードに「解析中 42%」などを出すため
                    j = st.latest_job(v["id"])
                    v["job"] = {"state": j["state"], "progress": j["progress"]} if j else None
                return self._json(200, {"videos": videos})
            if method == "POST":
                self._check_write(need_json=False)
                return self._upload()
        if len(parts) >= 2 and parts[0] == "videos":
            vid = parts[1]
            st.get_video(vid)   # 存在と ID 形式の確認
            rest = parts[2:]
            if not rest:
                if method == "GET":
                    v = st.get_video(vid)
                    return self._json(200, {"video": v, "job": st.latest_job(vid), "analyses": st.list_analyses(vid),
                                            "corrections": st.read_json(vid, "corrections.json", [])})
                if method == "PATCH":
                    self._check_write(need_json=True)
                    return self._json(200, {"video": st.update_video(vid, validate_meta(self._read_json(), True))})
                if method == "DELETE":
                    self._check_write(need_json=True)
                    with app.video_lock(vid):
                        st.delete_video(vid)
                    return self._json(200, {"deleted": vid})
            if rest == ["jobs"] and method == "POST":
                self._check_write(need_json=True)
                body = self._read_json()
                with app.video_lock(vid):
                    job = app.jobs.submit(vid, body.get("kind", "full"), body.get("mode", "performance"))
                return self._json(202, {"job": job})
            if rest == ["analysis"] and method == "GET":
                snap = st.get_analysis(vid, q.get("revision"))
                if snap is None:
                    raise ApiError(404, "解析結果がまだない")
                return self._json(200, snap)
            if rest == ["people"] and method == "GET":
                try:
                    fid = int(q.get("frame", ""))
                except ValueError:
                    raise ApiError(400, "frame は原本フレームID") from None
                return self._json(200, app.jobs.people_in_frame(vid, fid))
            if rest == ["target"] and method == "POST":
                self._check_write(need_json=True)
                body = self._read_json()
                fid, person = body.get("frame_id"), body.get("person")
                if not all(isinstance(v, int) and not isinstance(v, bool) for v in (fid, person)):
                    raise ApiError(400, "frame_id と person は整数")
                with app.video_lock(vid):
                    if self._job_running(vid):
                        raise Busy("解析の実行中は人を選び直せない。終わってから選ぶ")
                    snap = app.jobs.set_target(vid, fid, person)
                return self._json(200, snap)
            if rest == ["corrections"] and method == "POST":
                self._check_write(need_json=True)
                return self._add_correction(vid, self._read_json())
            if len(rest) == 3 and rest[0] == "corrections" and rest[2] == "revoke" and method == "POST":
                self._check_write(need_json=True)
                if not re.fullmatch(r"\d{1,5}", rest[1]):
                    raise ApiError(404, "見つからない")
                return self._revoke_correction(vid, int(rest[1]), self._read_json())
            if rest == ["export"] and method == "GET":
                v = st.get_video(vid)
                body = {"video": v, "corrections": st.read_json(vid, "corrections.json", []),
                        "analyses": [st.get_analysis(vid, a["revision"]) for a in st.list_analyses(vid)]}
                data = json.dumps(body, ensure_ascii=False, indent=1).encode("utf-8")
                return self._send(200, data, "application/json; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="athform-{vid}.json"'})
            if len(rest) == 2 and rest[0] == "frames" and method in ("GET", "HEAD"):
                m = re.fullmatch(r"(\d{1,7})\.jpg", rest[1])
                if not m:
                    raise ApiError(404, "見つからない")
                path = os.path.join(st.frames_dir(vid), f"{int(m.group(1))}.jpg")
                if not os.path.isfile(path):
                    raise ApiError(404, "フレーム画像がない")
                with open(path, "rb") as fh:
                    return self._send(200, fh.read(), "image/jpeg")
        if len(parts) == 2 and parts[0] == "jobs" and method == "GET":
            return self._json(200, {"job": st.get_job(parts[1])})
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel" and method == "POST":
            self._check_write(need_json=True)
            return self._json(200, {"job": app.jobs.cancel(parts[1])})
        if parts == ["compare"] and method == "GET":
            return self._compare(q)
        raise ApiError(404, "見つからない")

    def _upload(self) -> None:
        st = self.app.store
        try:
            n = int(self.headers.get("Content-Length") or "")
        except ValueError:
            raise ApiError(411, "Content-Length が必要") from None
        if n <= 0 or n > MAX_UPLOAD:
            raise ApiError(413, f"ファイルの大きさは 1 バイト〜{MAX_UPLOAD // 1024 ** 3} GB")
        name = urllib.parse.unquote(self.headers.get("X-Filename", ""))[:200]
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in ALLOWED_EXT:
            raise ApiError(400, f"対応している形式: {', '.join(sorted(ALLOWED_EXT))}")
        meta = validate_meta(json.loads(urllib.parse.unquote(self.headers.get("X-Athform-Meta", "") or "{}")), False)
        vid = new_id()
        d = st.video_dir(vid)
        os.makedirs(d)
        path = os.path.join(d, f"original.{ext}")
        h = hashlib.sha256()
        remaining = n
        try:
            with open(path, "wb") as fh:
                while remaining:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        raise ApiError(400, "アップロードが途中で切れた")
                    h.update(chunk)
                    fh.write(chunk)
                    remaining -= len(chunk)
            # 中身が本当に動画か（拡張子だけを信用しない: NFR-SEC-003）。時刻が読めない動画もここで止める
            kwargs = {"runner": self.app.ffprobe_runner} if self.app.ffprobe_runner else {}
            try:
                probe = probe_video(path, **kwargs)
                check_probe_matches_video(probe, path)
                build_frame_index(probe)
            except (SystemExit, TimebaseError, ValueError, OSError) as exc:
                raise ApiError(400, f"動画として読めない: {exc}") from exc
            st.write_json(vid, "probe.json", probe)
            video = st.add_video(vid, os.path.basename(name), ext, n, h.hexdigest(), meta)
        except BaseException:
            shutil.rmtree(d, ignore_errors=True)   # 失敗したアップロードの残骸を残さない
            raise
        self._json(201, {"video": video})

    def _job_running(self, vid: str) -> bool:
        job = self.app.store.latest_job(vid)
        return bool(job) and job["state"] in ("queued", "probing", "inferring", "analyzing", "cancel_requested")

    def _add_correction(self, vid: str, body: dict[str, Any]) -> None:
        st, app = self.app.store, self.app
        body = dict(body)
        if "revoked_at" in body or "revoked_reason" in body:
            raise ApiError(400, "取り消しは /revoke で行う")
        body.setdefault("corrected_by", "local-user")
        from .store import now  # noqa: PLC0415
        body["corrected_at"] = now()
        corr = Correction.from_json(body)
        with app.video_lock(vid):
            if self._job_running(vid):
                raise Busy("解析の実行中は訂正できない。終わってから訂正する")
            existing = st.read_json(vid, "corrections.json", [])
            st.write_json(vid, "corrections.json", existing + [corr.to_json()])
            try:
                snap = app.jobs.analyze_video(vid, require_last_correction=True)   # 推論なしで再計算（AC-018）
            except (CorrectionError, AnalysisError, ValueError):
                st.write_json(vid, "corrections.json", existing)   # 当てられない訂正は保存しない
                raise
        self._json(200, snap)

    def _revoke_correction(self, vid: str, index: int, body: dict[str, Any]) -> None:
        """訂正を取り消す。記録は消さず、取り消した日時と理由を残す（監査のため）。"""
        st, app = self.app.store, self.app
        reason = str(body.get("reason") or "").strip()
        if not reason:
            raise ApiError(400, "取り消しの理由が必要")
        from .store import now  # noqa: PLC0415
        with app.video_lock(vid):
            if self._job_running(vid):
                raise Busy("解析の実行中は取り消せない")
            existing = st.read_json(vid, "corrections.json", [])
            if not 0 <= index < len(existing) or existing[index].get("revoked_at"):
                raise ApiError(404, "取り消せる訂正がない")
            updated = [dict(c) for c in existing]
            updated[index]["revoked_at"] = now()
            updated[index]["revoked_reason"] = reason[:200]
            st.write_json(vid, "corrections.json", updated)
            try:
                snap = app.jobs.analyze_video(vid)
            except (CorrectionError, AnalysisError, ValueError):
                st.write_json(vid, "corrections.json", existing)   # 記録と版が食い違わないよう元に戻す（再レビュー N6）
                raise
        self._json(200, snap)

    def _compare(self, q: dict[str, str]) -> None:
        st = self.app.store

        def load(key: str) -> dict[str, Any]:
            ref = q.get(key, "")
            vid, _, rev = ref.partition(":")
            snap = st.get_analysis(vid, rev or None)
            if snap is None:
                raise ApiError(404, f"{key} の解析結果がない")
            return snap

        def rng(key: str) -> tuple[int, int] | None:
            v = q.get(key)
            if not v:
                return None
            m = re.fullmatch(r"(\d+)-(\d+)", v)
            if not m or int(m.group(1)) > int(m.group(2)):
                raise ApiError(400, f"{key} は 開始-終了 の原本フレームID")
            return int(m.group(1)), int(m.group(2))

        a = load("a")
        b = load("b") if q.get("b") else a
        self._json(200, compare(a, b, rng("ra"), rng("rb")))


def make_server(data_dir: str, port: int, pose_fn=None, ffprobe_runner=None) -> ThreadingHTTPServer:
    app = App(data_dir, port, pose_fn=pose_fn, ffprobe_runner=ffprobe_runner)
    handler = type("BoundHandler", (Handler,), {"app": app})
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    app.port = srv.server_address[1]
    srv.app = app   # type: ignore[attr-defined]
    return srv


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="athform.server")
    ap.add_argument("--port", type=int, default=8766)
    # 利用者のデータはプロジェクトの外（ホームの .athform）に置く。ATHFORM_DATA で変えられる
    ap.add_argument("--data", default=os.environ.get("ATHFORM_DATA") or os.path.join(os.path.expanduser("~"), ".athform", "data"))
    args = ap.parse_args(argv)
    srv = make_server(args.data, args.port)
    print(f"フォーム解析アプリ: http://127.0.0.1:{srv.server_address[1]}/  （終了は Ctrl+C）")
    print(f"データの保存先: {os.path.abspath(args.data)}")
    if not srv.app.ffmpeg:   # type: ignore[attr-defined]
        print("注意: ffmpeg が見つからない。解析はできない（ATHFORM_FFMPEG で場所を指定できる）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
