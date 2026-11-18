"""The zero-touch service: one `tick()` advances every job one step; `run_forever()` repeats it. Each step is isolated (one failing video or step never
stops the others), every state change is an atomic compare-and-swap in SQLite, and uploads use an intent-first protocol so a crash can never double-post.

Lifecycle (video jobs): EDITED -> QA_PASSED -> QUEUED -> UPLOADED -> PUBLISH_CONFIRMED -> METRICS_COLLECTED -> LEARNED -> QUARANTINED -> PURGED
(side states: REJECTED, FAILED, UPLOAD_UNCERTAIN). Source jobs: INGESTED -> ANALYZED -> CANDIDATES_SELECTED -> SOURCE_DONE (-> QUARANTINED -> PURGED).
"""
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config as project_config
from . import config as svc_config
from . import gate as gate_mod
from . import learning, postpackage
from .db import DB, StateError
from .publishers import (DELIVERED, FAILED, PUBLISHED, AuthExpired, LocalQueuePublisher, PublisherError, SimulatedPublisher)
from .scheduler import Scheduler
from .storage import Storage, StorageError

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
HOUR = 3600.0
DAY = 86400.0


class DryRunPublisher(SimulatedPublisher):
    """Dry run: nothing leaves the machine. Uploads 'succeed' locally but are never confirmed as published."""
    name = "dry-run"

    def status(self, publish_id):
        return {"status": DELIVERED, "post_id": None, "fail_reason": None, "retryable": False}


def default_runner(root: Path, video: Path, stage: str, content_type: str = "auto", rights: str | None = None) -> dict:
    env = {**os.environ, "CLIPAGENT_ROOT": str(root), "PYTHONPATH": os.pathsep.join([str(Path(__file__).resolve().parents[2]), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)}
    cmd = [sys.executable, "-m", "clipagent.cli", "make-clips", "--source", str(video), "--content-type", content_type, "--until", stage]
    if rights:
        cmd += ["--rights", rights]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=8 * 3600)
    return {"returncode": r.returncode, "stdout": r.stdout[-2000:], "stderr": "\n".join(l for l in r.stderr.splitlines() if "objc" not in l)[-1500:]}


