"""Required simulations 1-9: ingestion, candidate selection hand-off, rendering hand-off, QA rejection, upload, failed-upload recovery, duplicate-post
prevention, crash recovery, expired-token handling. The make-clips stage is stubbed here (the real pipeline is covered by test_clips_e2e.py)."""
import hashlib
import json
import urllib.parse
from pathlib import Path

import pytest

from clipagent.service.daemon import Service
from clipagent.service.db import StateError
from clipagent.service.publishers import PublisherError, SimulatedPublisher, TikTokOfficialPublisher
from clipagent.service.tokens import MemoryBackend, TokenStore
from service_helpers import GB, Clock, Disk, cfg, make_clip, pt, stub_runner

T0 = pt(2026, 11, 17, 10)


def drop(svc_root, name="stream.mp4", body=b"x" * 5000):
    p = svc_root / "sourcevideo" / "inbox" / name
    p.write_bytes(body)
    return p


def mk(svc_root, clock=None, publisher=None, runner=None, **over):
    clock = clock or Clock(T0)
    return Service(svc_root, cfg(**over), publisher=publisher or SimulatedPublisher(), clock=clock, disk_usage=Disk(), runner=runner or stub_runner(svc_root)), clock


def settle(svc, clock, n=1):
    for _ in range(n):
        svc.tick()
        clock.advance(61)


def drive_to_queued(svc, clock):
    """ingest (2 ticks: stability), analyse, select, edit+gate, schedule"""
    settle(svc, clock, 7)


# 1 ------------------------------------------------------------------ source ingestion
def test_1_ingestion_waits_for_stable_file_dedupes_and_records_job(svc_root):
    svc, clock = mk(svc_root)
    f = drop(svc_root)
    svc.tick()                                                       # first sight: not ingested yet (could still be copying)
    clock.advance(1)
    assert svc.db.jobs(kind="source") == []
    f.write_bytes(b"x" * 5000 + b"growing")                           # file changed -> still not stable
    svc.tick()
    assert svc.db.jobs(kind="source") == []
    svc.tick()
    jobs = svc.db.jobs(kind="source")
    assert len(jobs) == 1 and jobs[0]["state"] in ("INGESTED", "ANALYZED", "CANDIDATES_SELECTED", "SOURCE_DONE")
    sid = hashlib.sha256(f.read_bytes()).hexdigest()[:12]
    assert jobs[0]["source_id"] == sid and svc.storage.row(f)["retention_class"] == "source-keep"
    dup = drop(svc_root, "copy.mp4", f.read_bytes())                  # identical bytes under another name
    settle(svc, clock, 3)
    assert not dup.exists() and any(j["state"] == "DUPLICATE_SOURCE" for j in svc.db.jobs(kind="source"))
    assert len([j for j in svc.db.jobs(kind="source") if j["state"] != "DUPLICATE_SOURCE"]) == 1


def test_1b_ingestion_pauses_when_disk_reserve_would_be_crossed(svc_root):
    svc, clock = mk(svc_root)
    svc.storage.disk_usage = Disk(free=140 * GB)                      # 14% free < 15% reserve
    drop(svc_root)
    settle(svc, clock, 4)
    assert svc.db.jobs(kind="source") == []
    assert svc.storage.budget()["ingestion_paused"]


# 2/3 ------------------------------------------------------------------ candidate selection + editing hand-off, state history
def test_2_3_source_walks_the_lifecycle_and_creates_video_jobs(svc_root):
    runner = stub_runner(svc_root, clips=2)
    svc, clock = mk(svc_root, runner=runner)
    drop(svc_root)
    drive_to_queued(svc, clock)
    assert runner.calls == ["analysis", "passes", "qa"]
    src = svc.db.jobs(kind="source")[0]
    assert [h["to_state"] for h in svc.db.history(src["id"])] == ["INGESTED", "ANALYZED", "CANDIDATES_SELECTED", "SOURCE_DONE"]
    vids = svc.db.jobs(kind="video")
    assert len(vids) == 2 and all(v["state"] == "QUEUED" and v["scheduled_at"] for v in vids)
    assert [h["to_state"] for h in svc.db.history(vids[0]["id"])] == ["EDITED", "QA_PASSED", "QUEUED"]
    pkg = vids[0]["payload"]["package"]
    assert "fyp" not in pkg["hashtags"] and "viral" not in pkg["hashtags"] and pkg["title"].startswith("Hallway Scream")


