"""SQLite state store (WAL). Every transition is one IMMEDIATE transaction: compare-and-swap on the expected current state + history row, so a
crash leaves either the old or the new state, never a half-written one. Also: leases (queue locking), the storage ledger, metrics, strategy."""
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

STATES = ["INGESTED", "ANALYZED", "CANDIDATES_SELECTED", "EDITED", "QA_PASSED", "QUEUED", "UPLOADED", "PUBLISH_CONFIRMED", "METRICS_COLLECTED", "LEARNED",
          "QUARANTINED", "PURGED"]
SIDE = ["REJECTED", "FAILED", "UPLOAD_UNCERTAIN", "BLOCKED_RIGHTS", "DUPLICATE_SOURCE", "SOURCE_DONE"]
ALLOWED = {
    "INGESTED": {"ANALYZED", "BLOCKED_RIGHTS", "FAILED", "DUPLICATE_SOURCE"},
    "ANALYZED": {"CANDIDATES_SELECTED", "BLOCKED_RIGHTS", "FAILED"},
    "CANDIDATES_SELECTED": {"EDITED", "SOURCE_DONE", "FAILED"},
    "EDITED": {"QA_PASSED", "REJECTED", "FAILED"},
    "QA_PASSED": {"QUEUED", "REJECTED"},
    "QUEUED": {"UPLOADED", "UPLOAD_UNCERTAIN", "FAILED", "REJECTED"},
    "UPLOADED": {"PUBLISH_CONFIRMED", "FAILED"},
    "PUBLISH_CONFIRMED": {"METRICS_COLLECTED"},
    "METRICS_COLLECTED": {"LEARNED"},
    "LEARNED": {"QUARANTINED"},
    "QUARANTINED": {"PURGED"},
    "REJECTED": {"QUARANTINED"},
    "FAILED": {"QUARANTINED", "INGESTED", "QUEUED"},
    "UPLOAD_UNCERTAIN": {"QUARANTINED", "UPLOADED", "QUEUED"},
    "BLOCKED_RIGHTS": {"INGESTED", "QUARANTINED"},
    "DUPLICATE_SOURCE": {"QUARANTINED"},
    "SOURCE_DONE": {"QUARANTINED"},
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, source_id TEXT, state TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
  idempotency_key TEXT UNIQUE, attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,
  lock_owner TEXT, lock_until REAL NOT NULL DEFAULT 0, publish_id TEXT, post_id TEXT, upload_phase TEXT NOT NULL DEFAULT 'NONE',
  scheduled_at REAL, confirmed_at REAL, created REAL NOT NULL, updated REAL NOT NULL, last_error TEXT);
CREATE TABLE IF NOT EXISTS transitions(id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL, at REAL NOT NULL, note TEXT);
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, type TEXT NOT NULL, source_id TEXT, size INTEGER NOT NULL, sha256 TEXT, created REAL NOT NULL,
  state TEXT NOT NULL, retention_class TEXT NOT NULL, delete_after REAL, job_id TEXT);
