"""Required simulations 10-13: storage high-water cleanup, quarantine recovery, safe purge, protection against deleting untracked paths. Plus the full
retention lifecycle through PURGED."""
import os
from pathlib import Path

import pytest

from clipagent.service.daemon import Service
from clipagent.service.db import DB
from clipagent.service.publishers import SimulatedPublisher
from clipagent.service.storage import Storage, StorageError
from service_helpers import GB, Clock, Disk, cfg, pt, stub_runner

T0 = pt(2026, 11, 17, 10)
DAY = 86400


def mkstore(svc_root, disk=None, dry_run=False, **over):
    clock = Clock(T0)
    c = cfg(**over)
    db = DB(svc_root / "service" / "state.db", clock)
    return Storage(db, svc_root, c, disk or Disk(), clock, dry_run=dry_run), clock


def file(svc_root, rel, size=1000):
    p = svc_root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(os.urandom(size))
    return p


# 13 ------------------------------------------------------------------ untracked paths are never deleted
def test_13_untracked_and_outside_paths_are_never_deleted(svc_root, tmp_path_factory):
    st, _ = mkstore(svc_root)
    outside = tmp_path_factory.mktemp("elsewhere") / "precious.txt"
    outside.write_text("keep me")
    inside_untracked = file(svc_root, "quarantine/x/untracked.mp4")
    for p in (outside, inside_untracked, Path("/etc/hosts"), svc_root, svc_root / "quarantine"):
        with pytest.raises(StorageError):
            st.purge(p)
    assert outside.exists() and inside_untracked.exists()
    with pytest.raises(StorageError):
        st.purge(Path("relative/path.mp4"))                                           # exact absolute paths only
    with pytest.raises(StorageError):
        st.register(outside, "render", None, "active", "render-recovery")           # cannot even be registered from outside the workspace
    d = svc_root / "quarantine" / "dir"
    d.mkdir(parents=True)
    with pytest.raises(StorageError):
        st.purge(d)                                                                   # directories are never deleted by purge


