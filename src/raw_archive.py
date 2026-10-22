"""
========================================
raw_archive.py — 聊天原文抽屉
========================================

把官方 app 导出的聊天原文单独存成一个 SQLite 库，给“按日子翻原话”“在原话里搜”用。

边界：
- 不是记忆桶。breath / 召回 / dream / 衰减 / 合并 / github_sync 都不碰它。
- 库放在 vault 下的 _raw/，与 _media 同级；github_sync 只收 Markdown，不会把它推上去。
- MCP 只读工具是 raw_browse；写入走独立口令导入口，手动删除走登录后的 Dashboard。
- 软删立即从所有读路径隐藏，30 天后清除正文；删除记录保留，用来挡住后续导入。
- 重复导入按 msg_uuid 覆盖，不会翻倍；被删除记录命中的原文不会重建。

对外暴露：RawArchive、get_archive(config)
========================================
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

_BEIJING = ZoneInfo("Asia/Shanghai")

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
    source TEXT NOT NULL DEFAULT '',
    deleted_at TEXT,
    purge_after TEXT
);
CREATE INDEX IF NOT EXISTS idx_raw_day_at ON raw_messages(day, at);
CREATE INDEX IF NOT EXISTS idx_raw_conv_at ON raw_messages(conv_uuid, at);
CREATE TABLE IF NOT EXISTS raw_message_parts (
    msg_uuid TEXT NOT NULL,
    digest TEXT NOT NULL,
    part_index INTEGER NOT NULL,
    part_count INTEGER NOT NULL,
    conv_uuid TEXT NOT NULL,
    conv_name TEXT NOT NULL,
    speaker TEXT NOT NULL,
    at TEXT NOT NULL,
    day TEXT NOT NULL,
    text TEXT NOT NULL,
    thinking TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (msg_uuid, part_index)
);
CREATE TABLE IF NOT EXISTS raw_deletions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_uuid TEXT,
    start_at TEXT,
    end_at TEXT,
    deleted_at TEXT NOT NULL,
    purge_after TEXT NOT NULL,
    purged_at TEXT,
    affected_count INTEGER NOT NULL DEFAULT 0,
    CHECK ((msg_uuid IS NOT NULL) != (start_at IS NOT NULL AND end_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_raw_deletions_uuid ON raw_deletions(msg_uuid);
CREATE INDEX IF NOT EXISTS idx_raw_deletions_range ON raw_deletions(start_at, end_at);
"""
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FIELDS = ("msg_uuid", "conv_uuid", "conv_name", "speaker", "at", "day", "text", "thinking")
_SPEAKERS = {"知知", "顾凛"}
_LEGACY_HOME_ID = re.compile(r"^home-msg-\d+$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _content_digest(text: str, thinking: str) -> str:
    body = json.dumps([text, thinking], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class RawArchive:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(raw_messages)")}
            for column in ("deleted_at", "purge_after"):
                if column not in columns:
                    conn.execute(f"ALTER TABLE raw_messages ADD COLUMN {column} TEXT")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    # ---------- 写 ----------
    def import_rows(self, rows: Iterable[dict[str, Any]], source: str = "") -> dict[str, int]:
        added = updated = skipped = 0
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in rows:
                clean = _clean_row(row)
                if clean is None:
                    skipped += 1
                    continue
                if _is_deleted(conn, clean):
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

    def import_rows_v2(self, rows: Iterable[dict[str, Any]], source: str = "") -> dict[str, int]:
        """Receive complete messages or staged fragments; expose only assembled originals."""
        added = updated = pending = 0
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in rows:
                clean = _clean_row(row)
                if clean is None:
                    raise ValueError("invalid raw message")
                if _is_deleted(conn, clean):
                    conn.execute("DELETE FROM raw_message_parts WHERE msg_uuid=?", (clean["msg_uuid"],))
                    continue
                has_part = any(key in row for key in ("fragment_index", "fragment_count", "fragment_digest"))
                if not has_part:
                    conn.execute("DELETE FROM raw_message_parts WHERE msg_uuid=?", (clean["msg_uuid"],))
                    _delete_legacy_parts(conn, clean["msg_uuid"])
                    existed = _upsert(conn, clean, source)
                    added += not existed
                    updated += existed
                    continue

                try:
                    index = row["fragment_index"]
                    count = row["fragment_count"]
                    digest = row["fragment_digest"]
                    if (type(index) is not int or type(count) is not int
                            or not 0 <= index < count <= 100_000
                            or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)):
                        raise ValueError
                except (KeyError, ValueError, TypeError):
                    raise ValueError("invalid raw message fragment") from None

                if index == 0:
                    # 每次完整重送都从第 0 段开始；旧布局即使正文 digest 相同也不能混拼。
                    conn.execute("DELETE FROM raw_message_parts WHERE msg_uuid=?", (clean["msg_uuid"],))
                else:
                    conn.execute("DELETE FROM raw_message_parts WHERE msg_uuid=? AND digest<>?",
                                 (clean["msg_uuid"], digest))
                conn.execute(
                    "INSERT INTO raw_message_parts "
                    "(msg_uuid,digest,part_index,part_count,conv_uuid,conv_name,speaker,at,day,text,thinking,source) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(msg_uuid,part_index) DO UPDATE SET "
                    "digest=excluded.digest,part_count=excluded.part_count,conv_uuid=excluded.conv_uuid,"
                    "conv_name=excluded.conv_name,speaker=excluded.speaker,at=excluded.at,day=excluded.day,"
                    "text=excluded.text,thinking=excluded.thinking,source=excluded.source",
                    (clean["msg_uuid"], digest, index, count, *(clean[f] for f in _FIELDS if f != "msg_uuid"), source[:80]),
                )
                parts = conn.execute(
                    "SELECT * FROM raw_message_parts WHERE msg_uuid=? ORDER BY part_index",
                    (clean["msg_uuid"],),
                ).fetchall()
                if len(parts) != count:
                    pending += 1
                    continue
                if any(part["part_index"] != i or part["part_count"] != count or part["digest"] != digest
                       or any(part[f] != clean[f] for f in ("conv_uuid", "conv_name", "speaker", "at", "day"))
                       for i, part in enumerate(parts)):
                    raise ValueError("inconsistent raw message fragments")
                assembled = dict(clean)
                assembled["text"] = "".join(part["text"] for part in parts)
                assembled["thinking"] = "".join(part["thinking"] for part in parts)
                if _content_digest(assembled["text"], assembled["thinking"]) != digest:
                    raise ValueError("raw message fragment digest mismatch")
                _delete_legacy_parts(conn, clean["msg_uuid"])
                existed = _upsert(conn, assembled, source)
                conn.execute("DELETE FROM raw_message_parts WHERE msg_uuid=?", (clean["msg_uuid"],))
                added += not existed
                updated += existed
        return {"added": added, "updated": updated, "pending": pending}

    def delete_source(self, source: str) -> int:
        """撤回某一次导入（按导入时带的 source 标签），给灌错的那批反悔用。"""
        if not source:
            return 0
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM raw_message_parts WHERE source=?", (source,))
            return conn.execute("DELETE FROM raw_messages WHERE source=?", (source,)).rowcount

    def hide_message(self, msg_uuid: str, *, now: datetime | None = None) -> int:
        """Hide one original immediately and retain a content-free reimport tombstone."""
        msg_uuid = _base_uuid(msg_uuid)
        if not msg_uuid or len(msg_uuid) > 80:
            raise ValueError("msg_uuid 无效")
        deleted_at, purge_after = _deletion_times(now)
        target = "msg_uuid=?"
        target_params: tuple[str, ...] = (msg_uuid,)
        if _LEGACY_HOME_ID.fullmatch(msg_uuid):
            target += " OR msg_uuid GLOB ?"
            target_params += (msg_uuid + "-part-[0-9]*",)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            count = conn.execute(
                f"SELECT COUNT(*) FROM raw_messages WHERE deleted_at IS NULL AND ({target})",  # nosec B608
                target_params,
            ).fetchone()[0]
            if not count:
                raise ValueError("这条原文不存在或已藏起")
            conn.execute(
                "INSERT INTO raw_deletions(msg_uuid,deleted_at,purge_after,affected_count) "
                "VALUES (?,?,?,?)", (msg_uuid, deleted_at, purge_after, count),
            )
            conn.execute(
                f"UPDATE raw_messages SET deleted_at=?, purge_after=? "
                f"WHERE deleted_at IS NULL AND ({target})",  # nosec B608
                (deleted_at, purge_after, *target_params),
            )
            conn.execute("DELETE FROM raw_message_parts WHERE msg_uuid=?", (msg_uuid,))
        return int(count)

    def hide_range(self, start: str, end: str, *, now: datetime | None = None) -> int:
        """Hide inclusive Beijing timestamps/days, including future reimports in the range."""
        start_at, end_at = _boundary(start, end=False), _boundary(end, end=True)
        if start_at > end_at:
            raise ValueError("开始时间不能晚于结束时间")
        deleted_at, purge_after = _deletion_times(now)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            count = conn.execute(
                "SELECT COUNT(*) FROM raw_messages WHERE deleted_at IS NULL AND at BETWEEN ? AND ?",
                (start_at, end_at),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO raw_deletions(start_at,end_at,deleted_at,purge_after,affected_count) "
                "VALUES (?,?,?,?,?)", (start_at, end_at, deleted_at, purge_after, count),
            )
            conn.execute(
                "UPDATE raw_messages SET deleted_at=?, purge_after=? "
                "WHERE deleted_at IS NULL AND at BETWEEN ? AND ?",
                (deleted_at, purge_after, start_at, end_at),
            )
            conn.execute("DELETE FROM raw_message_parts WHERE at BETWEEN ? AND ?", (start_at, end_at))
        return int(count)

    def purge_due(self, *, now: datetime | None = None) -> int:
        """Physically remove hidden content after 30 days; keep the tombstones forever."""
        current = (now or datetime.now(_BEIJING)).astimezone(_BEIJING).isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            count = conn.execute(
                "DELETE FROM raw_messages WHERE deleted_at IS NOT NULL AND purge_after<=?", (current,)
            ).rowcount
            conn.execute(
                "UPDATE raw_deletions SET purged_at=? WHERE purged_at IS NULL AND purge_after<=?",
                (current, current),
            )
        return int(count)

    def deletion_log(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id,msg_uuid,start_at,end_at,deleted_at,purge_after,purged_at,affected_count "
                "FROM raw_deletions ORDER BY id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 读 ----------
    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) n, MIN(day) first, MAX(day) last, COUNT(DISTINCT day) days "
                "FROM raw_messages WHERE deleted_at IS NULL"
            ).fetchone()
        return dict(row)

    def days(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT day, COUNT(*) n FROM raw_messages WHERE deleted_at IS NULL GROUP BY day ORDER BY day"
            ).fetchall()
        return [dict(r) for r in rows]

    def rows_for_days(self, days: list[str]) -> list[dict[str, Any]]:
        """Every message on the given calendar days, oldest first (for the daily impression)."""
        wanted = [d for d in days if _DAY.match(d)]
        if not wanted:
            return []
        marks = ",".join("?" for _ in wanted)
        with self._connect() as conn:
            rows = conn.execute(
                # Only parameter placeholders are interpolated; day values stay bound.
                f"SELECT msg_uuid, conv_name, speaker, at, day, text FROM raw_messages "  # nosec B608
                f"WHERE deleted_at IS NULL AND day IN ({marks}) ORDER BY at, conv_uuid, msg_uuid",
                wanted,
            ).fetchall()
        return [dict(r) for r in rows]

    def day(self, day: str, offset: int = 0, limit: int = 40) -> tuple[list[dict[str, Any]], int]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM raw_messages WHERE deleted_at IS NULL AND day=?", (day,)).fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM raw_messages WHERE deleted_at IS NULL AND day=? ORDER BY at, conv_uuid LIMIT ? OFFSET ?",
                (day, limit, offset),
            ).fetchall()
        return [dict(r) for r in rows], int(total)

    def range_days(
        self, start_day: str, end_day: str, offset: int = 0, limit: int = 40
    ) -> tuple[list[dict[str, Any]], int]:
        """Page through visible originals in an inclusive Beijing calendar range."""
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM raw_messages WHERE deleted_at IS NULL AND day BETWEEN ? AND ?",
                (start_day, end_day),
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM raw_messages WHERE deleted_at IS NULL AND day BETWEEN ? AND ? "
                "ORDER BY at, conv_uuid, msg_uuid LIMIT ? OFFSET ?",
                (start_day, end_day, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows], int(total)

    def search(
        self, query: str, limit: int = 8, include_thinking: bool = False, speaker: str = "",
        start_day: str = "", end_day: str = "",
    ) -> list[dict[str, Any]]:
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where = "deleted_at IS NULL AND (text LIKE ? ESCAPE '\\'" + (" OR thinking LIKE ? ESCAPE '\\')" if include_thinking else ")")
        params: list[Any] = [like, like] if include_thinking else [like]
        if speaker in _SPEAKERS:
            where += " AND speaker=?"
            params.append(speaker)
        range_where = ""
        range_params: list[Any] = []
        if start_day and end_day:
            where += " AND day BETWEEN ? AND ?"
            params.extend((start_day, end_day))
            range_where = " AND day BETWEEN ? AND ?"
            range_params = [start_day, end_day]
        with self._connect() as conn:
            hits = conn.execute(
                # where uses fixed SQL fragments; search values stay bound.
                f"SELECT * FROM raw_messages WHERE {where} ORDER BY at LIMIT ?",  # nosec B608
                (*params, limit),
            ).fetchall()
            results = []
            for hit in hits:
                before = conn.execute(
                    "SELECT speaker, text FROM raw_messages WHERE deleted_at IS NULL AND conv_uuid=? AND at<? "
                    f"{range_where} ORDER BY at DESC LIMIT 1",  # nosec B608
                    (hit["conv_uuid"], hit["at"], *range_params),
                ).fetchone()
                after = conn.execute(
                    "SELECT speaker, text FROM raw_messages WHERE deleted_at IS NULL AND conv_uuid=? AND at>? "
                    f"{range_where} ORDER BY at LIMIT 1",  # nosec B608
                    (hit["conv_uuid"], hit["at"], *range_params),
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


def _base_uuid(msg_uuid: str) -> str:
    value = str(msg_uuid or "").strip()
    match = re.fullmatch(r"(home-msg-\d+)-part-\d+", value)
    return match.group(1) if match else value


def _deletion_times(now: datetime | None) -> tuple[str, str]:
    current = (now or datetime.now(_BEIJING)).astimezone(_BEIJING)
    return current.isoformat(timespec="seconds"), (current + timedelta(days=30)).isoformat(timespec="seconds")


def _boundary(value: str, *, end: bool) -> str:
    value = str(value or "").strip()
    if _DAY.fullmatch(value):
        day = datetime.fromisoformat(value)
        stamp = day.replace(hour=23, minute=59, second=59, microsecond=999999) if end else day
    else:
        try:
            stamp = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("时间须为 YYYY-MM-DD 或 ISO 日期时间") from None
        if stamp.microsecond:
            raise ValueError("时间精确到秒即可")
        if end:
            stamp = stamp.replace(microsecond=999999)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=_BEIJING)
    return stamp.astimezone(_BEIJING).isoformat(timespec="microseconds" if end else "seconds")


def _is_deleted(conn: sqlite3.Connection, clean: dict[str, str]) -> bool:
    return conn.execute(
        "SELECT 1 FROM raw_deletions WHERE msg_uuid=? "
        "OR (start_at IS NOT NULL AND ? BETWEEN start_at AND end_at) LIMIT 1",
        (_base_uuid(clean["msg_uuid"]), clean["at"]),
    ).fetchone() is not None


def _upsert(conn: sqlite3.Connection, clean: dict[str, str], source: str) -> bool:
    existed = conn.execute("SELECT 1 FROM raw_messages WHERE msg_uuid=?", (clean["msg_uuid"],)).fetchone()
    conn.execute(
        "INSERT OR REPLACE INTO raw_messages "
        "(msg_uuid, conv_uuid, conv_name, speaker, at, day, text, thinking, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        tuple(clean[f] for f in _FIELDS) + (source[:80],),
    )
    return bool(existed)


def _delete_legacy_parts(conn: sqlite3.Connection, msg_uuid: str) -> None:
    if _LEGACY_HOME_ID.fullmatch(msg_uuid):
        conn.execute("DELETE FROM raw_messages WHERE msg_uuid GLOB ?",
                     (msg_uuid + "-part-[0-9]*",))


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
