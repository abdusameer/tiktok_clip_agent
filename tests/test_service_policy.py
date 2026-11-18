"""Compliance mode, gate checks, post package, kill switch, dry run, health, launchd, no-secrets-in-source."""
import json
import re
import subprocess
from pathlib import Path

from clipagent.service import config as svc_config, gate, launchd, postpackage
from clipagent.service.daemon import Service, health
from clipagent.service.publishers import SimulatedPublisher
from service_helpers import Clock, Disk, cfg, make_clip, pt, stub_runner

T0 = pt(2026, 11, 17, 10)


# ------------------------------------------------------------------ publishing mode honesty
def test_direct_post_is_refused_without_audit_consent_and_ux_attestation():
    mode, why = svc_config.effective_mode(cfg(mode="COMPLIANT_DIRECT_POST"))
    assert mode == "PRIVATE_OR_DRAFT_QUEUE" and "audited_client" in why and "consent_recorded_at" in why and "ux_flow_attested" in why
    ok = cfg(mode="COMPLIANT_DIRECT_POST", direct_post={"audited_client": True, "audit_reference": "TikTok audit #123", "consent_recorded_at": "2026-11-17",
                                                        "ux_flow_attested": True})
    assert svc_config.effective_mode(ok) == ("COMPLIANT_DIRECT_POST", None)
    half = cfg(mode="COMPLIANT_DIRECT_POST", direct_post={"audited_client": True})
    assert svc_config.effective_mode(half)[0] == "PRIVATE_OR_DRAFT_QUEUE"


def test_default_privacy_is_private_and_config_has_no_secrets():
    assert svc_config.DEFAULTS["post_defaults"]["privacy_level"] == "SELF_ONLY" and svc_config.DEFAULTS["dry_run"] is True
    blob = json.dumps(svc_config.DEFAULTS).lower()
    assert "password" not in blob and "access_token" not in blob


def test_no_credentials_or_browser_automation_in_the_service_source():
    src = "\n".join(p.read_text() for p in Path(svc_config.__file__).parent.glob("*.py"))
    assert not re.search(r"selenium|playwright|puppeteer|pyppeteer|webdriver", src, re.I)
    assert not re.search(r"(client_secret|password|access_token)\s*=\s*[\"'][A-Za-z0-9_\-]{12,}", src)


# ------------------------------------------------------------------ post package
def man(**kw):
    return make_clip.__globals__["json"].loads(json.dumps(kw)) if kw else None


def test_post_package_is_truthful_searchable_and_never_stuffed(tmp_path):
    m = make_clip(tmp_path, "abc123abc123", "f" * 64)
    p = postpackage.build(m, cfg(), {"streamer": "Jynxzi", "game_or_topic": "Rainbow Six Siege", "hashtags": ["fyp", "viral", "R6Siege"]})
    assert p["title"] == "Hallway Scream | Rainbow Six Siege | Jynxzi" and len(p["title"]) <= 90
    assert not {"fyp", "viral", "foryou", "trending"} & set(p["hashtags"]) and "rainbowsixsiege" in p["hashtags"] and len(p["hashtags"]) <= 5
    assert "Oh no no no get it off me" in p["description"] and "Jynxzi" in p["description"]      # quotes what was actually said
    assert p["cover_timestamp_ms"] == 800 and p["settings"]["allow_duet"] is False and p["settings"]["privacy_level"] == "SELF_ONLY"
    minimal = postpackage.build(m, cfg(), {"streamer": "Jynxzi", "game_or_topic": "Rainbow Six Siege"}, style="minimal")
    assert minimal["title"] == "Hallway Scream | Jynxzi"


# ------------------------------------------------------------------ gate
def run_gate(tmp_path, posted=None, **kw):
    m = make_clip(tmp_path, "abc123abc123", "f" * 64, **kw.pop("clip", {}))
    return gate.run(m, tmp_path / m["location"] / m["output"], cfg(**kw), posted or []), m


def test_gate_passes_a_clean_clip_and_reports_every_check(tmp_path):
    g, _ = run_gate(tmp_path)
    assert g["ok"] and g["score"] >= 0.6 and {"playback_test", "rights_resolved", "editorial_score", "visual_safety", "no_duplicate_or_near_duplicate_post"} <= {c["check"] for c in g["checks"]}


def test_gate_blocks_each_required_failure(tmp_path):
    for fail in ("caption_accuracy", "no_black_frames", "av_sync_stream_durations", "subject_tracking_keeps_face", "offensive_written_captions", "captions_inside_safe_area"):
        g, _ = run_gate(tmp_path / fail, clip={"qa_fail": fail})
        assert not g["ok"] and any(fail in r for r in g["reasons"]), fail


