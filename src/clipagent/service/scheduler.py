"""Adaptive posting scheduler (Pacific Time by default).

Honours: a daily ceiling (also capped by TikTok's pending-share limit in inbox mode), minimum spacing, an error backoff (exponential, and until local
midnight after TikTok reports the daily cap), a queue that lives in the database. Time slots start with the configured lead slot (14:00: ONE earlier
observation, not a conclusion) and gradually test other slots through a controlled experiment (see learning.py); the lead slot only changes after the
experiment has enough account-specific evidence.
"""
import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .db import DB

COUNTED = ("QUEUED", "UPLOADED", "PUBLISH_CONFIRMED", "METRICS_COLLECTED", "LEARNED", "QUARANTINED", "PURGED")


def _u(job_id: str, salt: str) -> float:
    return int(hashlib.sha256(f"{job_id}:{salt}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


class Scheduler:
    def __init__(self, db: DB, cfg: dict, clock, mode: str):
        self.db, self.cfg, self.clock, self.mode = db, cfg, clock, mode
        self.p = cfg["posting"]
        self.tz = ZoneInfo(cfg["timezone"])

    # ------------------------------------------------------------------ strategy state
    def strategy(self) -> dict:
        s = self.db.kv_get("strategy")
        if not s:
            s = {"lead_slot": self.p["lead_slot"], "experiment": None, "history": [], "caption_style": "with_context"}
            self.db.kv_set("strategy", s)
        return s

    def ceiling(self) -> int:
        c = self.p["daily_ceiling"]
        return min(c, self.p["inbox_pending_cap_per_24h"]) if self.mode == "PRIVATE_OR_DRAFT_QUEUE" else c

    # ------------------------------------------------------------------ backoff
    def backoff_until(self) -> float:
        return float(self.db.kv_get("backoff_until", 0.0))

    def record_error(self, kind: str = "error") -> float:
        r = self.cfg["retry"]
        n = int(self.db.kv_get("consecutive_errors", 0)) + 1
        self.db.kv_set("consecutive_errors", n)
        now = self.clock()
        wait = min(r["base_seconds"] * (2 ** (n - 1)), r["cap_seconds"])
        if kind == "daily_cap":                                          # TikTok said the daily cap is reached: wait for the next Pacific day
            nxt = (datetime.fromtimestamp(now, self.tz) + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
            wait = nxt.timestamp() - now
        until = now + wait
        self.db.kv_set("backoff_until", until)
        self.db.log("WARN", f"posting backoff {wait:.0f}s ({kind}, {n} consecutive)")
        return until

    def record_success(self) -> None:
        self.db.kv_set("consecutive_errors", 0)
        self.db.kv_set("backoff_until", 0.0)

    def can_post_now(self) -> tuple[bool, str]:
        now = self.clock()
        if now < self.backoff_until():
            return False, f"backing off until {datetime.fromtimestamp(self.backoff_until(), self.tz):%Y-%m-%d %H:%M %Z}"
        return True, ""

    # ------------------------------------------------------------------ slot choice
    def _scheduled(self) -> list[float]:
        q = ",".join("?" * len(COUNTED))
        return [r["scheduled_at"] for r in self.db.q(f"SELECT scheduled_at FROM jobs WHERE kind='video' AND scheduled_at IS NOT NULL AND state IN ({q})", COUNTED)]

    def arm_for(self, job_id: str) -> dict:
        """Which strategy arm this post carries: exploit = incumbent; with probability explore_fraction the challenger of the ONE active experiment."""
        s = self.strategy()
        arms = {"slot": s["lead_slot"], "caption_style": s["caption_style"], "explore": False}
        e = s.get("experiment")
        if e and _u(job_id, "explore") < self.p["explore_fraction"]:
            arms[e["variable"]] = e["challenger"]
            arms["explore"] = True
        return arms

    def schedule(self, job_id: str, now: float | None = None) -> tuple[float, dict]:
        now = self.clock() if now is None else now
        arms = self.arm_for(job_id)
        slot = arms["slot"]
        sched = self._scheduled()
        spacing = self.p["min_spacing_minutes"] * 60
        win = self.p["slot_window_minutes"]
        day = datetime.fromtimestamp(max(now, self.backoff_until()), self.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        for _ in range(60):
            hh, mm = map(int, slot.split(":"))
            jitter = int(_u(job_id, f"jitter{day:%Y%m%d}") * win)
            t = day.replace(hour=hh, minute=mm) + timedelta(minutes=jitter)
            ts = t.timestamp()
            same_day = [x for x in sched if datetime.fromtimestamp(x, self.tz).date() == t.date()]
            if ts > now + 300 and ts >= self.backoff_until() and len(same_day) < self.ceiling() and all(abs(ts - x) >= spacing for x in sched):
                return ts, arms
            day += timedelta(days=1)
        raise RuntimeError("no schedulable slot in the next 60 days (check posting.daily_ceiling / min_spacing_minutes)")