def test_13b_protected_classes_and_symlinks_are_refused(svc_root):
    st, _ = mkstore(svc_root)
    for cls in ("render-unconfirmed", "source-keep", "permanent"):
        p = file(svc_root, f"runs/abc/{cls}.bin")
        st.register(p, "render", "abc", "quarantine", cls)
        with pytest.raises(StorageError):
            st.purge(p)
        assert p.exists()
    real = file(svc_root, "quarantine/real.bin")
    link = svc_root / "quarantine" / "link.bin"
    link.symlink_to(real)
    with pytest.raises(StorageError):
        st.register(link, "render", None, "quarantine", "render-recovery")
    # a ledger path routed through a symlinked directory is refused too
    (svc_root / "quarantine" / "sub").mkdir()
    victim = file(svc_root, "runs/victim.bin")
    (svc_root / "quarantine" / "evil").symlink_to(svc_root / "runs")
    sneaky = svc_root / "quarantine" / "evil" / "victim.bin"
    with st.db.tx() as c:
        c.execute("INSERT INTO files(path,type,source_id,size,sha256,created,state,retention_class,delete_after,job_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (str(sneaky), "render", None, 1, None, 0, "quarantine", "quarantine-media", 0, None))
    with pytest.raises(StorageError):
        st.purge(sneaky)
    assert victim.exists()


# 12 ------------------------------------------------------------------ safe purge: exact ledger paths, idempotent, logged, dry-run honoured
def test_12_safe_purge_is_exact_idempotent_logged_and_respects_dry_run(svc_root):
    st, clock = mkstore(svc_root)
    a, b = file(svc_root, "runs/s/a.mp4"), file(svc_root, "runs/s/b.mp4")
    for p in (a, b):
        st.register(p, "render", "s", "active", "render-recovery")
    qa = st.quarantine(a, "published", 7)
    st.quarantine(b, "published", 7)
    assert st.purge_due() == []                                                       # not due yet
    clock.advance(7 * DAY + 1)
    done = st.purge_due()
    assert sorted(Path(x).name for x in done) == ["a.mp4", "b.mp4"] and not qa.exists()
    assert st.purge_due() == [] and st.purge(qa) is False                              # idempotent: second purge is a no-op
    assert st.row(qa)["state"] == "purged" and st.row(qa)["size"] == 0                 # the ledger keeps the audit row
    assert any("purged" in r["message"] for r in st.db.q("SELECT message FROM events"))
    assert not (svc_root / "quarantine" / "published").exists()                        # empty dirs removed, nothing else touched
    assert (svc_root / "runs" / "s").exists()
    dry, _ = mkstore(svc_root, dry_run=True)
    c = file(svc_root, "runs/s/c.mp4")
    dry.register(c, "render", "s", "temp", "temp")
    assert dry.purge(c) is True and c.exists()                                         # dry run: reported, not deleted


# 11 ------------------------------------------------------------------ quarantine recovery
def test_11_quarantined_media_can_be_restored_before_it_is_purged(svc_root):
    st, clock = mkstore(svc_root)
    p = file(svc_root, "approved/standalone/s/v.mp4", 4000)
    row = st.register(p, "render", "s", "active", "render-recovery")
    q = st.quarantine(p, "published", 7)
    assert not p.exists() and q.exists()
    back = st.restore(q, p)
    assert back.read_bytes() and st.row(back)["sha256"] == row["sha256"] and not q.exists()
    st.quarantine(back, "published", 7)
    clock.advance(8 * DAY)
    st.purge_due()
    with pytest.raises(StorageError):
        st.restore(q, p)                                                              # after the window it is gone for good


# 10 ------------------------------------------------------------------ high-water cleanup, oldest first, never the unconfirmed
def test_10_high_water_cleanup_frees_temp_then_oldest_quarantine_but_spares_unconfirmed(svc_root):
    disk = Disk(total=100 * GB, free=50 * GB)
    st, clock = mkstore(svc_root, disk=disk)
    keep = file(svc_root, "runs/s/final.mp4", 100)
    st.register(keep, "render", "s", "active", "render-unconfirmed")
    src = file(svc_root, "sourcevideo/inbox/src.mp4", 100)
    st.register(src, "source", "s", "active", "source-keep")
    tmp = file(svc_root, "runs/s/work/frame.jpg", 100)
    st.register(tmp, "temp", "s", "temp", "temp", hash_it=False)
    olds = []
    for i in range(3):
        p = file(svc_root, f"runs/s/q{i}.mp4", 100)
        st.register(p, "render", "s", "active", "render-recovery")
        olds.append(st.quarantine(p, "published", 7))
        clock.advance(10)
    # simulate a managed footprint past 90% of the 20 GB budget
    with st.db.tx() as c:
        c.execute("UPDATE files SET size=? WHERE path=?", (int(7 * GB), str(olds[0])))
        c.execute("UPDATE files SET size=? WHERE path=?", (int(7 * GB), str(olds[1])))
        c.execute("UPDATE files SET size=? WHERE path=?", (int(7 * GB), str(olds[2])))
    assert st.budget()["high_water"] and st.budget()["ingestion_paused"]
    freed = st.emergency_cleanup()
    assert str(tmp.resolve()) in [str(Path(x)) for x in freed["temp"]] or not tmp.exists()
    assert not olds[0].exists()                                                       # oldest quarantined media went first
    assert keep.exists() and src.exists()                                             # unconfirmed render and live source untouched
    assert not st.budget()["high_water"] or olds[2].exists()                          # stops as soon as the pressure is relieved


def test_10b_budget_pauses_ingestion_before_the_reserve_is_crossed(svc_root):
    st, _ = mkstore(svc_root, disk=Disk(total=1000 * GB, free=160 * GB))               # 16%: within the 2% margin of the 15% reserve
    ok, why = st.ingestion_allowed()
    assert not ok and "reserve" in why[0]
    st2, _ = mkstore(svc_root, disk=Disk(total=1000 * GB, free=600 * GB))
    assert st2.ingestion_allowed()[0]
    assert st2.budget()["budget_bytes"] == 200 * GB                                    # never more than 20% of the disk


# ------------------------------------------------------------------ full retention lifecycle through the daemon
def test_full_lifecycle_publish_metrics_learn_quarantine_purge(svc_root):
    clock = Clock(T0)
    pub = SimulatedPublisher()
    svc = Service(svc_root, cfg(), publisher=pub, clock=clock, disk_usage=Disk(), runner=stub_runner(svc_root, clips=1))
    src = svc_root / "sourcevideo" / "inbox" / "stream.mp4"
    src.write_bytes(b"s" * 5000)
    for _ in range(7):
        svc.tick()
        clock.advance(61)
    v = svc.db.jobs(kind="video")[0]
    clock.t = v["scheduled_at"] + 5
    svc.tick(); clock.advance(61); svc.tick()
    v = svc.db.jobs(kind="video")[0]
    assert v["state"] == "PUBLISH_CONFIRMED"
    svc.tick()
    assert svc.db.jobs(kind="source")[0]["state"] == "QUARANTINED" and not src.exists()      # processed source quarantined only after a CONFIRMED post
    pub.metrics_by_post[v["post_id"]] = {"views": 1000, "likes": 100, "comments": 10, "shares": 5}
    for hours in (2, 6, 24, 72, 168):                                                  # poll windows 1h,6h,24h,72h,7d
        clock.t = v["confirmed_at"] + hours * 3600
        svc.tick()
    v = svc.db.jobs(kind="video")[0]
    assert {r["age_label"] for r in svc.db.q("SELECT age_label FROM metrics WHERE job_id=?", (v["id"],))} == {"1h", "6h", "24h", "72h", "7d"}
    hist = [h["to_state"] for h in svc.db.history(v["id"])]
    assert hist == ["EDITED", "QA_PASSED", "QUEUED", "UPLOADED", "PUBLISH_CONFIRMED", "METRICS_COLLECTED", "LEARNED", "QUARANTINED", "PURGED"]     # the exact lifecycle
    assert v["state"] == "PURGED"                                                      # recovery window (7 days after confirmation) has ended
    assert svc.db.jobs(kind="source")[0]["state"] == "PURGED"
    clock.t += 8 * DAY
    for _ in range(3):
        svc.tick()
    assert svc.db.jobs(kind="source")[0]["state"] == "PURGED"
    rows = {Path(r["path"]).name: r for r in svc.storage.rows()}
    assert rows["S01-clip-1.mp4"]["state"] == "purged" and not any(Path(r["path"]).exists() for r in rows.values() if r["state"] == "purged")
    kept = [Path(r["path"]).name for r in svc.storage.rows() if r["state"] != "purged"]
    assert {"manifest.json", "qa.json", "caption.txt"} <= set(kept)                    # the permanent record survives
    assert (svc_root / "service" / "state.db").exists()
    assert not any(p.suffix == ".mp4" for p in (svc_root / "quarantine").rglob("*"))   # no media left anywhere in quarantine