# 4 ------------------------------------------------------------------ QA / gate rejection
@pytest.mark.parametrize("kwargs,expect", [({"qa_fail": "no_frozen_frames"}, "no_frozen_frames"), ({"approved": False}, "clip QA"),
                                           ({"visual_review": "auto-metrics-only"}, "visual_safety")])
def test_4_rejections_are_logged_quarantined_and_do_not_stop_the_queue(svc_root, kwargs, expect):
    over = {"gate": {"visual_safety": {"mode": "block_until_reviewed", "command": None}}} if "visual_review" in kwargs else {}
    svc, clock = mk(svc_root, runner=stub_runner(svc_root, clips=1, **kwargs), **over)
    drop(svc_root)
    drive_to_queued(svc, clock)
    v = svc.db.jobs(kind="video")[0]
    assert v["state"] == "QUARANTINED" and expect in v["last_error"]
    assert [h["to_state"] for h in svc.db.history(v["id"])][:3] == ["EDITED", "REJECTED", "QUARANTINED"]
    assert Path(v["payload"]["mp4"]).is_file() and "quarantine/rejected" in v["payload"]["mp4"]        # kept, not discarded
    assert svc.db.jobs(kind="source")[0]["state"] == "SOURCE_DONE"                                   # the service carried on


def test_4b_agent_reviewed_clip_passes_the_visual_safety_gate(svc_root):
    over = {"gate": {"visual_safety": {"mode": "block_until_reviewed", "command": None}}}
    svc, clock = mk(svc_root, runner=stub_runner(svc_root, clips=1, visual_review="claude-agent"), **over)
    drop(svc_root)
    drive_to_queued(svc, clock)
    assert svc.db.jobs(kind="video")[0]["state"] == "QUEUED"


# 5 ------------------------------------------------------------------ successful upload simulation + confirmation
def test_5_successful_upload_confirms_and_moves_render_to_quarantine(svc_root):
    pub = SimulatedPublisher()
    svc, clock = mk(svc_root, publisher=pub)
    drop(svc_root)
    drive_to_queued(svc, clock)
    v = svc.db.jobs(kind="video")[0]
    clock.t = v["scheduled_at"] + 5
    settle(svc, clock, 2)
    v = svc.db.jobs(kind="video")[0]
    assert v["state"] == "PUBLISH_CONFIRMED" and v["post_id"] and pub.uploads_attempted == 1
    assert "quarantine/published" in v["payload"]["mp4"] and Path(v["payload"]["mp4"]).is_file()
    row = svc.storage.row(Path(v["payload"]["mp4"]))
    assert row["state"] == "quarantine" and abs(row["delete_after"] - (clock.t + 7 * 86400)) < 200


def test_5b_post_not_verified_is_not_confirmed_and_nothing_is_moved(svc_root):
    pub = SimulatedPublisher()
    pub.verify_post = lambda post_id, package: (False, "post not found for this account")
    svc, clock = mk(svc_root, publisher=pub)
    drop(svc_root)
    drive_to_queued(svc, clock)
    clock.t = svc.db.jobs(kind="video")[0]["scheduled_at"] + 5
    settle(svc, clock, 3)
    v = svc.db.jobs(kind="video")[0]
    assert v["state"] == "UPLOADED" and "quarantine" not in v["payload"]["mp4"] and Path(v["payload"]["mp4"]).is_file()