CREATE TABLE IF NOT EXISTS metrics(job_id TEXT NOT NULL, age_label TEXT NOT NULL, taken_at REAL NOT NULL, views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER,
  unavailable TEXT NOT NULL DEFAULT '[]', PRIMARY KEY(job_id, age_label));
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, level TEXT NOT NULL, job_id TEXT, message TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, kind);
CREATE INDEX IF NOT EXISTS files_state ON files(state, delete_after);
"""


class StateError(Exception):
    pass


class DB:
    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.owner = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
        c = self._conn()
        c.executescript(SCHEMA)
        c.close()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=FULL")
        return c

    @contextmanager
    def tx(self):
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        finally:
            c.close()

    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        c = self._conn()
        try:
            return c.execute(sql, args).fetchall()
        finally:
            c.close()

    # ------------------------------------------------------------------ jobs
    def add_job(self, job_id: str, kind: str, state: str, source_id: str | None = None, payload: dict | None = None, key: str | None = None, note: str = "created") -> bool:
        """Idempotent: returns False (and changes nothing) when the id or idempotency key already exists."""
        now = self.clock()
        with self.tx() as c:
            if c.execute("SELECT 1 FROM jobs WHERE id=? OR (idempotency_key IS NOT NULL AND idempotency_key=?)", (job_id, key)).fetchone():
                return False
            c.execute("INSERT INTO jobs(id,kind,source_id,state,payload,idempotency_key,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                      (job_id, kind, source_id, state, json.dumps(payload or {}), key, now, now))
            c.execute("INSERT INTO transitions(job_id,from_state,to_state,at,note) VALUES(?,?,?,?,?)", (job_id, None, state, now, note))
        return True

    def get(self, job_id: str) -> dict | None:
        r = self.q("SELECT * FROM jobs WHERE id=?", (job_id,))
        return self._job(r[0]) if r else None

    @staticmethod
    def _job(r) -> dict:
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        return d

    def jobs(self, state: str | None = None, kind: str | None = None, source_id: str | None = None) -> list[dict]:
        sql, a = "SELECT * FROM jobs WHERE 1=1", []
        for col, v in (("state", state), ("kind", kind), ("source_id", source_id)):
            if v is not None:
                sql += f" AND {col}=?"
                a.append(v)
        return [self._job(r) for r in self.q(sql + " ORDER BY created", a)]

    def transition(self, job_id: str, expect: str, to: str, note: str = "", **fields) -> dict:
        """Atomic compare-and-swap. `fields` may set payload (merged), publish_id, post_id, scheduled_at, confirmed_at, upload_phase, last_error, attempts, next_attempt_at."""
        if to not in ALLOWED.get(expect, set()) and to != expect:
            raise StateError(f"illegal transition {expect} -> {to} for {job_id}")
        now = self.clock()
        with self.tx() as c:
            r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if r is None:
                raise StateError(f"unknown job {job_id}")
            if r["state"] != expect:
                raise StateError(f"{job_id} is {r['state']}, expected {expect} (another worker or a restart already moved it)")
            sets, args = ["state=?", "updated=?"], [to, now]
            for k, v in fields.items():
                if k == "payload":
                    merged = {**json.loads(r["payload"]), **v}
                    sets.append("payload=?")
                    args.append(json.dumps(merged))
                elif k in ("publish_id", "post_id", "scheduled_at", "confirmed_at", "upload_phase", "last_error", "attempts", "next_attempt_at"):
                    sets.append(f"{k}=?")
                    args.append(v)
                else:
                    raise StateError(f"cannot set {k}")
            c.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", (*args, job_id))
            c.execute("INSERT INTO transitions(job_id,from_state,to_state,at,note) VALUES(?,?,?,?,?)", (job_id, expect, to, now, note))
        return self.get(job_id)

    def update(self, job_id: str, **fields) -> None:
        """Non-state updates (attempts, backoff, phase). Same atomicity."""
        with self.tx() as c:
            sets, args = ["updated=?"], [self.clock()]
            for k, v in fields.items():
                if k == "payload":
                    r = c.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
                    sets.append("payload=?")
                    args.append(json.dumps({**json.loads(r["payload"]), **v}))
                else:
                    sets.append(f"{k}=?")
                    args.append(v)
            c.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", (*args, job_id))

    def history(self, job_id: str) -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM transitions WHERE job_id=? ORDER BY id", (job_id,))]

    # ------------------------------------------------------------------ leases (queue locking)
    def lease(self, job_id: str, seconds: float = 600) -> bool:
        now = self.clock()
        with self.tx() as c:
            r = c.execute("SELECT lock_owner, lock_until FROM jobs WHERE id=?", (job_id,)).fetchone()
            if r is None or (r["lock_owner"] not in (None, self.owner) and r["lock_until"] > now):
                return False
            c.execute("UPDATE jobs SET lock_owner=?, lock_until=? WHERE id=?", (self.owner, now + seconds, job_id))
        return True

    def release(self, job_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE jobs SET lock_owner=NULL, lock_until=0 WHERE id=? AND lock_owner=?", (job_id, self.owner))

    # ------------------------------------------------------------------ kv / events
    def kv_get(self, key: str, default=None):
        r = self.q("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(r[0]["value"]) if r else default

    def kv_set(self, key: str, value) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def log(self, level: str, message: str, job_id: str | None = None) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO events(at,level,job_id,message) VALUES(?,?,?,?)", (self.clock(), level, job_id, message))
