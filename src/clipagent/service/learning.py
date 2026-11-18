"""Metrics polling and the explore/exploit learning loop. Only OFFICIAL metrics are used; unavailable ones are recorded as unavailable, never invented.

Available through the Display API `video.list` scope: view_count, like_count, comment_count, share_count. NOT available: average watch time, completion
rate, rewatches, saves, follower conversions, profile visits (recorded as unavailable).

"Viral" is account-relative: age-adjusted views per hour against the account's own rolling median at the same post age. A breakout candidate needs
>= 3x the median with >= 10 baseline posts. Strategy changes need >= min_observations_per_arm for BOTH arms, a >= 25% median improvement and a
permutation-test p < 0.1; one experiment (one variable) runs at a time and one post can never change the strategy.
"""
import random
import statistics

from .db import DB

POLLS = {"1h": 1, "6h": 6, "24h": 24, "72h": 72, "7d": 168}
UNAVAILABLE = ["average_watch_time", "completion_rate", "rewatches", "saves", "follower_conversions", "profile_visits"]
VARIABLES = {"slot": None, "caption_style": ["with_context", "minimal"]}      # the only variables the service controls; everything else is observed, not steered
HOUR = 3600.0


def due(db: DB, now: float, hours=None) -> list[tuple[dict, str]]:
    hours = hours or {k: v for k, v in POLLS.items()}
    out = []
    for j in db.jobs(kind="video"):
        if j["state"] not in ("PUBLISH_CONFIRMED", "METRICS_COLLECTED") or not j["confirmed_at"] or not j["post_id"]:
            continue
        have = {r["age_label"] for r in db.q("SELECT age_label FROM metrics WHERE job_id=?", (j["id"],))}
        for label, h in hours.items():
            if label not in have and now - j["confirmed_at"] >= h * HOUR:
                out.append((j, label))
    return out


def collect(db: DB, publisher, now: float, hours=None) -> int:
    """Poll the publisher for every due (job, age) in batches of <= 20 videos (the API limit). Returns rows stored."""
    pending = due(db, now, hours)
    n = 0
    for i in range(0, len(pending), 20):
        batch = pending[i:i + 20]
        res = publisher.video_metrics([j["post_id"] for j, _ in batch])
        for j, label in batch:
            m = res.get(j["post_id"])
            if m is None:
                db.log("WARN", f"no metrics returned for post {j['post_id']} at {label}", j["id"])
                continue
            with db.tx() as c:
                c.execute("INSERT OR REPLACE INTO metrics(job_id,age_label,taken_at,views,likes,comments,shares,unavailable) VALUES(?,?,?,?,?,?,?,?)",
                          (j["id"], label, now, m.get("views"), m.get("likes"), m.get("comments"), m.get("shares"), __import__("json").dumps(UNAVAILABLE)))
            n += 1
    return n


def derived(row, confirmed_at: float, label: str) -> dict:
    age_h = POLLS[label]
    v = row["views"] or 0
    return {"age_hours": age_h, "views_per_hour": v / age_h, "like_rate": (row["likes"] or 0) / v if v else None,
            "comment_rate": (row["comments"] or 0) / v if v else None, "share_rate": (row["shares"] or 0) / v if v else None}


def baseline(db: DB, label: str, exclude: str, n: int = 30) -> tuple[float | None, int]:
    rows = db.q("SELECT m.views FROM metrics m JOIN jobs j ON j.id=m.job_id WHERE m.age_label=? AND m.job_id<>? AND m.views IS NOT NULL ORDER BY m.taken_at DESC LIMIT ?", (label, exclude, n))
    vals = [r["views"] / POLLS[label] for r in rows]
    return (statistics.median(vals) if vals else None), len(vals)