class Service:
    def __init__(self, root=None, cfg: dict | None = None, publisher=None, clock=time.time, disk_usage=None, runner=None, token_backend=None, tokens=None):
        self.root = Path(root or project_config.project_root()).resolve()
        self.cfg = cfg or svc_config.load(self.root)
        self.clock = clock
        self.dir = self.root / "service"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db = DB(self.dir / "state.db", clock)
        import shutil
        self.storage = Storage(self.db, self.root, self.cfg, disk_usage or shutil.disk_usage, clock)
        override = self.db.kv_get("mode_override")
        self.cfg_mode = {**self.cfg, "mode": override} if override else self.cfg
        self.mode, self.mode_note = svc_config.effective_mode(self.cfg_mode)
        self.scheduler = Scheduler(self.db, self.cfg, clock, self.mode)
        self.runner = runner or (lambda video, stage, ct="auto", rights=None: default_runner(self.root, video, stage, ct, rights))
        self.tokens = tokens
        self.publisher = publisher or self._make_publisher(token_backend)
        self.tick_no = 0
        self.tz = ZoneInfo(self.cfg["timezone"])
        self.logf = self.root / "logs" / "service.log"
        self.logf.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ plumbing
    def _make_publisher(self, token_backend):
        if self.cfg["dry_run"]:
            return DryRunPublisher(mode=self.mode)
        from .publishers import TikTokOfficialPublisher
        from .tokens import TokenStore
        self.tokens = self.tokens or TokenStore(self.cfg, token_backend)
        if self.tokens.load() is None:
            return LocalQueuePublisher(self.root)              # no authorization yet: the compliant fallback is the local ready-to-post queue
        return TikTokOfficialPublisher(self.cfg, self.tokens, self.mode)

    def log(self, level: str, msg: str, job_id: str | None = None, **kv) -> None:
        self.db.log(level, msg, job_id)
        with self.logf.open("a") as f:
            f.write(json.dumps({"ts": datetime.fromtimestamp(self.clock(), self.tz).isoformat(timespec="seconds"), "level": level, "job": job_id, "msg": msg, **kv}) + "\n")

    def step(self, name: str, fn) -> None:
        try:
            fn()
        except KeyboardInterrupt:
            raise
        except Exception as exc:                                    # failure isolation: record and carry on with the remaining steps
            self.log("ERROR", f"step {name} failed: {type(exc).__name__}: {exc}")

    def stopped(self) -> bool:
        return (self.root / self.cfg["emergency_stop_file"]).exists()

    # ------------------------------------------------------------------ the tick
    def tick(self) -> dict:
        self.tick_no += 1
        if self.stopped():
            self.heartbeat("STOPPED")
            return {"state": "STOPPED"}
        self.step("recover", self.recover)
        self.step("storage", self.storage_step)
        self.step("ingest", self.ingest_step)
        self.step("process", self.process_step)
        self.step("schedule", self.schedule_step)
        self.step("upload", self.upload_step)
        self.step("status", self.status_step)
        self.step("metrics", self.metrics_step)
        self.step("retention", self.retention_step)
        self.step("summary", self.summary_step)
        self.heartbeat("RUNNING")
        return {"state": "RUNNING", "counts": self.counts()}

    def counts(self) -> dict:
        out: dict[str, int] = {}
        for r in self.db.q("SELECT kind, state, COUNT(*) AS n FROM jobs GROUP BY kind, state"):
            out[f"{r['kind']}:{r['state']}"] = r["n"]
        return out

    def heartbeat(self, state: str) -> None:
        b = self.storage.budget()
        data = {"ts": self.clock(), "pid": os.getpid(), "tick": self.tick_no, "state": state, "mode": self.mode, "mode_note": self.mode_note, "dry_run": self.cfg["dry_run"],
                "ingestion_paused": b["ingestion_paused"], "free_fraction": b["free_fraction"], "managed_gb": round(b["managed_bytes"] / 1e9, 3),
                "auth_blocked": bool(self.db.kv_get("auth_blocked", False))}
        tmp = self.dir / "health.json.tmp"
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self.dir / "health.json")

    # ------------------------------------------------------------------ crash recovery
    def recover(self) -> None:
        """Resolve anything a crash left half-done. An upload whose publish id was never recorded is looked up; if TikTok cannot tell us, the job
        goes to UPLOAD_UNCERTAIN (never re-sent automatically: no duplicate post)."""
        for j in self.db.jobs(state="QUEUED", kind="video"):
            if j["upload_phase"] == "INTENT" and not j["publish_id"]:
                pid = self.publisher.find_existing(j["idempotency_key"])
                if pid:
                    self.db.transition(j["id"], "QUEUED", "UPLOADED", "recovered after crash: upload had reached TikTok", publish_id=pid, upload_phase="SENT")
                    self.log("WARN", f"recovered upload {pid} after crash", j["id"])
                else:
                    self.db.transition(j["id"], "QUEUED", "UPLOAD_UNCERTAIN", "crash during upload; cannot confirm whether TikTok received it", upload_phase="INTENT")
                    self.log("WARN", "upload outcome unknown after crash: NOT re-sending (possible duplicate)", j["id"])
            if j["lock_owner"] and j["lock_until"] < self.clock():
                self.db.release(j["id"])

    # ------------------------------------------------------------------ storage
    def storage_step(self) -> None:
        b = self.storage.budget()
        if b["high_water"]:
            self.storage.emergency_cleanup()
        self.storage.purge_due()
        self.storage.sweep_temp()

    # ------------------------------------------------------------------ ingest
    def ingest_step(self) -> None:
        ok, why = self.storage.ingestion_allowed()
        if not ok:
            if self.db.kv_get("ingest_pause_logged") != why:
                self.log("WARN", "ingestion PAUSED: " + "; ".join(why))
                self.db.kv_set("ingest_pause_logged", why)
            return
        self.db.kv_set("ingest_pause_logged", None)
        inbox = self.root / self.cfg["inbox"]
        inbox.mkdir(parents=True, exist_ok=True)
        from ..clips import source as clip_source
        files = [f for f in sorted(inbox.iterdir()) if f.is_file() and not f.is_symlink() and not f.name.startswith(".")]
        for v, audio in clip_source._pair(files):
            parts = [v] + ([audio] if audio is not None else [])
            if any(self.db.kv_get(f"consumed:{p}") for p in parts):
                continue
            if any((self.storage.row(p.resolve()) or {}).get("state") not in (None, "purged") for p in parts):
                continue                                             # already ingested (the source stays in place until its videos are confirmed)
            if not all(self._stable(p) for p in parts):
                continue
            if audio is None:
                self.ingest_file(v)
            else:                                                    # a video with its separate audio: combine first (originals untouched)
                try:
                    muxed = clip_source.mux_with_audio(v, audio, self.root)
                except Exception as exc:
                    self.log("WARN", f"cannot combine {v.name} with {audio.name}: {exc}")
                    continue
                self.ingest_file(muxed, originals=parts)
                for p in parts:
                    self.db.kv_set(f"consumed:{p}", True)

    def _stable(self, f: Path) -> bool:
        """True once the file has stopped changing for `stable_seconds` (service clock), so a file still being copied is never ingested."""
        now = self.clock()
        st = f.stat()
        sig = [st.st_size, int(st.st_mtime_ns)]
        key = f"seen:{f}"
        seen = self.db.kv_get(key)
        if not seen or seen[:2] != sig:
            self.db.kv_set(key, [*sig, now])
            return False
        return now - seen[2] >= self.cfg["stable_seconds"]

    def ingest_file(self, f: Path, originals: list[Path] | None = None) -> None:
        sha = hashlib.sha256()
        with open(f, "rb") as fh:
            while chunk := fh.read(1 << 22):
                sha.update(chunk)
        digest = sha.hexdigest()
        sid = digest[:12]
        dup = self.storage.find_duplicate(digest, "source")
        if dup and dup["path"] == str(f.resolve()):
            return
        sidecar = {}
        ref = originals[0] if originals else f                          # sidecars belong to the file the user dropped, not to the combined copy
        mp = ref.with_name(ref.name + ".meta.json")
        if mp.is_file():
            try:
                sidecar = json.loads(mp.read_text())
            except ValueError:
                self.log("WARN", f"ignoring unreadable metadata sidecar {mp.name}")
        if dup:
            jid = f"src-{sid}-dup-{int(self.clock())}"
            self.db.add_job(jid, "source", "INGESTED", sid, {"path": str(f), "name": f.name, "note": "duplicate of " + dup["path"]}, note="duplicate source")
            self.db.transition(jid, "INGESTED", "DUPLICATE_SOURCE", "identical bytes already ingested: not processed twice")
            self.storage.register(f, "source", sid, "active", "duplicate-source", jid, sha=digest)
            self.storage.quarantine(f, "duplicates", self.cfg["storage"]["recovery_days"], jid)       # deduplicated: one copy kept, the duplicate recoverable for 7 days
            self.log("INFO", f"duplicate source {f.name} (same bytes as {dup['path']}) quarantined", jid)
            return
        rights_file = ref.with_suffix(".rights.txt")
        rights = rights_file.read_text().strip().splitlines()[0] if rights_file.is_file() and rights_file.read_text().strip() else None
        row = self.storage.register(f, "source", sid, "active", "source-keep", f"src-{sid}", None, sha=digest)
        self.db.kv_set(f"hash:{f}", digest)
        created = self.db.add_job(f"src-{sid}", "source", "INGESTED", sid, {"path": str(f), "name": f.name, "sha256": digest, "meta": sidecar, "rights": rights, "size": row["size"]},
                                  key=f"source:{digest}")
        if created:
            self.log("INFO", f"ingested {f.name} as {sid}", f"src-{sid}")

    # ------------------------------------------------------------------ process (analysis -> edit -> gate)
    def _backoff(self, attempts: int) -> float:
        r = self.cfg["retry"]
        return self.clock() + min(r["base_seconds"] * 2 ** max(attempts - 1, 0), r["cap_seconds"])

    def process_step(self) -> None:
        for j in self.db.jobs(kind="source"):
            if j["state"] not in ("INGESTED", "ANALYZED", "CANDIDATES_SELECTED") or j["next_attempt_at"] > self.clock():
                continue
            if not self.db.lease(j["id"], 8 * HOUR):
                continue
            try:
                self.process_source(j)
            except Exception as exc:                                  # one failing source never stops the rest of the queue
                self.fail_attempt(j, exc)
            finally:
                self.db.release(j["id"])

    def fail_attempt(self, j: dict, exc: Exception) -> None:
        cur = self.db.get(j["id"])
        n = cur["attempts"] + 1
        if n >= self.cfg["retry"]["max_attempts"]:
            self.db.transition(j["id"], cur["state"], "FAILED", f"gave up after {n} attempts: {exc}", attempts=n, last_error=str(exc)[:500])
            self.log("ERROR", f"{j['id']} FAILED permanently after {n} attempts: {exc}", j["id"])
        else:
            self.db.update(j["id"], attempts=n, next_attempt_at=self._backoff(n), last_error=str(exc)[:500])
            self.log("WARN", f"{j['id']} attempt {n} failed ({exc}); retry with backoff", j["id"])

    def process_source(self, j: dict) -> None:
        p = j["payload"]
        video = Path(p["path"])
        ct = (p.get("meta") or {}).get("content_type", "auto")
        stage = {"INGESTED": "analysis", "ANALYZED": "passes", "CANDIDATES_SELECTED": "qa"}[j["state"]]
        res = self.runner(video, stage, ct, p.get("rights"))
        if res["returncode"] == 2:
            self.db.transition(j["id"], j["state"], "BLOCKED_RIGHTS", "no resolvable rights status", last_error=res["stdout"][-300:])
            self.log("WARN", f"{video.name}: rights status missing for a copyrighted-type source; nothing made", j["id"])
            return
        if res["returncode"] != 0:
            raise RuntimeError(f"make-clips --until {stage} exited {res['returncode']}: {res['stderr'][-300:]}")
        if stage == "analysis":
            self.db.transition(j["id"], "INGESTED", "ANALYZED", "whole source analysed")
        elif stage == "passes":
            self.db.transition(j["id"], "ANALYZED", "CANDIDATES_SELECTED", "candidates selected, passes planned")
        else:
            self.collect_videos(j)
            self.db.transition(j["id"], "CANDIDATES_SELECTED", "SOURCE_DONE", "all planned videos edited and gated")

    def collect_videos(self, j: dict) -> None:
        sid = j["source_id"]
        rundir = self.root / "runs" / sid
        mans = sorted((rundir / "manifests").glob("*.json")) if (rundir / "manifests").is_dir() else []
        posted = [m for m in self._posted_manifests()]
        for mp in mans:
            man = json.loads(mp.read_text())
            mp4 = self.root / man["location"] / man["output"]
            render_sha = man["qa"].get("sha256") or ""
            vid = f"vid-{sid}-{man['project_id']}"
            key = hashlib.sha256(f"{man['source_sha256']}:{man['project_id']}:{render_sha}".encode()).hexdigest()
            if not self.db.add_job(vid, "video", "EDITED", sid, {"manifest": str(mp), "mp4": str(mp4), "format": man["format"], "content_type": man["content_type"]}, key=key,
                                   note="rendered"):
                continue
            if mp4.is_file():
                self.storage.register(mp4, "render", sid, "active", "render-unconfirmed", vid)
            for extra in ("manifest.json", "qa.json", "caption.txt", "cover.jpg"):
                ep = mp4.parent / extra
                if ep.is_file():
                    self.storage.register(ep, "record", sid, "active", "permanent", vid, hash_it=False)
            if man["status"] != "ready_for_draft_transfer":
                self.reject(vid, "EDITED", "clip QA: " + "; ".join(man["qa"]["reasons"])[:400])
                continue
            g = gate_mod.run(man, mp4, self.cfg, posted)
            self.db.update(vid, payload={"gate": g, "editorial_score": g["score"]})
            if g["ok"]:
                self.db.transition(vid, "EDITED", "QA_PASSED", f"auto-post gate passed (editorial score {g['score']})")
                self.log("INFO", f"{vid} passed the auto-post gate", vid)
            else:
                self.reject(vid, "EDITED", "gate: " + "; ".join(g["reasons"])[:600])
        self.sweep_after_qa(sid)

    def reject(self, vid: str, expect: str, reason: str) -> None:
        self.db.transition(vid, expect, "REJECTED", reason, last_error=reason)
        self.log("INFO", f"REJECTED {vid}: {reason}", vid)
        j = self.db.get(vid)
        mp4 = Path(j["payload"]["mp4"])
        if mp4.is_file() and self.storage.row(mp4):
            with self.db.tx() as c:
                c.execute("UPDATE files SET retention_class='render-rejected', state='active' WHERE path=?", (str(mp4.resolve()),))
            dest = self.storage.quarantine(mp4, "rejected", self.cfg["storage"]["rejected_retention_days"], vid)
            if dest != mp4:
                self.db.transition(vid, "REJECTED", "QUARANTINED", "rejected render quarantined", payload={"mp4": str(dest)})

    def sweep_after_qa(self, sid: str) -> None:
        """Temporary artifacts that are no longer needed once QA is done: review packs, contact sheets, decode caches, working files."""
        rundir = self.root / "runs" / sid
        for sub in ("candidates/review_pack", "work", "qa"):
            d = rundir / sub
            if d.is_dir():
                for dp, _, fns in os.walk(d):
                    for fn in fns:
                        fp = Path(dp) / fn
                        if fp.suffix == ".json" and sub == "qa":
                            continue                                       # QA records are permanent
                        if fp.is_file() and not fp.is_symlink():
                            self.storage.register(fp, "temp", sid, "temp", "temp", hash_it=False)
        for sub in ("transcripts",):
            pass
        self.storage.sweep_temp(force=True)

    def _posted_manifests(self) -> list[dict]:
        out = []
        for j in self.db.jobs(kind="video"):
            if j["state"] in ("UPLOADED", "PUBLISH_CONFIRMED", "METRICS_COLLECTED", "LEARNED", "QUARANTINED", "PURGED", "QUEUED", "QA_PASSED") and "manifest" in j["payload"]:
                try:
                    out.append(json.loads(Path(j["payload"]["manifest"]).read_text()))
                except Exception:
                    pass
        return out

    # ------------------------------------------------------------------ schedule
    def schedule_step(self) -> None:
        for j in self.db.jobs(state="QA_PASSED", kind="video"):
            man = json.loads(Path(j["payload"]["manifest"]).read_text())
            src = self.db.get(f"src-{j['source_id']}") or {"payload": {}}
            arms_preview = self.scheduler.arm_for(j["id"])
            pkg = postpackage.build(man, self.cfg, (src["payload"].get("meta") or {}), arms_preview["caption_style"])
            ts, arms = self.scheduler.schedule(j["id"])
            self.db.transition(j["id"], "QA_PASSED", "QUEUED", f"scheduled {datetime.fromtimestamp(ts, self.tz):%Y-%m-%d %H:%M %Z} (slot {arms['slot']}{', explore' if arms['explore'] else ''})",
                               scheduled_at=ts, payload={"package": pkg, "arms": arms})

    # ------------------------------------------------------------------ upload
    def upload_step(self) -> None:
        ok, why = self.scheduler.can_post_now()
        if not ok:
            return
        if self.db.kv_get("auth_blocked", False):
            r = self.publisher.check_ready()
            if not r["ok"]:
                return
            self.db.kv_set("auth_blocked", False)
        for j in self.db.jobs(state="QUEUED", kind="video"):
            if j["scheduled_at"] and j["scheduled_at"] > self.clock() or j["next_attempt_at"] > self.clock():
                continue
            if j["upload_phase"] == "INTENT":
                continue                                              # outcome unknown: handled by recover()
            if not self.db.lease(j["id"], 1800):
                continue
            try:
                self.upload_one(j)
            finally:
                self.db.release(j["id"])
            if not self.scheduler.can_post_now()[0] or self.db.kv_get("auth_blocked", False):
                break

    def upload_one(self, j: dict) -> None:
        mp4 = Path(j["payload"]["mp4"])
        if not mp4.is_file():
            self.db.transition(j["id"], "QUEUED", "FAILED", "render file missing", last_error="render missing")
            self.log("ERROR", "render missing at upload time", j["id"])
            return
        self.db.update(j["id"], upload_phase="INTENT")                   # (1) record the intent BEFORE anything can reach TikTok
        try:
            pid = self.publisher.upload(mp4, j["payload"]["package"], j["idempotency_key"])
        except PublisherError as e:
            self.db.update(j["id"], upload_phase="NONE")                  # the API told us it did not take the video: safe to retry
            self.handle_publish_error(j, e)
            return
        self.db.transition(j["id"], "QUEUED", "UPLOADED", f"uploaded via {self.publisher.name}; publish id {pid}", publish_id=pid, upload_phase="SENT")   # (2) commit
        self.scheduler.record_success()
        self.log("INFO", f"uploaded {j['id']} ({self.publisher.name}) publish_id={pid}", j["id"])

    def handle_publish_error(self, j: dict, e: PublisherError) -> None:
        self.log("WARN", f"publish error {e.kind}: {e}", j["id"])
        if e.kind == "auth":
            self.db.kv_set("auth_blocked", True)
            self.scheduler.record_error("auth")
            self.log("ERROR", "TikTok authorization expired: posting paused until `clipagent service auth` is run (everything else continues)")
            return
        if e.kind == "daily_cap":
            self.scheduler.record_error("daily_cap")
            ts, _ = self.scheduler.schedule(j["id"])
            self.db.update(j["id"], scheduled_at=ts)
            return
        if e.kind == "unaudited" and self.mode == "COMPLIANT_DIRECT_POST":
            self.db.kv_set("mode_override", "PRIVATE_OR_DRAFT_QUEUE")
            self.log("ERROR", "TikTok says this client is unaudited: Direct Post disabled, using the draft/inbox queue from the next tick")
            return
        cur = self.db.get(j["id"])
        n = cur["attempts"] + 1
        if e.kind in ("rate_limit", "transient") and n < self.cfg["retry"]["max_attempts"]:
            self.scheduler.record_error(e.kind)
            self.db.update(j["id"], attempts=n, next_attempt_at=self._backoff(n), last_error=str(e)[:300])
            return
        self.db.transition(j["id"], "QUEUED", "FAILED", f"{e.kind}: {e}", attempts=n, last_error=str(e)[:300])

    # ------------------------------------------------------------------ status / confirmation
    def status_step(self) -> None:
        if self.db.kv_get("auth_blocked", False):
            return
        for j in self.db.jobs(state="UPLOADED", kind="video"):
            if self.clock() - j["updated"] < 60 and j["payload"].get("last_status_at", 0) > self.clock() - 60:
                continue
            try:
                st = self.publisher.status(j["publish_id"])
            except AuthExpired as e:
                self.handle_publish_error(j, e)
                return
            except PublisherError as e:
                self.log("WARN", f"status check failed: {e}", j["id"])
                continue
            self.db.update(j["id"], payload={"last_status_at": self.clock(), "upload_status": st["status"]})
            if st["status"] == FAILED:
                if st["retryable"]:
                    self.db.transition(j["id"], "UPLOADED", "QUEUED", f"TikTok processing failed ({st['fail_reason']}); retry", upload_phase="NONE", publish_id=None)
                else:
                    self.db.transition(j["id"], "UPLOADED", "FAILED", f"TikTok rejected the video: {st['fail_reason']}", last_error=str(st["fail_reason"]))
            elif st["status"] == PUBLISHED:
                self.confirm(j, st)

    def confirm(self, j: dict, st: dict) -> None:
        """Only a verified post counts: PUBLISH_COMPLETE, a post id, owned by the authorized account, visibility consistent with the configured mode."""
        pkg = j["payload"]["package"]
        post_id = st.get("post_id")
        if not post_id:
            self.db.update(j["id"], payload={"confirmation": "PUBLISH_COMPLETE without a public post id (private/unmoderated): not confirmed, nothing deleted"})
            return
        ok, detail = self.publisher.verify_post(post_id, pkg)
        vis = pkg["settings"]["privacy_level"]
        if self.mode == "COMPLIANT_DIRECT_POST" and vis == "PUBLIC_TO_EVERYONE" and not post_id:
            ok, detail = False, "public post requested but no public id returned"
        if not ok:
            self.db.update(j["id"], payload={"confirmation": f"not verified: {detail}"})
            self.log("WARN", f"post {post_id} not verified: {detail}", j["id"])
            return
        self.finish_confirmation(j, post_id, detail)

    def finish_confirmation(self, j: dict, post_id: str, detail: str) -> None:
        now = self.clock()
        self.db.transition(j["id"], "UPLOADED", "PUBLISH_CONFIRMED", f"confirmed: {detail}", post_id=post_id, confirmed_at=now)
        self.log("INFO", f"PUBLISH CONFIRMED {j['id']} post_id={post_id}", j["id"])
        mp4 = Path(j["payload"]["mp4"])
        if mp4.is_file() and self.storage.row(mp4):
            with self.db.tx() as c:
                c.execute("UPDATE files SET retention_class='render-recovery' WHERE path=?", (str(mp4.resolve()),))
            dest = self.storage.quarantine(mp4, "published", self.cfg["storage"]["recovery_days"], j["id"])
            self.db.update(j["id"], payload={"mp4": str(dest)})

    # ------------------------------------------------------------------ metrics + learning
    def metrics_step(self) -> None:
        if self.db.kv_get("auth_blocked", False) or self.publisher.name in ("dry-run", "local-queue"):
            return
        try:
            n = learning.collect(self.db, self.publisher, self.clock())
        except AuthExpired as e:
            self.db.kv_set("auth_blocked", True)
            self.log("ERROR", str(e))
            return
        for j in self.db.jobs(state="PUBLISH_CONFIRMED", kind="video"):
            if self.db.q("SELECT 1 FROM metrics WHERE job_id=?", (j["id"],)):
                self.db.transition(j["id"], "PUBLISH_CONFIRMED", "METRICS_COLLECTED", "first official metrics stored")
        for j in self.db.jobs(state="METRICS_COLLECTED", kind="video"):
            if self.db.q("SELECT 1 FROM metrics WHERE job_id=? AND age_label='7d'", (j["id"],)):
                perf = learning.relative_performance(self.db, j["id"], "24h")
                self.db.transition(j["id"], "METRICS_COLLECTED", "LEARNED", "7-day window complete; strategy evidence updated", payload={"performance_24h": perf})
                d = learning.update_strategy(self.db, self.cfg, self.clock())
                self.log("INFO", f"strategy review: {d['reason'][:300]}")
        if n:
            self.log("INFO", f"stored {n} metric snapshots")

    # ------------------------------------------------------------------ retention
    def retention_step(self) -> None:
        for j in self.db.jobs(state="LEARNED", kind="video"):
            files = self.storage.rows(job_id=j["id"])
            media = [f for f in files if f["type"] == "render" and f["state"] != "purged"]
            if all(f["state"] == "quarantine" for f in media):
                self.db.transition(j["id"], "LEARNED", "QUARANTINED", "media in quarantine; only the permanent record remains after purge")
        for j in self.db.jobs(state="QUARANTINED", kind="video"):
            media = [f for f in self.storage.rows(job_id=j["id"]) if f["type"] == "render" and f["state"] != "purged"]
            if not media:
                self.db.transition(j["id"], "QUARANTINED", "PURGED", "media purged; manifest, recipe, caption, title, post id, metrics and QA record kept")
        self.retire_sources()

    def retire_sources(self) -> None:
        """A processed source is quarantined only when it produced at least one CONFIRMED post and no video of it is still in flight."""
        active = {"EDITED", "QA_PASSED", "QUEUED", "UPLOADED", "FAILED", "UPLOAD_UNCERTAIN"}
        for s in self.db.jobs(kind="source", state="SOURCE_DONE"):
            vids = self.db.jobs(kind="video", source_id=s["source_id"])
            if any(v["state"] in active for v in vids) or not any(v["state"] in ("PUBLISH_CONFIRMED", "METRICS_COLLECTED", "LEARNED", "QUARANTINED", "PURGED") and v["confirmed_at"] for v in vids):
                continue
            src = Path(s["payload"]["path"])
            if src.is_file() and self.storage.row(src):
                dest = self.storage.quarantine(src, f"sources/{s['source_id']}", self.cfg["storage"]["recovery_days"], s["id"])
                if dest != src:
                    self.db.transition(s["id"], "SOURCE_DONE", "QUARANTINED", "processed source quarantined (7-day recovery)", payload={"path": str(dest)})
        for s in self.db.jobs(kind="source", state="QUARANTINED"):
            f = self.storage.row(Path(s["payload"]["path"]))
            if f is None or f["state"] == "purged":
                self.db.transition(s["id"], "QUARANTINED", "PURGED", "source purged")

    # ------------------------------------------------------------------ daily summary
    def summary_step(self) -> None:
        now = datetime.fromtimestamp(self.clock(), self.tz)
        if now.hour < self.cfg["daily_summary_hour"]:
            return
        day = f"{now:%Y-%m-%d}"
        if self.db.kv_get("summary_day") == day:
            return
        self.write_summary(day)
        self.db.kv_set("summary_day", day)

    def write_summary(self, day: str | None = None) -> Path:
        day = day or f"{datetime.fromtimestamp(self.clock(), self.tz):%Y-%m-%d}"
        b = self.storage.budget()
        c = self.counts()
        since = self.clock() - DAY
        recent = [dict(r) for r in self.db.q("SELECT to_state, COUNT(*) n FROM transitions WHERE at>=? GROUP BY to_state", (since,))]
        rejected = [dict(r) for r in self.db.q("SELECT job_id, note FROM transitions WHERE to_state='REJECTED' AND at>=? ORDER BY at DESC LIMIT 10", (since,))]
        errs = [dict(r) for r in self.db.q("SELECT message FROM events WHERE level='ERROR' AND at>=? ORDER BY at DESC LIMIT 5", (since,))]
        needs = []
        if self.db.kv_get("auth_blocked", False):
            needs.append("TikTok authorization expired: run `clipagent service auth`")
        if b["ingestion_paused"]:
            needs.append("ingestion paused: " + "; ".join(b["pause_reasons"]))
        if self.mode_note:
            needs.append(self.mode_note)
        for j in self.db.jobs(state="BLOCKED_RIGHTS", kind="source"):
            needs.append(f"rights status missing for {j['payload'].get('name')}")
        for j in self.db.jobs(state="UPLOAD_UNCERTAIN", kind="video"):
            needs.append(f"{j['id']}: upload outcome unknown after a crash (not re-sent)")
        for j in self.db.jobs(state="UPLOADED", kind="video"):
            needs.append(f"{j['id']}: delivered to TikTok but not yet posted/confirmed ({j['payload'].get('upload_status')})")
        strat = self.db.kv_get("strategy")
        summary = {"day": day, "mode": self.mode, "dry_run": self.cfg["dry_run"], "counts": c, "last_24h_transitions": recent, "rejected_recent": rejected, "errors_recent": errs,
                   "storage": {k: b[k] for k in ("free_fraction", "managed_bytes", "budget_bytes", "ingestion_paused")}, "strategy": strat, "attention": needs}
        out = self.root / "logs" / f"daily-summary-{day}.json"
        out.write_text(json.dumps(summary, indent=1, default=str))
        md = [f"# Daily summary {day}", f"mode: {self.mode}{' (DRY RUN)' if self.cfg['dry_run'] else ''}", "",
              "jobs: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())), f"free disk {b['free_fraction']:.1%}, managed {b['managed_bytes'] / 1e9:.2f} GB",
              "", "needs attention:" if needs else "needs attention: nothing"] + [f"- {n}" for n in needs]
        out.with_suffix(".md").write_text("\n".join(md) + "\n")
        return out

    # ------------------------------------------------------------------ user-facing actions
    def mark_posted(self, job_id: str, post_id: str) -> dict:
        """The creator posted a delivered video from the TikTok inbox / ready-to-post queue: record it (the only manual step the official API leaves)."""
        j = self.db.get(job_id)
        if j is None or j["state"] != "UPLOADED":
            raise StateError(f"{job_id} is not awaiting confirmation")
        self.finish_confirmation(j, post_id, "confirmed by the creator via `service mark-posted`")
        return self.db.get(job_id)


def acquire_lock(root: Path):
    f = open(Path(root) / "service" / "daemon.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("another clipagent service instance is already running")
    return f


def health(root: Path, max_age: float = 300.0) -> tuple[bool, str]:
    p = Path(root) / "service" / "health.json"
    if not p.is_file():
        return False, "no heartbeat file"
    d = json.loads(p.read_text())
    age = time.time() - d["ts"]
    return (age <= max_age), f"{d['state']} tick {d['tick']} {age:.0f}s ago, mode {d['mode']}"


def run_forever(svc: Service, once: bool = False) -> None:
    lock = acquire_lock(svc.root)
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *a: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *a: stop.update(flag=True))
    svc.log("INFO", f"service started (mode {svc.mode}, dry_run={svc.cfg['dry_run']})")
    while not stop["flag"]:
        try:
            svc.tick()
        except Exception as exc:
            svc.log("ERROR", f"tick crashed: {exc}")
        if once:
            break
        for _ in range(int(svc.cfg["tick_seconds"])):
            if stop["flag"]:
                break
            time.sleep(1)
    lock.close()
