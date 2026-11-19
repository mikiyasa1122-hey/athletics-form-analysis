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
from datetime import datetime, timedelta, timezone
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
CREATE TABLE IF NOT EXISTS analysis_versions (
  video_id TEXT NOT NULL, revision INTEGER NOT NULL, analysis_id TEXT NOT NULL, created_at TEXT NOT NULL,
  mode TEXT NOT NULL, model TEXT NOT NULL, PRIMARY KEY (video_id, revision)
);
"""
VIDEO_FIELDS = ("discipline", "camera_view", "direction", "practice_date", "place", "record", "notes", "tags",
                "declared_capture_fps", "declared_slowmo", "ground_override")
JOB_STATES = ("queued", "probing", "inferring", "analyzing", "completed", "failed", "cancel_requested", "cancelled")


class StoreError(ValueError):
    pass


class NotFound(KeyError):
    pass


class Busy(StoreError):
    """実行中の解析と衝突する操作（HTTP 409）。"""


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
            self._migrate_hash_named_analyses()
            # 前回の起動中に終わらなかったジョブは失敗として閉じる（孤児ジョブを残さない: ARCH-005）
            self._db.execute("UPDATE jobs SET state='failed', error='アプリの終了で中断された', updated_at=? "
                             "WHERE state IN ('queued','probing','inferring','analyzing','cancel_requested')", (now(),))
            self._db.commit()

    def _migrate_hash_named_analyses(self) -> None:
        """旧形式（analyses/<ハッシュ>.json、版の表なし）の最新の解析を、版 1 として登録する（再レビュー N3）。"""
        rows = self._db.execute("SELECT id, latest_analysis FROM videos WHERE latest_analysis IS NOT NULL").fetchall()
        for vid, latest in rows:
            if not re.fullmatch(r"[0-9a-f]{64}", latest or ""):
                continue
            has = self._db.execute("SELECT 1 FROM analysis_versions WHERE video_id=?", (vid,)).fetchone()
            src = os.path.join(self.root, "videos", vid, "analyses", f"{latest}.json")
            dst = os.path.join(self.root, "videos", vid, "analyses", "r1.json")
            if has or not os.path.isfile(src) or os.path.exists(dst):
                continue
            with open(src, encoding="utf-8") as fh:
                snap = json.load(fh)
            snap["revision"] = 1
            snap.setdefault("ground_quality", {"method": "legacy", "problems": [
                "旧形式の解析（地面の質・追跡の区切りの確認なし）。計算し直す"]})
            with open(dst, "x", encoding="utf-8") as fh:
                json.dump(snap, fh, ensure_ascii=False)
            self._db.execute("INSERT INTO analysis_versions VALUES (?,?,?,?,?,?)",
                             (vid, 1, latest, now(), "legacy", json.dumps(snap.get("model", {}), ensure_ascii=False)))
            self._db.execute("UPDATE videos SET latest_analysis='1' WHERE id=?", (vid,))
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
            if self._active_job(video_id):
                # 解析中に消すと、ワーカーが書き続けて残骸が残る。先に取り消してもらう（レビュー B2）
                raise Busy("解析中の動画は削除できない。解析を取り消してから削除する"
                           "（取り消しが終わらないときは、2分待つかアプリを再起動する）")
            self._db.execute("DELETE FROM videos WHERE id=?", (video_id,))
            self._db.execute("DELETE FROM analysis_versions WHERE video_id=?", (video_id,))
            self._db.execute("DELETE FROM jobs WHERE video_id=?", (video_id,))
            self._db.commit()
        shutil.rmtree(d, ignore_errors=True)   # 元動画・派生物・姿勢・解析をまとめて消す（FR-HIST-005, AC-009）
        if os.path.exists(d):
            raise StoreError("一部のファイルを削除できなかった（他のプログラムが開いている可能性）。もう一度削除する")

    def _active_job(self, video_id: str):
        # 取り消しを頼んでから 2分たっても終わらないジョブ（取り消しを確かめない処理の中で止まっている）は
        # 失敗として閉じ、削除や再実行が詰まらないようにする（再レビュー N4）
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(timespec="seconds")
        self._db.execute("UPDATE jobs SET state='failed', error='取り消しが終わらなかったので閉じた', updated_at=? "
                         "WHERE video_id=? AND state='cancel_requested' AND updated_at < ?", (now(), video_id, cutoff))
        self._db.commit()
        return self._db.execute("SELECT id FROM jobs WHERE video_id=? AND state IN "
                                "('queued','probing','inferring','analyzing','cancel_requested')", (video_id,)).fetchone()

    def cleanup_orphans(self) -> list[str]:
        """DB に行のない動画フォルダ（強制終了したアップロードの残骸）を消す。"""
        with self._lock:
            known = {r[0] for r in self._db.execute("SELECT id FROM videos").fetchall()}
        removed = []
        for name in os.listdir(os.path.join(self.root, "videos")):
            if ID_RE.match(name) and name not in known:
                shutil.rmtree(os.path.join(self.root, "videos", name), ignore_errors=True)
                removed.append(name)
        return removed

    # ---------- JSON ファイル ----------
    def write_json(self, video_id: str, name: str, obj: Any, exclusive: bool = False) -> str:
        vdir = self.video_dir(video_id)
        if not os.path.isdir(vdir):
            raise NotFound(video_id)   # 削除された動画のフォルダを作り直さない（レビュー B2）
        path = os.path.join(vdir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if exclusive and os.path.exists(path):
            raise StoreError(f"{name} はすでにある。解析の版は上書きしない")
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
    def save_analysis(self, video_id: str, snapshot: dict[str, Any], mode: str) -> int:
        """解析を新しい版として保存する。既存の版のファイルは書き換えない（レビュー B3）。版の番号を返す。

        analysis_id は解析の入力と結果から決まる（同じ入力なら同じ ID: 再現性）。版はそれとは別に毎回増える。
        """
        with self._lock:
            row = self._db.execute("SELECT MAX(revision) FROM analysis_versions WHERE video_id=?", (video_id,)).fetchone()
            revision = (row[0] or 0) + 1
            # 書き込みの後・登録の前に落ちて、ファイルだけ残った番号は飛ばす（再レビュー N1）
            while os.path.exists(os.path.join(self.video_dir(video_id), "analyses", f"r{revision}.json")):
                revision += 1
            snapshot["revision"] = revision
            self.write_json(video_id, f"analyses/r{revision}.json", snapshot, exclusive=True)
            self._db.execute("INSERT INTO analysis_versions VALUES (?,?,?,?,?,?)",
                             (video_id, revision, snapshot["analysis_id"], now(), mode,
                              json.dumps(snapshot["model"], ensure_ascii=False)))
            self._db.execute("UPDATE videos SET latest_analysis=? WHERE id=?", (str(revision), video_id))
            self._db.commit()
        return revision

    def get_analysis(self, video_id: str, revision: int | str | None = None) -> dict[str, Any] | None:
        v = self.get_video(video_id)
        rev = revision if revision not in (None, "") else v["latest_analysis"]
        if rev is None or not re.fullmatch(r"\d{1,6}", str(rev)):
            return None
        return self.read_json(video_id, f"analyses/r{int(rev)}.json")

    def list_analyses(self, video_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM analysis_versions WHERE video_id=? ORDER BY revision DESC", (video_id,)).fetchall()]

    # ---------- ジョブ ----------
    def add_job(self, video_id: str, kind: str, mode: str) -> dict[str, Any]:
        jid = new_id()
        with self._lock:
            active = self._active_job(video_id)
            if active:
                raise Busy("この動画の解析はすでに実行中")   # 二重実行を防ぐ（ARCH-004）
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

    def transition_job(self, job_id: str, to_state: str, from_states: tuple[str, ...], **fields: Any) -> bool:
        """状態が from_states のどれかのときだけ変える（取消と完了の競合で、完了したジョブを上書きしない: レビュー N1）。"""
        fields["state"] = to_state
        fields["updated_at"] = now()
        with self._lock:
            sets = ",".join(f"{k}=?" for k in fields)
            marks = ",".join("?" * len(from_states))
            cur = self._db.execute(f"UPDATE jobs SET {sets} WHERE id=? AND state IN ({marks})",
                                   (*fields.values(), job_id, *from_states))
            self._db.commit()
            return cur.rowcount == 1

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
