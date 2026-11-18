"""Shared helpers for the service tests: fake clock/disk, config builder, tiny real clips + manifests, stub make-clips runner."""
import hashlib
import json
import subprocess
from collections import namedtuple
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from clipagent.service import config as svc_config

PT = ZoneInfo("America/Los_Angeles")
Usage = namedtuple("Usage", "total used free")
GB = 10 ** 9


def pt(y, m, d, h=0, mi=0) -> float:
    return datetime(y, m, d, h, mi, tzinfo=PT).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, sec):
        self.t += sec


class Disk:
    def __init__(self, total=1000 * GB, free=600 * GB):
        self.total, self.free = total, free

    def __call__(self, path):
        return Usage(self.total, self.total - self.free, self.free)


def cfg(**over):
    base = {"dry_run": False, "stable_seconds": 0, "gate": {"visual_safety": {"mode": "metrics_only_allowed", "command": None}},
            "posting": {"daily_ceiling": 5, "min_spacing_minutes": 60}, "retry": {"max_attempts": 3, "base_seconds": 10, "cap_seconds": 100},
            "metadata": {"streamer": "TestStreamer", "game_or_topic": "Haunted House Game", "event": None}}
    return svc_config.deep_merge(svc_config.deep_merge(svc_config.DEFAULTS, base), over)


def make_mp4(path: Path, seconds: float = 2.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=1080x1920:rate=30:duration={seconds}", "-f", "lavfi", "-i",
                        f"sine=frequency=440:duration={seconds}", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                        "-movflags", "+faststart", str(path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return path


QA_NAMES = ["resolution_1080x1920", "aspect_9_16", "h264_video", "aac_audio", "faststart", "av_sync_stream_durations", "no_black_frames", "no_frozen_frames",
            "no_clipped_audio", "audio_present_not_silent", "caption_accuracy", "offensive_written_captions", "onscreen_text_safe", "setup_and_payoff_present",
            "truthful_edit", "subject_tracking_keeps_face", "not_a_duplicate_output", "captions_inside_safe_area"]


def make_clip(root: Path, sid: str, source_sha: str, pid="S01", name="hallway-scream", start=10.0, end=12.0, qa_fail: str | None = None, headline="Hallway Scream",
              fmt="standalone", visual_review="auto-metrics-only", approved=True) -> dict:
    loc = Path("approved") / ("rankings" if fmt == "ranking" else "standalone") / sid / f"{pid}-{name}"
    mp4 = make_mp4(root / loc / f"{pid}-{name}.mp4", end - start)
    checks = [{"check": n, "status": "fail" if n == qa_fail else "pass", "detail": ""} for n in QA_NAMES]
    man = {"output": mp4.name, "source_id": sid, "source_file": "stream.mp4", "source_sha256": source_sha, "content_type": "streamer", "format": fmt, "project_id": pid,
           "theme": "Loudest Screams" if fmt == "ranking" else None, "headline": None if fmt == "ranking" else headline,
           "candidate_ids": ["C001"], "source_timecodes": [{"rank": 1, "title": headline, "start": start, "end": end, "peak_t": start + 0.8, "candidate_id": "C001"}],
           "hook": ["Oh no"], "setup": ["I think this hallway is empty"], "payoff": ["Oh no no no get it off me"], "final_runtime_sec": round(end - start, 2),
           "transcript_confidence": [{"rank": 1, "decode_agreement": 0.9}], "visual_evidence": [{"rank": 1, "visual_review": visual_review}],
           "editorial": [{"rank": 1, "candidate_id": "C001", "candidate_score": 0.9, "tags": ["scream", "laugh"], "drivers": ["energy", "exclaim", "laugh"]}],
           "qa": {"approved": approved, "verdict": "pass", "reasons": [] if approved else ["simulated QA failure"], "checks": checks, "sha256": hashlib.sha256(mp4.read_bytes()).hexdigest()},
           "rights": {"status": "owned", "detail": "owned: test"}, "rights_note": None, "location": str(loc),
           "status": "ready_for_draft_transfer" if approved else "needs_review"}
    mdir = root / "runs" / sid / "manifests"
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / f"{pid}-{name}.json").write_text(json.dumps(man))
    for extra, body in (("manifest.json", json.dumps(man)), ("qa.json", "{}"), ("caption.txt", "x"), ("cover.jpg", "x")):
        (root / loc / extra).write_text(body)
    return man


def stub_runner(root: Path, clips=1, **clip_kw):
    """A make-clips stand-in: analysis/passes succeed; the final stage writes `clips` real clip files + manifests."""
    calls = []

    def run(video, stage, ct="auto", rights=None):
        calls.append(stage)
        if stage == "qa":
            sha = hashlib.sha256(Path(video).read_bytes()).hexdigest()
            for i in range(clips):
                make_clip(root, sha[:12], sha, pid=f"S{i + 1:02d}", name=f"clip-{i + 1}", start=10.0 + 30 * i, end=12.0 + 30 * i, headline=f"Hallway Scream {chr(65 + i)}", **clip_kw)
        return {"returncode": 0, "stdout": "", "stderr": ""}
    run.calls = calls
    return run
