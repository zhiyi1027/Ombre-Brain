"""
========================================
raw_archive.py — 聊天原文抽屉
========================================

把官方 app 导出的聊天原文单独存成一个 SQLite 库，给“按日子翻原话”“在原话里搜”用。

边界：
- 不是记忆桶。breath / 召回 / dream / 衰减 / 合并 / github_sync 都不碰它。
- 库放在 vault 下的 _raw/，与 _media 同级；github_sync 只收 Markdown，不会把它推上去。
  原文随时能从导出 JSON 重建，导出文件本身就是备份。
- 只读工具：raw_day、raw_search。写入只走带独立口令的导入口（web/raw_import.py）。
- 重复导入按 msg_uuid 覆盖，不会翻倍。

对外暴露：RawArchive、get_archive(config)
========================================
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from typing import Any, Iterable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_messages (
    msg_uuid TEXT PRIMARY KEY,
    conv_uuid TEXT NOT NULL,
    conv_name TEXT NOT NULL DEFAULT '',
    speaker TEXT NOT NULL,
    at TEXT NOT NULL,
    day TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    thinking TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_raw_day_at ON raw_messages(day, at);
CREATE INDEX IF NOT EXISTS idx_raw_conv_at ON raw_messages(conv_uuid, at);
"""
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FIELDS = ("msg_uuid", "conv_uuid", "conv_name", "speaker", "at", "day", "text", "thinking")
_SPEAKERS = {"知知", "顾凛"}


class RawArchive:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    # ---------- 写 ----------
    def import_rows(self, rows: Iterable[dict[str, Any]], source: str = "") -> dict[str, int]:
        added = updated = skipped = 0
        with self._lock, self._connect() as conn:
            for row in rows:
                clean = _clean_row(row)
                if clean is None:
                    skipped += 1
                    continue
                exists = conn.execute(
                    "SELECT 1 FROM raw_messages WHERE msg_uuid=?", (clean["msg_uuid"],)
                ).fetchone()
                conn.execute(
                    "INSERT OR REPLACE INTO raw_messages "
                    "(msg_uuid, conv_uuid, conv_name, speaker, at, day, text, thinking, source) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    tuple(clean[f] for f in _FIELDS) + (source[:80],),
                )
                if exists:
                    updated += 1
                else:
                    added += 1
        return {"added": added, "updated": updated, "skipped": skipped}

    # ---------- 读 ----------
    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) n, MIN(day) first, MAX(day) last, COUNT(DISTINCT day) days "
                "FROM raw_messages"
            ).fetchone()
        return dict(row)

    def day(self, day: str, offset: int = 0, limit: int = 40) -> tuple[list[dict[str, Any]], int]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM raw_messages WHERE day=?", (day,)).fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM raw_messages WHERE day=? ORDER BY at, conv_uuid LIMIT ? OFFSET ?",
                (day, limit, offset),
            ).fetchall()
        return [dict(r) for r in rows], int(total)

    def search(
        self, query: str, limit: int = 8, include_thinking: bool = False, speaker: str = ""
    ) -> list[dict[str, Any]]:
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where = "(text LIKE ? ESCAPE '\\'" + (" OR thinking LIKE ? ESCAPE '\\')" if include_thinking else ")")
        params: list[Any] = [like, like] if include_thinking else [like]
        if speaker in _SPEAKERS:
            where += " AND speaker=?"
            params.append(speaker)
        with self._connect() as conn:
            hits = conn.execute(
                f"SELECT * FROM raw_messages WHERE {where} ORDER BY at LIMIT ?", (*params, limit)
            ).fetchall()
            results = []
            for hit in hits:
                before = conn.execute(
                    "SELECT speaker, text FROM raw_messages WHERE conv_uuid=? AND at<? "
                    "ORDER BY at DESC LIMIT 1",
                    (hit["conv_uuid"], hit["at"]),
                ).fetchone()
                after = conn.execute(
                    "SELECT speaker, text FROM raw_messages WHERE conv_uuid=? AND at>? "
                    "ORDER BY at LIMIT 1",
                    (hit["conv_uuid"], hit["at"]),
                ).fetchone()
                item = dict(hit)
                item["before"] = dict(before) if before else None
                item["after"] = dict(after) if after else None
                results.append(item)
        return results


def _clean_row(row: Any) -> dict[str, str] | None:
    if not isinstance(row, dict):
        return None
    clean = {f: str(row.get(f) or "") for f in _FIELDS}
    if not clean["msg_uuid"] or not clean["conv_uuid"] or not clean["at"]:
        return None
    if clean["speaker"] not in _SPEAKERS or not _DAY.match(clean["day"]):
        return None
    if not clean["text"] and not clean["thinking"]:
        return None
    clean["msg_uuid"] = clean["msg_uuid"][:80]
    clean["conv_uuid"] = clean["conv_uuid"][:80]
    clean["conv_name"] = clean["conv_name"][:200]
    return clean


_instances: dict[str, RawArchive] = {}
_instances_lock = threading.Lock()


def archive_path(config: dict[str, Any]) -> str:
    configured = str((config or {}).get("raw_archive_path") or "").strip()
    if configured:
        return configured
    return os.path.join(str(config["buckets_dir"]), "_raw", "raw_archive.sqlite3")


def get_archive(config: dict[str, Any]) -> RawArchive:
    path = archive_path(config)
    with _instances_lock:
        if path not in _instances:
            _instances[path] = RawArchive(path)
        return _instances[path]