# 6 ------------------------------------------------------------------ failed upload recovery
def test_6_transient_failures_back_off_then_succeed_without_duplicates(svc_root):
    pub = SimulatedPublisher(plan=["transient", "rate_limit", "ok"])
    svc, clock = mk(svc_root, publisher=pub)
    drop(svc_root)
    drive_to_queued(svc, clock)
    clock.t = svc.db.jobs(kind="video")[0]["scheduled_at"] + 5
    for _ in range(40):
        svc.tick()
        clock.advance(120)
        if svc.db.jobs(kind="video")[0]["state"] == "PUBLISH_CONFIRMED":
            break
    v = svc.db.jobs(kind="video")[0]
    assert v["state"] == "PUBLISH_CONFIRMED" and pub.uploads_attempted == 3 and len(pub.sent) == 1


def test_6b_permanent_failure_keeps_the_only_copy_and_other_clips_continue(svc_root):
    pub = SimulatedPublisher(plan=["permanent", "ok"])
    svc, clock = mk(svc_root, publisher=pub, runner=stub_runner(svc_root, clips=2))
    drop(svc_root)
    drive_to_queued(svc, clock)
    clock.t = max(v["scheduled_at"] for v in svc.db.jobs(kind="video")) + 5
    settle(svc, clock, 3)
    states = sorted(v["state"] for v in svc.db.jobs(kind="video"))
    assert states == ["FAILED", "PUBLISH_CONFIRMED"]
    failed = next(v for v in svc.db.jobs(kind="video") if v["state"] == "FAILED")
    assert Path(failed["payload"]["mp4"]).is_file()                      # never delete the only copy of a failed upload
    for _ in range(3):                                                   # and no sweep ever touches it
        svc.storage.purge_due()
        svc.storage.emergency_cleanup()
    assert Path(failed["payload"]["mp4"]).is_file()


# 7 ------------------------------------------------------------------ duplicate-post prevention
def test_7_same_render_can_never_become_two_jobs_or_two_posts(svc_root):
    pub = SimulatedPublisher()
    svc, clock = mk(svc_root, publisher=pub)
    drop(svc_root)
    drive_to_queued(svc, clock)
    j = svc.db.jobs(kind="video")[0]
    assert svc.db.add_job("vid-other-id", "video", "EDITED", j["source_id"], {}, key=j["idempotency_key"]) is False      # same render -> refused
    svc.collect_videos(svc.db.jobs(kind="source")[0])                                                                    # re-collecting is idempotent
    assert len(svc.db.jobs(kind="video")) == 1
    with pytest.raises(StateError):                                                                                      # stale writer cannot move a job twice
        svc.db.transition(j["id"], "EDITED", "QA_PASSED")
    clock.t = j["scheduled_at"] + 5
    settle(svc, clock, 4)
    svc.upload_step()
    assert pub.uploads_attempted == 1


# 8 ------------------------------------------------------------------ crash recovery
def test_8_crash_after_tiktok_received_the_video_recovers_without_a_second_post(svc_root):
    pub = SimulatedPublisher(plan=["crash_after_send"])
    svc, clock = mk(svc_root, publisher=pub)
    drop(svc_root)
    drive_to_queued(svc, clock)
    clock.t = svc.db.jobs(kind="video")[0]["scheduled_at"] + 5
    with pytest.raises(KeyboardInterrupt):
        svc.upload_step()                                                # process "dies" between TikTok accepting it and us recording it
    j = svc.db.jobs(kind="video")[0]
    assert j["state"] == "QUEUED" and j["upload_phase"] == "INTENT" and not j["publish_id"]
    svc2, _ = mk(svc_root, clock=clock, publisher=pub)                   # restart: a brand-new Service on the same database
    settle(svc2, clock, 3)
    j = svc2.db.jobs(kind="video")[0]
    assert j["state"] in ("UPLOADED", "PUBLISH_CONFIRMED") and pub.uploads_attempted == 1 and len(pub.sent) == 1


def test_8b_crash_with_unknowable_outcome_is_quarantined_not_resent(svc_root):
    class Blind(SimulatedPublisher):
        def find_existing(self, idem):
            return None
    pub = Blind(plan=["crash_after_send"])
    svc, clock = mk(svc_root, publisher=pub)
    drop(svc_root)
    drive_to_queued(svc, clock)
    clock.t = svc.db.jobs(kind="video")[0]["scheduled_at"] + 5
    with pytest.raises(KeyboardInterrupt):
        svc.upload_step()
    svc2, _ = mk(svc_root, clock=clock, publisher=pub)
    settle(svc2, clock, 4)
    assert svc2.db.jobs(kind="video")[0]["state"] == "UPLOAD_UNCERTAIN" and pub.uploads_attempted == 1


