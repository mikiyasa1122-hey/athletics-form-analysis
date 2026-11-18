"""ローカル保存（FR-VID-002/005/008, FR-HIST-001〜005, §11.2）。

data/
  athform.db                       … 動画・ジョブ・解析の一覧（SQLite）
  videos/<video_id>/original.<ext> … 原本（改変しない。SHA-256 を記録）
                    probe.json     … ffprobe の出力
                    frames/<source_frame_id>.jpg … 表示用（原本フレームIDで対応）
                    pose.json      … 姿勢推定の生値（推論のやり直しなしで訂正・再解析するため）
                    corrections.json … 手動訂正（自動検出を上書きしない別レイヤー）
                    analyses/<analysis_id>.json … 解析スナップショット（不変。再解析は別ファイル）
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

ID_RE = re.compile(r"^[a-z0-9]{12}$")
ALLOWED_EXT = {"mp4", "mov", "m4v", "mkv", "webm", "avi", "ogv"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  id TEXT PRIMARY KEY, created_at TEXT NOT NULL, filename TEXT NOT NULL, ext TEXT NOT NULL,
  size INTEGER NOT NULL, sha256 TEXT NOT NULL, discipline TEXT NOT NULL, camera_view TEXT, direction TEXT,
  practice_date TEXT, place TEXT, record TEXT, notes TEXT, tags TEXT NOT NULL DEFAULT '',
  declared_capture_fps TEXT, declared_slowmo INTEGER NOT NULL DEFAULT 0,
  ground_override TEXT, latest_analysis TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, video_id TEXT NOT NULL, kind TEXT NOT NULL, mode TEXT NOT NULL, state TEXT NOT NULL,
  progress REAL NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '', error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analyses (
  analysis_id TEXT NOT NULL, video_id TEXT NOT NULL, created_at TEXT NOT NULL, mode TEXT NOT NULL,
  model TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY (video_id, analysis_id)
);
"""
VIDEO_FIELDS = ("discipline", "camera_view", "direction", "practice_date", "place", "record", "notes", "tags",
                "declared_capture_fps", "declared_slowmo", "ground_override")
JOB_STATES = ("queued", "probing", "inferring", "analyzing", "completed", "failed", "cancel_requested", "cancelled")


class StoreError(ValueError):
    pass


