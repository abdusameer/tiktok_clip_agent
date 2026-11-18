"""Required simulations 14-15: metrics collection and strategy update without overfitting; plus scheduler behaviour."""
import json
import random
from datetime import datetime

from clipagent.service import learning
from clipagent.service.db import DB
from clipagent.service.scheduler import Scheduler
from service_helpers import PT, Clock, cfg, pt

T0 = pt(2026, 11, 17, 10)
H = 3600


def mkdb(tmp_path, clock=None):
    clock = clock or Clock(T0)
    return DB(tmp_path / "s.db", clock), clock


def add_published(db, clock, n, arms=None, views=None, label_hours=(24,), post_prefix="p"):
    """n published jobs with metrics at the given ages."""
    out = []
    for i in range(n):
        jid = f"vid-{post_prefix}{i}"
        db.add_job(jid, "video", "EDITED", "s", {"arms": arms[i] if arms else {}}, key=jid)
        for a, b in (("EDITED", "QA_PASSED"), ("QA_PASSED", "QUEUED"), ("QUEUED", "UPLOADED")):
            db.transition(jid, a, b)
        db.transition(jid, "UPLOADED", "PUBLISH_CONFIRMED", post_id=f"{post_prefix}{i}", confirmed_at=clock.t)
        for h in label_hours:
            label = {1: "1h", 6: "6h", 24: "24h", 72: "72h", 168: "7d"}[h]
            with db.tx() as c:
                c.execute("INSERT INTO metrics(job_id,age_label,taken_at,views,likes,comments,shares) VALUES(?,?,?,?,?,?,?)",
                          (jid, label, clock.t, views[i] if views else 1000, 50, 5, 2))
        out.append(jid)
    return out


class FakeAPI:
    def __init__(self, data):
        self.data, self.batches = data, []

    def video_metrics(self, ids):
        self.batches.append(list(ids))
        return {i: self.data[i] for i in ids if i in self.data}


# 14 ------------------------------------------------------------------ metrics collection
def test_14_polls_at_1h_6h_24h_72h_7d_in_batches_and_records_unavailable_metrics(tmp_path):
    db, clock = mkdb(tmp_path)
    ids = []
    for i in range(25):                                              # 25 posts: more than one 20-video API batch
        jid = f"vid-{i}"
        db.add_job(jid, "video", "EDITED", "s", {}, key=jid)
        for a, b in (("EDITED", "QA_PASSED"), ("QA_PASSED", "QUEUED"), ("QUEUED", "UPLOADED")):
            db.transition(jid, a, b)
        db.transition(jid, "UPLOADED", "PUBLISH_CONFIRMED", post_id=f"post{i}", confirmed_at=T0)
        ids.append(jid)
    api = FakeAPI({f"post{i}": {"views": 100 * (i + 1), "likes": 10, "comments": 1, "shares": 0} for i in range(25)})
    assert learning.collect(db, api, T0 + 30 * 60) == 0              # too early: nothing due
    assert learning.collect(db, api, T0 + 1 * H) == 25 and [len(b) for b in api.batches] == [20, 5]
    assert learning.collect(db, api, T0 + 1 * H + 10) == 0           # not collected twice
    for hrs in (6, 24, 72, 168):
        assert learning.collect(db, api, T0 + hrs * H) == 25
    labels = {r["age_label"] for r in db.q("SELECT DISTINCT age_label FROM metrics")}
    assert labels == {"1h", "6h", "24h", "72h", "7d"}
    row = db.q("SELECT * FROM metrics WHERE job_id='vid-3' AND age_label='24h'")[0]
    assert row["views"] == 400 and set(json.loads(row["unavailable"])) >= {"average_watch_time", "completion_rate", "saves", "follower_conversions"}
    d = learning.derived(row, T0, "24h")
    assert abs(d["views_per_hour"] - 400 / 24) < 1e-9 and abs(d["like_rate"] - 10 / 400) < 1e-9


def test_14b_account_relative_virality_and_breakout(tmp_path):
    db, clock = mkdb(tmp_path)
    add_published(db, clock, 12, views=[1000] * 12, post_prefix="a")
    add_published(db, clock, 1, views=[4000], post_prefix="b")
    r = learning.relative_performance(db, "vid-b0", "24h")
    assert r["baseline_n"] == 12 and abs(r["relative"] - 4.0) < 1e-6 and r["breakout_candidate"]
    add_published(db, clock, 1, views=[1500], post_prefix="c")
    assert not learning.relative_performance(db, "vid-c0", "24h")["breakout_candidate"]         # 1.5x is not a breakout
    db2, clock2 = mkdb(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path)
    add_published(db2, clock2, 3, views=[1000, 1000, 9000], post_prefix="z")
    assert not learning.relative_performance(db2, "vid-z2", "24h")["breakout_candidate"]        # too little history to call anything viral


# 15 ------------------------------------------------------------------ strategy update without overfitting
def strategy_cfg():
    return cfg(posting={"min_observations_per_arm": 8, "lead_slot": "14:00", "candidate_slots": ["11:00", "14:00", "19:00"]})


def test_15_one_lucky_post_never_changes_the_strategy(tmp_path):
    db, clock = mkdb(tmp_path)
    c = strategy_cfg()
    s0 = learning.update_strategy(db, c, clock.t)["strategy"]
    assert s0["lead_slot"] == "14:00" and s0["experiment"]["variable"] == "slot"                # starts ONE controlled experiment
    e = s0["experiment"]
    arms = [{"slot": "14:00"}] * 9 + [{"slot": e["challenger"]}] * 1
    add_published(db, clock, 10, arms=arms, views=[1000] * 9 + [500000])                       # one viral outlier in the challenger arm
    d = learning.update_strategy(db, c, clock.t)
    assert d["strategy"]["lead_slot"] == "14:00" and not d["changed"] and "need 8 each" in d["reason"]
    assert d["strategy"]["experiment"] == e                                                    # experiment keeps running, nothing concluded


