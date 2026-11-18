"""Storage protection: budget, ledger of every managed file, quarantine and purge.

Hard rules (enforced in `_guard_delete`, the ONLY place a file is ever unlinked):
  * the exact absolute path must be in the ledger (never a glob, never a recursive delete, never a directory);
  * it must sit inside the quarantine/ or a registered-temp location of this project, with no symlink anywhere in its path;
  * retention class must allow deletion (`render-unconfirmed`, `source-keep`, `permanent` never do);
  * dry-run mode logs what would be deleted and deletes nothing.
Directories are only ever removed with os.rmdir (empty directories under quarantine/), never recursively.
"""
import hashlib
import os
import shutil
import time
from pathlib import Path

from .db import DB

NEVER_DELETE = {"permanent", "render-unconfirmed", "source-keep", "database"}
TEMP_CLASS = "temp"
QUARANTINE = "quarantine"
DAY = 86400.0


class StorageError(Exception):
    pass


def sha256_file(p: Path, block: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(block):
            h.update(chunk)
    return h.hexdigest()


class Storage:
    def __init__(self, db: DB, root: Path, cfg: dict, disk_usage=shutil.disk_usage, clock=time.time, dry_run: bool | None = None):
        self.db, self.root, self.cfg, self.disk_usage, self.clock = db, Path(root).resolve(), cfg, disk_usage, clock
        self.dry_run = cfg["dry_run"] if dry_run is None else dry_run
        self.s = cfg["storage"]
        self.quarantine_root = self.root / QUARANTINE

    # ------------------------------------------------------------------ ledger
    def register(self, path: Path, type: str, source_id: str | None, state: str, retention_class: str, job_id: str | None = None,
                 delete_after: float | None = None, hash_it: bool = True, sha: str | None = None) -> dict:
        p = Path(path)
        if not p.is_absolute():
            p = self.root / p
        if not p.is_file():
            raise StorageError(f"cannot register a missing file: {p}")
        if p.is_symlink():
            raise StorageError(f"refusing to register a symlink: {p}")
        p = p.resolve()
        if not self._inside(p, self.root) and retention_class != "source-keep":
            raise StorageError(f"{p} is outside the project workspace")
        st = p.stat()
        sha = sha or (sha256_file(p) if hash_it else None)
        with self.db.tx() as c:
            c.execute("INSERT INTO files(path,type,source_id,size,sha256,created,state,retention_class,delete_after,job_id) VALUES(?,?,?,?,?,?,?,?,?,?) "
                      "ON CONFLICT(path) DO UPDATE SET type=excluded.type,source_id=excluded.source_id,size=excluded.size,sha256=excluded.sha256,state=excluded.state,"
                      "retention_class=excluded.retention_class,delete_after=excluded.delete_after,job_id=excluded.job_id",
                      (str(p), type, source_id, st.st_size, sha, self.clock(), state, retention_class, delete_after, job_id))
        return self.row(p)

    def row(self, path) -> dict | None:
        r = self.db.q("SELECT * FROM files WHERE path=?", (str(path),))
        return dict(r[0]) if r else None

    def rows(self, **where) -> list[dict]:
        sql, a = "SELECT * FROM files WHERE 1=1", []
        for k, v in where.items():
            sql += f" AND {k}=?"
            a.append(v)
        return [dict(r) for r in self.db.q(sql + " ORDER BY created", a)]

    def register_tree(self, directory: Path, type: str, source_id: str | None, retention_class: str, state: str = "active", job_id: str | None = None,
                      delete_after: float | None = None) -> int:
        """Register every file under `directory` (walk, exact files only)."""
        n = 0
        for dp, _, fns in os.walk(directory):
            for fn in fns:
                f = Path(dp) / fn
                if f.is_file() and not f.is_symlink():
                    self.register(f, type, source_id, state, retention_class, job_id, delete_after, hash_it=retention_class != TEMP_CLASS)
                    n += 1
        return n

    def find_duplicate(self, sha: str, type: str) -> dict | None:
        r = self.db.q("SELECT * FROM files WHERE sha256=? AND type=? AND state NOT IN ('purged') LIMIT 1", (sha, type))
        return dict(r[0]) if r else None

    @staticmethod
    def _inside(p: Path, parent: Path) -> bool:
        try:
            p.relative_to(parent)
            return True
        except ValueError:
            return False

    # ------------------------------------------------------------------ budget
    def managed_bytes(self) -> int:
        r = self.db.q("SELECT COALESCE(SUM(size),0) AS n FROM files WHERE state NOT IN ('purged')")
        return int(r[0]["n"])

    def budget(self) -> dict:
        du = self.disk_usage(self.root)
        total, free = du.total, du.free
        managed = self.managed_bytes()
        max_bytes = self.s["max_fraction_of_disk"] * total
        reserve = self.s["min_free_fraction"] * total
        room_before_reserve = max(0.0, free - reserve) + 0                   # bytes we can still add without cutting into the free-space reserve
        limit = min(max_bytes, managed + room_before_reserve)
        free_frac = free / total
        margin = 0.02
        reasons = []
        if free_frac < self.s["min_free_fraction"] + margin:
            reasons.append(f"free space {free_frac:.1%} is within {margin:.0%} of the {self.s['min_free_fraction']:.0%} reserve")
        if managed >= max_bytes:
            reasons.append(f"managed data {managed / 1e9:.2f} GB reached the {self.s['max_fraction_of_disk']:.0%} budget ({max_bytes / 1e9:.2f} GB)")
        high_water = managed >= self.s["high_water_fraction_of_budget"] * max_bytes or free_frac < self.s["min_free_fraction"]
        return {"total_bytes": total, "free_bytes": free, "free_fraction": round(free_frac, 4), "managed_bytes": managed, "budget_bytes": int(max_bytes),
                "effective_limit_bytes": int(limit), "ingestion_paused": bool(reasons), "pause_reasons": reasons, "high_water": bool(high_water)}

    def ingestion_allowed(self) -> tuple[bool, list[str]]:
        b = self.budget()
        return (not b["ingestion_paused"]), b["pause_reasons"]

    # ------------------------------------------------------------------ guarded deletion
    def _guard_delete(self, path: Path) -> dict:
        p = Path(path)
        if not p.is_absolute():
            raise StorageError(f"deletion needs an exact absolute path, got {path!r}")
        row = self.row(p)
        if row is None:
            raise StorageError(f"{p} is not in the storage ledger: refusing to delete an untracked path")
        if row["retention_class"] in NEVER_DELETE:
            raise StorageError(f"{p} has retention class {row['retention_class']!r}, which is never deleted")
        if row["state"] == "purged":
            return row
        if row["state"] not in (QUARANTINE, TEMP_CLASS):
            raise StorageError(f"{p} is in state {row['state']!r}; only quarantined or temp files can be purged")
        if os.path.realpath(p) != str(p) or p.is_symlink():
            raise StorageError(f"{p} contains a symlink: refusing")
        if p.exists() and not p.is_file():
            raise StorageError(f"{p} is not a regular file: directories are never deleted recursively")
        if row["state"] == QUARANTINE and not self._inside(p, self.quarantine_root):
            raise StorageError(f"{p} is marked quarantined but is outside {self.quarantine_root}")
        if row["state"] == TEMP_CLASS and not (self._inside(p, self.root / "runs") or self._inside(p, self.root / "work") or self._inside(p, self.quarantine_root)):
            raise StorageError(f"{p} is a temp artifact outside runs/ or work/")
        return row

    def purge(self, path: Path) -> bool:
        """Delete ONE ledger file. Idempotent: a file that is already gone is just marked purged. Returns True if bytes were (or, dry-run, would be) freed."""
        p = Path(path)
        row = self._guard_delete(p)
        if row["state"] == "purged":
            return False
        if self.dry_run:
            self.db.log("INFO", f"[dry-run] would purge {p} ({row['size']} bytes)", row.get("job_id"))
            return True
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass
        with self.db.tx() as c:
            c.execute("UPDATE files SET state='purged', size=0 WHERE path=?", (str(p),))
        self.db.log("INFO", f"purged {p} ({row['size']} bytes, class {row['retention_class']})", row.get("job_id"))
        self._rmdir_empty(p.parent)
        return True

    def _rmdir_empty(self, d: Path) -> None:
        """Remove now-empty directories under quarantine/ (os.rmdir only: it fails on a non-empty dir, so nothing recursive can happen)."""
        while d != self.quarantine_root and self._inside(d, self.quarantine_root):
            try:
                os.rmdir(d)
            except OSError:
                break
            d = d.parent

    # ------------------------------------------------------------------ quarantine
    def quarantine(self, path: Path, subdir: str, purge_after_days: float, job_id: str | None = None) -> Path:
        """Move one ledger file into quarantine/<subdir>/ and schedule its purge. The move is an atomic rename on the same volume."""
        p = Path(path).resolve()
        row = self.row(p)
        if row is None:
            raise StorageError(f"{p} is not in the ledger; cannot quarantine an untracked file")
        if row["retention_class"] in ("permanent", "database"):
            raise StorageError(f"{p} is permanent")
        dest_dir = self.quarantine_root / subdir
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / p.name
        n = 1
        while dest.exists():
            dest = dest_dir / f"{p.stem}.{n}{p.suffix}"
            n += 1
        if self.dry_run:
            self.db.log("INFO", f"[dry-run] would quarantine {p} -> {dest}", job_id)
            return p
        os.replace(p, dest)
        with self.db.tx() as c:
            c.execute("DELETE FROM files WHERE path=?", (str(p),))
            c.execute("INSERT INTO files(path,type,source_id,size,sha256,created,state,retention_class,delete_after,job_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (str(dest), row["type"], row["source_id"], row["size"], row["sha256"], self.clock(), QUARANTINE, "quarantine-media",
                       self.clock() + purge_after_days * DAY, job_id or row.get("job_id")))
        self.db.log("INFO", f"quarantined {p.name} -> {dest} (purge after {purge_after_days} days)", job_id)
        return dest

    def restore(self, path: Path, to: Path) -> Path:
        """Recovery: move a quarantined file back (before it is purged)."""
        p = Path(path).resolve()
        row = self.row(p)
        if row is None or row["state"] != QUARANTINE or not p.is_file():
            raise StorageError(f"{p} is not a restorable quarantined file")
        to = Path(to).resolve()
        if not self._inside(to, self.root):
            raise StorageError("restore target must be inside the project workspace")
        to.parent.mkdir(parents=True, exist_ok=True)
        os.replace(p, to)
        with self.db.tx() as c:
            c.execute("DELETE FROM files WHERE path=?", (str(p),))
            c.execute("INSERT INTO files(path,type,source_id,size,sha256,created,state,retention_class,delete_after,job_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (str(to), row["type"], row["source_id"], row["size"], row["sha256"], self.clock(), "active", "render-recovery", None, row["job_id"]))
        self._rmdir_empty(p.parent)
        return to

    # ------------------------------------------------------------------ sweeps
    def purge_due(self) -> list[str]:
        now, done = self.clock(), []
        for r in self.db.q("SELECT path FROM files WHERE state=? AND delete_after IS NOT NULL AND delete_after<=? ORDER BY delete_after", (QUARANTINE, now)):
            if self.purge(Path(r["path"])):
                done.append(r["path"])
        return done

    def sweep_temp(self, ttl_hours: float | None = None, force: bool = False) -> list[str]:
        ttl = (self.s["temp_ttl_hours"] if ttl_hours is None else ttl_hours) * 3600
        now, done = self.clock(), []
        for r in self.db.q("SELECT path,created FROM files WHERE state=? ORDER BY created", (TEMP_CLASS,)):
            if force or now - r["created"] >= ttl:
                if self.purge(Path(r["path"])):
                    done.append(r["path"])
        return done

    def emergency_cleanup(self) -> dict:
        """At the high-water mark: temp artifacts first, then quarantined media oldest first (earlier than the 7-day window). Never touches
        unconfirmed renders, sources still needed, permanent records or anything outside the ledger."""
        freed = {"temp": [], "quarantine": []}
        if not self.budget()["high_water"]:
            return freed
        freed["temp"] = self.sweep_temp(force=True)
        for r in self.db.q("SELECT path FROM files WHERE state=? ORDER BY COALESCE(delete_after, created) ASC, created ASC", (QUARANTINE,)):
            if not self.budget()["high_water"]:
                break
            if self.purge(Path(r["path"])):
                freed["quarantine"].append(r["path"])
        self.db.log("WARN", f"emergency cleanup freed {len(freed['temp'])} temp + {len(freed['quarantine'])} quarantined files")
        return freed