def test_8c_crash_mid_edit_resumes_from_the_saved_state(svc_root):
    calls = []

    def flaky(video, stage, ct="auto", rights=None):
        calls.append(stage)
        if stage == "passes" and calls.count("passes") == 1:
            raise RuntimeError("simulated worker crash")
        return stub_runner(svc_root)(video, stage, ct, rights)
    svc, clock = mk(svc_root, runner=flaky)
    drop(svc_root)
    settle(svc, clock, 12)
    assert svc.db.jobs(kind="source")[0]["state"] == "SOURCE_DONE" and calls.count("analysis") == 1       # analysis was not redone
    assert svc.db.jobs(kind="video")[0]["state"] in ("QUEUED", "UPLOADED", "PUBLISH_CONFIRMED")


def test_8d_source_that_always_fails_is_isolated_after_max_attempts(svc_root):
    def bad(video, stage, ct="auto", rights=None):
        return {"returncode": 1, "stdout": "", "stderr": "boom"}
    svc, clock = mk(svc_root, runner=bad)
    drop(svc_root)
    drop(svc_root, "second.mp4", b"y" * 6000)
    settle(svc, clock, 25)
    assert {j["state"] for j in svc.db.jobs(kind="source")} == {"FAILED"}
    assert svc.db.jobs(kind="source")[0]["attempts"] == 3


def test_8e_rights_missing_blocks_only_that_source(svc_root):
    def rights(video, stage, ct="auto", rights=None):
        return {"returncode": 2, "stdout": "STOP rights", "stderr": ""} if video.name == "film.mp4" else stub_runner(svc_root)(video, stage, ct, rights)
    svc, clock = mk(svc_root, runner=rights)
    drop(svc_root, "film.mp4", b"f" * 5000)
    drop(svc_root, "stream.mp4", b"s" * 5000)
    settle(svc, clock, 9)
    by = {j["payload"]["name"]: j["state"] for j in svc.db.jobs(kind="source")}
    assert by["film.mp4"] == "BLOCKED_RIGHTS" and by["stream.mp4"] == "SOURCE_DONE"


