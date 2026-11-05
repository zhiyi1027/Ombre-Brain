"""Private derived clerk state, separate from buckets and Git sync.

One SQLite transaction publishes a complete day. Failed/incomplete work never
replaces the last successful publication. Chunk caches are resumable, but all
cache hits are revalidated against the current allowed fragments by the caller.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import uuid
from clerk_privacy import VISIBLE_TEXT_POLICY, VisibilityStateError


class ClerkStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "clerk.db"
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS markers(at TEXT,conv TEXT,PRIMARY KEY(at,conv));
                CREATE TABLE IF NOT EXISTS withheld(mid TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY,day TEXT,data TEXT);
                CREATE TABLE IF NOT EXISTS publications(day TEXT PRIMARY KEY,revision TEXT,data TEXT);
                CREATE TABLE IF NOT EXISTS attempts(day TEXT PRIMARY KEY,status TEXT,code TEXT);
                CREATE TABLE IF NOT EXISTS run_guards(day TEXT PRIMARY KEY,revision TEXT,code TEXT);
                CREATE TABLE IF NOT EXISTS lease(id INTEGER PRIMARY KEY CHECK(id=1),owner TEXT,expires REAL);
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY,day TEXT,operation TEXT,status TEXT,
                                                input_upper_bound INTEGER,usage TEXT);
            """)
            if (
                not conn.execute("SELECT 1 FROM markers LIMIT 1").fetchone()
                and not conn.execute("SELECT 1 FROM withheld LIMIT 1").fetchone()
            ):
                conn.execute("INSERT OR IGNORE INTO meta VALUES ('input_policy',?)", (VISIBLE_TEXT_POLICY,))
        os.chmod(self.root, 0o700)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.execute("PRAGMA secure_delete=ON")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def privacy_state(self):
        with self.connect() as conn:
            policy = conn.execute("SELECT value FROM meta WHERE key='input_policy'").fetchone()
            if not policy or policy[0] != VISIBLE_TEXT_POLICY:
                raise VisibilityStateError("legacy privacy state requires visible-text revalidation")
            markers = list(conn.execute("SELECT at,conv FROM markers ORDER BY at,conv"))
            withheld = [r[0] for r in conn.execute("SELECT mid FROM withheld ORDER BY mid")]
        return markers, withheld

    def remember_privacy(self, result):
        with self.connect() as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO markers VALUES (?,?)",
                [(i.opened_at.isoformat(), i.conv_uuid or "") for i in result.intervals],
            )
            conn.executemany(
                "INSERT OR IGNORE INTO withheld VALUES (?)",
                [
                    (d.msg_uuid,)
                    for d in result.decisions
                    if d.reason in ("private_interval", "unconfirmed_private_boundary")
                ],
            )
            return conn.total_changes != before

    def get_cache(self, key):
        with self.connect() as conn:
            row = conn.execute("SELECT data FROM cache WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_cache(self, key, day, data):
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache VALUES (?,?,?)", (key, day, json.dumps(data, ensure_ascii=False))
            )

    def publication(self, day):
        with self.connect() as conn:
            row = conn.execute("SELECT revision,data FROM publications WHERE day=?", (day,)).fetchone()
        return {"revision": row[0], **json.loads(row[1])} if row else None

    def publish(self, day, revision, data, cache_keys, *, owner):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            lease = conn.execute("SELECT owner,expires FROM lease WHERE id=1").fetchone()
            if not lease or lease[0] != owner or lease[1] <= datetime.now(timezone.utc).timestamp():
                raise RuntimeError("clerk_lease_lost")
            conn.execute(
                "INSERT OR REPLACE INTO publications VALUES (?,?,?)",
                (day, revision, json.dumps(data, ensure_ascii=False)),
            )
            conn.execute("INSERT OR REPLACE INTO attempts VALUES (?,?,?)", (day, "ready", ""))
            conn.execute(
                "DELETE FROM cache WHERE day=? AND key NOT IN (SELECT value FROM json_each(?))",
                (day, json.dumps(cache_keys)),
            )

    def attempt(self, day, status, code=""):
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO attempts VALUES (?,?,?)", (day, status, code))

    def status(self, day):
        with self.connect() as conn:
            row = conn.execute("SELECT status,code FROM attempts WHERE day=?", (day,)).fetchone()
        return {"status": row[0], "code": row[1]} if row else {"status": "pending", "code": ""}

    def paused(self, day, revision):
        with self.connect() as conn:
            row = conn.execute("SELECT revision,code FROM run_guards WHERE day=?", (day,)).fetchone()
        return row[1] if row and row[0] == revision else None

    def pause(self, day, revision, code):
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO run_guards VALUES (?,?,?)", (day, revision, code))

    def clear_pause(self, day):
        with self.connect() as conn:
            conn.execute("DELETE FROM run_guards WHERE day=?", (day,))

    @contextmanager
    def generation_lease(self, seconds=180):
        owner = uuid.uuid4().hex
        now = datetime.now(timezone.utc).timestamp()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT expires FROM lease WHERE id=1").fetchone()
            if existing and existing[0] > now:
                raise RuntimeError("clerk_busy")
            conn.execute("INSERT OR REPLACE INTO lease VALUES (1,?,?)", (owner, now + seconds))
        try:
            yield owner
        finally:
            with self.connect() as conn:
                conn.execute("DELETE FROM lease WHERE owner=?", (owner,))

    def renew(self, owner, seconds=180):
        with self.connect() as conn:
            now = datetime.now(timezone.utc).timestamp()
            cursor = conn.execute("UPDATE lease SET expires=? WHERE owner=?", (now + seconds, owner))
            if cursor.rowcount != 1:
                raise RuntimeError("clerk_lease_lost")

    def begin_call(self, day, operation, upper_bound):
        call_id = uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO calls VALUES (?,?,?,?,?,?)", (call_id, day, operation, "pending", upper_bound, "{}")
            )
        return call_id

    def finish_call(self, call_id, status, usage=None):
        with self.connect() as conn:
            conn.execute("UPDATE calls SET status=?,usage=? WHERE id=?", (status, json.dumps(usage or {}), call_id))

    def usage(self, day):
        with self.connect() as conn:
            return [
                {"id": r[0], "operation": r[1], "status": r[2], "input_upper_bound": r[3], "reported": json.loads(r[4])}
                for r in conn.execute(
                    "SELECT id,operation,status,input_upper_bound,usage FROM calls WHERE day=? ORDER BY rowid", (day,)
                )
            ]

    def days(self):
        with self.connect() as conn:
            return [
                r[0]
                for r in conn.execute("SELECT day FROM publications UNION SELECT day FROM attempts ORDER BY day DESC")
            ]

    def takeover(self):
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='takeover'").fetchone()
        value = json.loads(row[0]) if row else None
        return value if value and value.get("input_policy") == VISIBLE_TEXT_POLICY else None

    def confirm_takeover(self, day, revision, *, confirmed_by):
        if confirmed_by not in ("知知", "顾凛"):
            raise ValueError("explicit takeover confirmation required")
        with self.connect() as conn:
            published = conn.execute("SELECT revision FROM publications WHERE day=?", (day,)).fetchone()
            if not published or published[0] != revision:
                raise ValueError("current successful publication required")
            seen = conn.execute("SELECT value FROM meta WHERE key='breath_read'").fetchone()
            if not seen or json.loads(seen[0]) != {"day": day, "revision": revision}:
                raise ValueError("current impression must actually fit in breath before takeover")
            value = {
                "day": day,
                "revision": revision,
                "confirmed_by": confirmed_by,
                "input_policy": VISIBLE_TEXT_POLICY,
            }
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('takeover',?)", (json.dumps(value),))

    def mark_breath_read(self, day, revision):
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta VALUES ('breath_read',?)",
                (json.dumps({"day": day, "revision": revision}),),
            )