def relative_performance(db: DB, job_id: str, label: str = "24h") -> dict | None:
    r = db.q("SELECT * FROM metrics WHERE job_id=? AND age_label=?", (job_id, label))
    if not r:
        return None
    j = db.get(job_id)
    d = derived(r[0], j["confirmed_at"], label)
    med, n = baseline(db, label, job_id)
    rel = (d["views_per_hour"] / med) if med else None
    return {**d, "baseline_median_vph": med, "baseline_n": n, "relative": rel, "breakout_candidate": bool(rel is not None and n >= 10 and rel >= 3.0)}


def permutation_p(a: list[float], b: list[float], rounds: int = 5000, seed: int = 7) -> float:
    rng = random.Random(seed)
    obs = abs(statistics.median(a) - statistics.median(b))
    pool, na = a + b, len(a)
    hits = 0
    for _ in range(rounds):
        rng.shuffle(pool)
        if abs(statistics.median(pool[:na]) - statistics.median(pool[na:])) >= obs:
            hits += 1
    return (hits + 1) / (rounds + 1)


def observations(db: DB, variable: str, label: str = "24h") -> dict[str, list[float]]:
    """arm value -> age-adjusted performance (views/hour relative to the account's all-post median at that age, or raw vph when no baseline yet)."""
    rows = db.q("SELECT j.id, j.payload, m.views FROM jobs j JOIN metrics m ON m.job_id=j.id WHERE m.age_label=? AND m.views IS NOT NULL", (label,))
    vph = [r["views"] / POLLS[label] for r in rows]
    med = statistics.median(vph) if vph else 1.0
    out: dict[str, list[float]] = {}
    import json
    for r in rows:
        arm = json.loads(r["payload"]).get("arms", {}).get(variable)
        if arm is not None:
            out.setdefault(arm, []).append((r["views"] / POLLS[label]) / (med or 1.0))
    return out


def update_strategy(db: DB, cfg: dict, now: float) -> dict:
    """Conclude the active experiment when both arms have enough observations; otherwise start the next one. Never reacts to a single post."""
    p = cfg["posting"]
    s = db.kv_get("strategy") or {"lead_slot": p["lead_slot"], "experiment": None, "history": [], "caption_style": "with_context"}
    minn = p["min_observations_per_arm"]
    e = s.get("experiment")
    decision = {"changed": False, "reason": "no active experiment with enough data"}
    if e:
        obs = observations(db, e["variable"])
        a, b = obs.get(e["incumbent"], []), obs.get(e["challenger"], [])
        if len(a) >= minn and len(b) >= minn:
            ma, mb, pv = statistics.median(a), statistics.median(b), permutation_p(a, b)
            win = mb >= 1.25 * ma and pv < 0.1
            rec = {"variable": e["variable"], "incumbent": e["incumbent"], "challenger": e["challenger"], "median_incumbent": ma, "median_challenger": mb,
                   "p": pv, "n": [len(a), len(b)], "winner": e["challenger"] if win else e["incumbent"], "at": now}
            s["history"].append(rec)
            if win:
                s["lead_slot" if e["variable"] == "slot" else e["variable"]] = e["challenger"]
            s["experiment"] = None
            decision = {"changed": bool(win), "reason": f"experiment concluded: {rec}"}
        else:
            decision = {"changed": False, "reason": f"experiment {e['variable']}: {e['incumbent']} n={len(a)} vs {e['challenger']} n={len(b)}; need {minn} each"}
    if s.get("experiment") is None:
        tested = [(h["variable"], h["challenger"]) for h in s["history"] if h["winner"] != h["challenger"]]
        nxt = None
        for var in ("slot", "caption_style"):
            inc = s["lead_slot"] if var == "slot" else s[var]
            choices = [x for x in (p["candidate_slots"] if var == "slot" else VARIABLES[var]) if x != inc and (var, x) not in tested]
            if choices:
                nxt = {"variable": var, "incumbent": inc, "challenger": choices[len(s["history"]) % len(choices)], "started": now}
                break
        s["experiment"] = nxt
        if nxt and decision["reason"].startswith("no active"):
            decision["reason"] = f"started experiment: {nxt}"
    db.kv_set("strategy", s)
    return {**decision, "strategy": s}