def test_gate_blocks_unreviewed_visual_safety_missing_rights_low_score_corrupt_file_and_near_duplicates(tmp_path):
    g, m = run_gate(tmp_path / "a", gate={"visual_safety": {"mode": "block_until_reviewed", "command": None}})
    assert not g["ok"] and any("visual_safety" in r for r in g["reasons"])
    m2 = make_clip(tmp_path / "b", "abc123abc123", "f" * 64)
    m2["rights"], m2["rights_note"], m2["content_type"] = None, None, "movie"
    assert not gate.run(m2, tmp_path / "b" / m2["location"] / m2["output"], cfg(), [])["ok"]
    m3 = make_clip(tmp_path / "c", "abc123abc123", "f" * 64)
    m3["editorial"][0].update(candidate_score=0.1, drivers=[])
    assert any("editorial_score" in r for r in gate.run(m3, tmp_path / "c" / m3["location"] / m3["output"], cfg(), [])["reasons"])
    m4 = make_clip(tmp_path / "d", "abc123abc123", "f" * 64)
    bad = tmp_path / "d" / m4["location"] / m4["output"]
    bad.write_bytes(bad.read_bytes()[:3000])                                      # truncated file: playback test must catch it
    assert any("playback_test" in r for r in gate.run(m4, bad, cfg(), [])["reasons"])
    m5 = make_clip(tmp_path / "e", "abc123abc123", "f" * 64, start=10.0, end=12.0)
    earlier = make_clip(tmp_path / "f", "abc123abc123", "f" * 64, start=10.2, end=12.1)      # same source, ~same footage
    assert any("near_duplicate" in r for r in gate.run(m5, tmp_path / "e" / m5["location"] / m5["output"], cfg(), [earlier])["reasons"])
    far = make_clip(tmp_path / "g", "abc123abc123", "f" * 64, start=60.0, end=63.0)
    assert gate.run(m5, tmp_path / "e" / m5["location"] / m5["output"], cfg(), [far])["ok"]


# ------------------------------------------------------------------ kill switch, dry run, health
def test_emergency_stop_file_freezes_everything_but_the_heartbeat(svc_root):
    clock = Clock(T0)
    svc = Service(svc_root, cfg(), publisher=SimulatedPublisher(), clock=clock, disk_usage=Disk(), runner=stub_runner(svc_root))
    (svc_root / "sourcevideo" / "inbox" / "a.mp4").write_bytes(b"a" * 4000)
    (svc_root / "STOP").write_text("stop")
    for _ in range(6):
        svc.tick()
        clock.advance(61)
    assert svc.db.jobs() == [] and json.loads((svc_root / "service" / "health.json").read_text())["state"] == "STOPPED"
    (svc_root / "STOP").unlink()
    for _ in range(4):
        svc.tick()
        clock.advance(61)
    assert svc.db.jobs(kind="source")


def test_dry_run_never_uploads_deletes_or_confirms(svc_root):
    clock = Clock(T0)
    svc = Service(svc_root, cfg(dry_run=True), clock=clock, disk_usage=Disk(), runner=stub_runner(svc_root))
    src = svc_root / "sourcevideo" / "inbox" / "a.mp4"
    src.write_bytes(b"a" * 4000)
    for _ in range(8):
        svc.tick()
        clock.advance(61)
    v = svc.db.jobs(kind="video")[0]
    clock.t = v["scheduled_at"] + 5
    for _ in range(40 if False else 6):
        svc.tick()
        clock.advance(3600 * 24)
    v = svc.db.jobs(kind="video")[0]
    assert v["state"] == "UPLOADED" and v["post_id"] is None                      # never confirmed in a dry run
    assert src.exists() and Path(v["payload"]["mp4"]).exists()                    # nothing deleted or moved
    assert svc.publisher.name == "dry-run"


def test_health_check_reports_stale_heartbeat(svc_root):
    clock = Clock(T0)
    svc = Service(svc_root, cfg(), publisher=SimulatedPublisher(), clock=clock, disk_usage=Disk(), runner=stub_runner(svc_root))
    assert health(svc_root)[0] is False
    svc.tick()
    import time
    h = json.loads((svc_root / "service" / "health.json").read_text())
    h["ts"] = time.time()
    (svc_root / "service" / "health.json").write_text(json.dumps(h))
    assert health(svc_root)[0] is True
    h["ts"] -= 1000
    (svc_root / "service" / "health.json").write_text(json.dumps(h))
    assert health(svc_root, 300)[0] is False


def test_launchd_plist_is_valid_restarts_automatically_and_embeds_no_secrets(tmp_path):
    p = launchd.write(tmp_path, dest=tmp_path / "x.plist", python="/usr/bin/python3")
    assert subprocess.run(["plutil", "-lint", str(p)], capture_output=True).returncode == 0
    import plistlib
    d = plistlib.loads(p.read_bytes())
    assert d["KeepAlive"] is True and d["RunAtLoad"] is True and d["ThrottleInterval"] >= 10
    assert d["ProgramArguments"][-2:] == ["service", "run"] and "secret" not in json.dumps(d).lower() and "token" not in json.dumps(d).lower()


def test_second_instance_cannot_run_concurrently(svc_root):
    from clipagent.service.daemon import acquire_lock
    Service(svc_root, cfg(), publisher=SimulatedPublisher())
    lock = acquire_lock(svc_root)
    import pytest
    with pytest.raises(SystemExit):
        acquire_lock(svc_root)
    lock.close()
