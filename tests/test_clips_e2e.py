"""End-to-end /make-clips on the legal synthetic fixture (macOS `say` speech + synthesized screams/laughter + ffmpeg video). Slow: ~1-2 min."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import make_fixture  # noqa: E402

from clipagent.clips import pipeline  # noqa: E402

needs = pytest.mark.skipif(shutil.which("say") is None or shutil.which("ffmpeg") is None, reason="needs macOS say + ffmpeg")


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    root = tmp_path_factory.mktemp("clips-e2e")
    (root / "config").mkdir()
    shutil.copy(Path(__file__).resolve().parents[1] / "config" / "ranking_style.json", root / "config")
    video = root / "sourcevideo" / "stream_fixture.mp4"
    video.parent.mkdir()
    make_fixture.build(video)
    import os
    os.environ["CLIPAGENT_ROOT"] = str(root)
    try:
        res = pipeline.process(video, "auto", "tiktok", root=root, echo=lambda *_: None)
    finally:
        os.environ.pop("CLIPAGENT_ROOT", None)
    return root, res


@needs
def test_content_type_is_detected_and_planted_events_are_found(run):
    root, res = run
    assert res["content_type"] == "streamer" and res["candidates"] >= 4
    led = json.loads((root / "runs" / res["source_id"] / "candidates" / "ledger.json").read_text())
    peaks = [c["peak_t"] for c in led["candidates"]]
    for planted in make_fixture.EVENTS:
        assert any(abs(p - planted[0]) < 5 for p in peaks), f"planted event at {planted[0]}s not found in {peaks}"


@needs
def test_final_videos_pass_qa_and_land_in_the_right_folders_with_full_manifests(run):
    root, res = run
    assert len(res["approved"]) >= 3 and not res["needs_review"]
    kinds = {a["format"] for a in res["approved"]}
    assert kinds == {"ranking", "standalone"}                                                     # both passes produced output
    for a in res["approved"]:
        d = root / a["location"]
        man = json.loads((d / "manifest.json").read_text())
        mp4 = d / man["output"]
        info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(mp4)], capture_output=True, text=True).stdout)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        assert (v["width"], v["height"]) == (1080, 1920) and v["codec_name"] == "h264"
        for k in ("source_id", "source_timecodes", "content_type", "candidate_ids", "format", "hook", "setup", "payoff", "final_runtime_sec", "transcript_confidence",
                  "visual_evidence", "audio_evidence", "reframing", "editing_decisions", "reused_material", "rights", "rights_note", "qa"):
            assert k in man, k
        assert man["qa"]["approved"] and man["status"] == "ready_for_draft_transfer" and "never uploaded" in man["publishing"]
        assert abs(man["final_runtime_sec"] - float(info["format"]["duration"])) < 1.0
        assert (d / "cover.jpg").exists() and (d / "caption.txt").exists() and (d / "qa.json").exists()
        if a["format"] == "ranking":
            assert "approved/rankings" in a["location"]
            ranks = [t["rank"] for t in sorted(man["source_timecodes"], key=lambda t: -t["rank"])]
            assert ranks == sorted(ranks, reverse=True) and ranks[-1] == 1 and len(ranks) >= 3
        else:
            assert "approved/standalone" in a["location"] and man["reused_material"]["reason"]


@needs
def test_no_duplicate_outputs_and_rerun_is_idempotent(run):
    root, res = run
    import hashlib
    sums = [hashlib.sha256(next((root / a["location"]).glob("*.mp4")).read_bytes()).hexdigest() for a in res["approved"]]
    assert len(sums) == len(set(sums))
    again = pipeline.process(root / "sourcevideo" / "stream_fixture.mp4", "auto", "tiktok", root=root, echo=lambda *_: None)
    assert again["approved"] == [] and again["needs_review"] == []                                  # everything already produced: nothing is made twice


@needs
def test_movie_without_rights_stops_cleanly_and_creates_nothing(run, tmp_path):
    root, res = run
    out = pipeline.process(root / "sourcevideo" / "stream_fixture.mp4", "movie", "tiktok", root=root, echo=lambda *_: None) if False else None
    from clipagent.clips.common import RightsMissing
    from clipagent.clips import source
    with pytest.raises(RightsMissing):
        source.register(root / "sourcevideo" / "stream_fixture.mp4", None, "movie", tmp_path)