def test_15b_enough_consistent_evidence_changes_the_lead_slot_one_variable_at_a_time(tmp_path):
    db, clock = mkdb(tmp_path)
    c = strategy_cfg()
    e = learning.update_strategy(db, c, clock.t)["strategy"]["experiment"]
    rng = random.Random(1)
    inc = [{"slot": e["incumbent"]}] * 10
    chal = [{"slot": e["challenger"]}] * 10
    views = [int(1000 * rng.uniform(0.9, 1.1)) for _ in range(10)] + [int(2200 * rng.uniform(0.9, 1.1)) for _ in range(10)]
    add_published(db, clock, 20, arms=inc + chal, views=views)
    d = learning.update_strategy(db, c, clock.t)
    assert d["changed"] and d["strategy"]["lead_slot"] == e["challenger"]
    assert d["strategy"]["history"][-1]["p"] < 0.1 and d["strategy"]["history"][-1]["n"] == [10, 10]
    nxt = d["strategy"]["experiment"]
    assert nxt is not None and nxt["variable"] == "slot"                                       # one variable at a time: a new single experiment, not several


def test_15c_noise_does_not_change_the_lead_slot(tmp_path):
    db, clock = mkdb(tmp_path)
    c = strategy_cfg()
    e = learning.update_strategy(db, c, clock.t)["strategy"]["experiment"]
    rng = random.Random(3)
    arms = [{"slot": e["incumbent"]}] * 12 + [{"slot": e["challenger"]}] * 12
    add_published(db, clock, 24, arms=arms, views=[int(1000 * rng.uniform(0.7, 1.3)) for _ in range(24)])
    d = learning.update_strategy(db, c, clock.t)
    assert not d["changed"] and d["strategy"]["lead_slot"] == "14:00"
    assert d["strategy"]["history"][-1]["winner"] == "14:00"


# ------------------------------------------------------------------ scheduler
def sched(tmp_path, mode="PRIVATE_OR_DRAFT_QUEUE", **over):
    db, clock = mkdb(tmp_path)
    return Scheduler(db, cfg(**over), clock, mode), db, clock


def queue(db, jid, ts):
    db.add_job(jid, "video", "EDITED", "s", {}, key=jid)
    db.transition(jid, "EDITED", "QA_PASSED")
    db.transition(jid, "QA_PASSED", "QUEUED", scheduled_at=ts)


def test_scheduler_starts_at_2pm_pacific_and_respects_ceiling_and_spacing(tmp_path):
    s, db, clock = sched(tmp_path, posting={"daily_ceiling": 2, "min_spacing_minutes": 180, "explore_fraction": 0.0})
    times = []
    for i in range(5):
        ts, arms = s.schedule(f"j{i}")
        queue(db, f"j{i}", ts)
        times.append(ts)
        assert arms["slot"] == "14:00" and not arms["explore"]
    dts = [datetime.fromtimestamp(t, PT) for t in times]
    assert all(14 <= d.hour <= 14 and d.minute < 30 + 1 for d in dts)                          # 14:00 slot window, Pacific time
    per_day = {}
    for d in dts:
        per_day[d.date()] = per_day.get(d.date(), 0) + 1
    assert max(per_day.values()) <= 2 and len(per_day) >= 3                                    # daily ceiling holds
    assert all(b - a >= 180 * 60 for a, b in zip(sorted(times), sorted(times)[1:]))            # minimum spacing


def test_scheduler_inbox_mode_never_exceeds_tiktoks_pending_share_cap(tmp_path):
    s, _, _ = sched(tmp_path, posting={"daily_ceiling": 50})
    assert s.ceiling() == 5
    s2, _, _ = sched(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path, mode="COMPLIANT_DIRECT_POST", posting={"daily_ceiling": 50})
    assert s2.ceiling() == 50


def test_scheduler_backoff_is_exponential_and_daily_cap_waits_for_the_next_pacific_day(tmp_path):
    s, db, clock = sched(tmp_path)
    waits = []
    for _ in range(4):
        until = s.record_error("transient")
        waits.append(until - clock.t)
    assert waits == [60, 120, 240, 480] or waits[1] == 2 * waits[0]
    assert not s.can_post_now()[0]
    s.record_success()
    assert s.can_post_now()[0]
    until = s.record_error("daily_cap")
    nxt = datetime.fromtimestamp(until, PT)
    assert nxt.day == 18 and nxt.hour == 0                                                     # next Pacific day
    ts, _ = s.schedule("late")
    assert ts >= until


def test_scheduler_explores_with_the_configured_fraction_and_only_the_active_experiment(tmp_path):
    s, db, clock = sched(tmp_path, posting={"explore_fraction": 0.25, "candidate_slots": ["11:00", "14:00", "19:00"]})
    learning_cfg = s.cfg
    learning.update_strategy(db, learning_cfg, clock.t)
    arms = [s.arm_for(f"job{i}") for i in range(400)]
    frac = sum(a["explore"] for a in arms) / 400
    assert 0.15 < frac < 0.35                                                                  # most posts exploit the leader
    assert {a["slot"] for a in arms if not a["explore"]} == {"14:00"}
    assert len({a["slot"] for a in arms if a["explore"]}) == 1                                  # exactly one challenger at a time