# 9 ------------------------------------------------------------------ expired token handling
class FakeTikTok:
    """Minimal fake of TikTok's HTTP API: token endpoint with rotation, creator_info, inbox init, chunk PUT, status, video query."""
    def __init__(self, refresh_ok=True):
        self.refresh_ok, self.calls, self.valid_access, self.valid_refresh = refresh_ok, [], "access-1", "refresh-1"
        self.n = 1

    def request(self, method, url, headers, body=None, timeout=60):
        self.calls.append((method, url))
        path = urllib.parse.urlparse(url).path
        if path == "/v2/oauth/token/":
            form = urllib.parse.parse_qs(body.decode())
            if not self.refresh_ok or form["refresh_token"][0] != self.valid_refresh:
                return 400, {"error": "invalid_grant"}
            self.n += 1
            self.valid_access, self.valid_refresh = f"access-{self.n}", f"refresh-{self.n}"
            return 200, {"access_token": self.valid_access, "refresh_token": self.valid_refresh, "expires_in": 7200, "refresh_expires_in": 15552000, "open_id": "o"}
        if headers.get("Authorization") != f"Bearer {self.valid_access}" and path.startswith("/v2/post"):
            return 401, {"error": {"code": "access_token_invalid"}}
        if path == "/v2/post/publish/inbox/video/init/":
            return 200, {"data": {"publish_id": "p1", "upload_url": "https://upload.example/x"}, "error": {"code": "ok"}}
        if method == "PUT":
            return 200, {}
        if path == "/v2/post/publish/status/fetch/":
            return 200, {"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [999]}, "error": {"code": "ok"}}
        if path == "/v2/video/query/":
            return 200, {"data": {"videos": [{"id": 999, "view_count": 10, "like_count": 1, "comment_count": 0, "share_count": 0}]}, "error": {"code": "ok"}}
        return 404, {}


def official(clock, http, refresh_expiry=4e9, access_expiry=0.0):
    ts = TokenStore(cfg(), MemoryBackend(), clock)
    ts.set_client("key", "secret")
    ts.b.set(ts.t["token_keychain"], json.dumps({"access_token": "access-1", "refresh_token": "refresh-1", "open_id": "o", "scope": "video.upload",
                                                  "access_expires_at": access_expiry, "refresh_expires_at": refresh_expiry}))
    return TikTokOfficialPublisher(cfg(), ts, "PRIVATE_OR_DRAFT_QUEUE", http, clock), ts


def test_9_expired_access_token_is_refreshed_and_rotated(svc_root, tmp_path):
    clock, http = Clock(T0), FakeTikTok()
    pub, ts = official(clock, http, access_expiry=T0 - 10)
    mp4 = tmp_path / "v.mp4"
    mp4.write_bytes(b"0" * 3000)
    pid = pub.upload(mp4, {"title": "t", "caption": "c", "settings": {}, "cover_timestamp_ms": 0}, "idem")
    assert pid == "p1" and ts.load()["refresh_token"] == "refresh-2"      # rotated refresh token stored immediately
    assert any(u.endswith("/v2/oauth/token/") for _, u in http.calls)


def test_9b_dead_refresh_token_pauses_posting_not_the_rest_of_the_service(svc_root):
    clock, http = Clock(T0), FakeTikTok(refresh_ok=False)
    pub, _ = official(clock, http, access_expiry=T0 - 10)
    svc, _ = mk(svc_root, clock=clock, publisher=pub, runner=stub_runner(svc_root, clips=2))
    drop(svc_root)
    drive_to_queued(svc, clock)
    clock.t = max(v["scheduled_at"] for v in svc.db.jobs(kind="video")) + 5
    settle(svc, clock, 3)
    assert svc.db.kv_get("auth_blocked") is True
    assert {v["state"] for v in svc.db.jobs(kind="video")} == {"QUEUED"}                     # waiting, nothing lost, no attempts burned
    drop(svc_root, "another.mp4", b"z" * 7000)
    settle(svc, clock, 8)
    assert any(j["payload"]["name"] == "another.mp4" and j["state"] == "SOURCE_DONE" for j in svc.db.jobs(kind="source"))   # other work continues
    assert "authorization" in json.dumps(svc.write_summary().read_text()).lower()


def test_9c_refresh_expiry_raises_reauthorisation_error(tmp_path):
    clock = Clock(T0)
    pub, _ = official(clock, FakeTikTok(), refresh_expiry=T0 - 1, access_expiry=T0 - 1)
    from clipagent.service.publishers import AuthExpired
    with pytest.raises(AuthExpired):
        pub.access_token()


# ------------------------------------------------------------------ inbox: a video with its separate audio file
def test_inbox_combines_a_video_with_its_separate_audio_and_keeps_the_originals(svc_root):
    import subprocess
    from service_helpers import make_mp4
    inbox = svc_root / "sourcevideo" / "inbox"
    v = make_mp4(inbox / "talk.mp4", 3.0)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(v), "-vn", "-c:a", "pcm_s16le", str(inbox / "talk.wav")], check=True)
    seen = []

    def runner(video, stage, ct="auto", rights=None):
        seen.append(Path(video))
        return {"returncode": 0, "stdout": "", "stderr": ""}
    svc, clock = mk(svc_root, runner=runner)
    settle(svc, clock, 4)
    jobs = svc.db.jobs(kind="source")
    assert len(jobs) == 1 and "_muxed" in jobs[0]["payload"]["path"]
    assert v.exists() and (inbox / "talk.wav").exists()                      # the user's originals are untouched
    assert all("_muxed" in str(p) for p in seen) and len(svc.db.jobs(kind="source")) == 1     # not ingested a second time as a lone video