class NotFound(KeyError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return secrets.token_hex(6)


class Store:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(os.path.join(self.root, "videos"), exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(os.path.join(self.root, "athform.db"), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            # 前回の起動中に終わらなかったジョブは失敗として閉じる（孤児ジョブを残さない: ARCH-005）
            self._db.execute("UPDATE jobs SET state='failed', error='アプリの終了で中断された', updated_at=? "
                             "WHERE state IN ('queued','probing','inferring','analyzing','cancel_requested')", (now(),))
            self._db.commit()

    # ---------- パス ----------
    def video_dir(self, video_id: str) -> str:
        if not ID_RE.match(video_id or ""):
            raise NotFound(video_id)
        return os.path.join(self.root, "videos", video_id)

    def original_path(self, video: dict[str, Any]) -> str:
        return os.path.join(self.video_dir(video["id"]), f"original.{video['ext']}")

    def frames_dir(self, video_id: str) -> str:
        return os.path.join(self.video_dir(video_id), "frames")

    # ---------- 動画 ----------
    def add_video(self, video_id: str, filename: str, ext: str, size: int, sha256: str, meta: dict[str, Any]) -> dict:
        row = {"id": video_id, "created_at": now(), "filename": filename, "ext": ext, "size": size,
               "sha256": sha256, **{k: meta.get(k) for k in VIDEO_FIELDS}}
        row["tags"] = row["tags"] or ""
        row["declared_slowmo"] = 1 if row["declared_slowmo"] else 0
        with self._lock:
            cols = ",".join(row)
            self._db.execute(f"INSERT INTO videos ({cols}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))
            self._db.commit()
        return self.get_video(video_id)

    def get_video(self, video_id: str) -> dict[str, Any]:
        self.video_dir(video_id)
        with self._lock:
            r = self._db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        if r is None:
            raise NotFound(video_id)
        return dict(r)

    def list_videos(self, q: str = "", discipline: str = "", date_from: str = "", date_to: str = "",
                    tag: str = "") -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM videos WHERE 1=1", []
        if discipline:
            sql += " AND discipline=?"
            args.append(discipline)
        if date_from:
            sql += " AND practice_date>=?"
            args.append(date_from)
        if date_to:
            sql += " AND practice_date<=?"
            args.append(date_to)
        if tag:
            sql += " AND (','||tags||',') LIKE ?"
            args.append(f"%,{tag},%")
        if q:
            sql += " AND (filename LIKE ? OR notes LIKE ? OR place LIKE ? OR record LIKE ?)"
            args += [f"%{q}%"] * 4
        sql += " ORDER BY COALESCE(practice_date, created_at) DESC, created_at DESC"
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def update_video(self, video_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        self.get_video(video_id)
        bad = set(fields) - set(VIDEO_FIELDS)
        if bad:
            raise StoreError(f"変更できない項目: {sorted(bad)}")
        if not fields:
            return self.get_video(video_id)
        if "declared_slowmo" in fields:
            fields["declared_slowmo"] = 1 if fields["declared_slowmo"] else 0
        with self._lock:
            sets = ",".join(f"{k}=?" for k in fields)
            self._db.execute(f"UPDATE videos SET {sets} WHERE id=?", (*fields.values(), video_id))
            self._db.commit()
        return self.get_video(video_id)

    def delete_video(self, video_id: str) -> None:
        d = self.video_dir(video_id)
        self.get_video(video_id)
        with self._lock:
            self._db.execute("DELETE FROM videos WHERE id=?", (video_id,))
            self._db.execute("DELETE FROM analyses WHERE video_id=?", (video_id,))
            self._db.execute("DELETE FROM jobs WHERE video_id=?", (video_id,))
            self._db.commit()
        shutil.rmtree(d, ignore_errors=True)   # 元動画・派生物・姿勢・解析をまとめて消す（FR-HIST-005, AC-009）

    # ---------- JSON ファイル ----------
    def write_json(self, video_id: str, name: str, obj: Any) -> str:
        path = os.path.join(self.video_dir(video_id), name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
        os.replace(tmp, path)   # 途中で落ちても壊れたファイルを残さない
        return path

    def read_json(self, video_id: str, name: str, default: Any = None) -> Any:
        path = os.path.join(self.video_dir(video_id), name)
        if not os.path.isfile(path):
            return default
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    # ---------- 解析 ----------
    def save_analysis(self, video_id: str, snapshot: dict[str, Any], mode: str, revision: int) -> None:
        aid = snapshot["analysis_id"]
        self.write_json(video_id, f"analyses/{aid}.json", snapshot)
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO analyses VALUES (?,?,?,?,?,?)",
                             (aid, video_id, now(), mode, json.dumps(snapshot["model"], ensure_ascii=False), revision))
            self._db.execute("UPDATE videos SET latest_analysis=? WHERE id=?", (aid, video_id))
            self._db.commit()

    def get_analysis(self, video_id: str, analysis_id: str | None = None) -> dict[str, Any] | None:
        v = self.get_video(video_id)
        aid = analysis_id or v["latest_analysis"]
        if not aid or not re.fullmatch(r"[0-9a-f]{64}", aid):
            return None
        return self.read_json(video_id, f"analyses/{aid}.json")

    def list_analyses(self, video_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM analyses WHERE video_id=? ORDER BY created_at DESC", (video_id,)).fetchall()]

    # ---------- ジョブ ----------
    def add_job(self, video_id: str, kind: str, mode: str) -> dict[str, Any]:
        jid = new_id()
        with self._lock:
            active = self._db.execute("SELECT id FROM jobs WHERE video_id=? AND state IN "
                                      "('queued','probing','inferring','analyzing','cancel_requested')",
                                      (video_id,)).fetchone()
            if active:
                raise StoreError("この動画の解析はすでに実行中")   # 二重実行を防ぐ（ARCH-004）
            self._db.execute("INSERT INTO jobs (id, video_id, kind, mode, state, created_at, updated_at) "
                             "VALUES (?,?,?,?,?,?,?)", (jid, video_id, kind, mode, "queued", now(), now()))
            self._db.commit()
        return self.get_job(jid)

    def get_job(self, job_id: str) -> dict[str, Any]:
        if not ID_RE.match(job_id or ""):
            raise NotFound(job_id)
        with self._lock:
            r = self._db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if r is None:
            raise NotFound(job_id)
        return dict(r)

    def update_job(self, job_id: str, **fields: Any) -> None:
        if "state" in fields and fields["state"] not in JOB_STATES:
            raise StoreError(f"不明な状態: {fields['state']}")
        fields["updated_at"] = now()
        with self._lock:
            sets = ",".join(f"{k}=?" for k in fields)
            self._db.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))
            self._db.commit()

    def latest_job(self, video_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM jobs WHERE video_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                                 (video_id,)).fetchone()
        return dict(r) if r else None

    def close(self) -> None:
        with self._lock:
            self._db.close()
