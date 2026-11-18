"""Unit tests for the /make-clips core (no models, no ffmpeg except where noted)."""
import json

import numpy as np
import pytest

from clipagent.clips import audio, ctype, events, passes, qa as clips_qa, reframe, render, review, source
from clipagent.clips.common import ClipsError, RightsMissing, read_json, write_json
from service_helpers import make_mp4

BASE = {"duration_min": 60, "aspect": 1.778, "letterboxed": False, "cut_rate_per_min": 1.0, "speech_ratio": 0.6, "words_per_speech_sec": 2.5, "speaker_changes_per_min": 1.0,
        "loud_per_min": 1.5, "laugh_per_min": 0.2, "music_change_per_min": 0.1, "face_ratio": 0.5, "mean_face_area": 0.02, "facecam_ratio": 0.0, "multi_face_ratio": 0.0,
        "chat_hits_per_10min": 0, "game_hits_per_10min": 0, "podcast_hits": 0, "interview_hits": 0}


# ------------------------------------------------------------------ content type
@pytest.mark.parametrize("over,expect", [
    ({"chat_hits_per_10min": 8, "facecam_ratio": 0.6, "game_hits_per_10min": 3}, "streamer"),
    ({"speech_ratio": 0.2, "game_hits_per_10min": 8, "cut_rate_per_min": 0.5}, "gaming"),
    ({"letterboxed": True, "cut_rate_per_min": 9, "music_change_per_min": 1, "speaker_changes_per_min": 3, "duration_min": 120, "loud_per_min": 0.2}, "movie"),
    ({"cut_rate_per_min": 8, "music_change_per_min": 1, "speaker_changes_per_min": 3, "duration_min": 40, "loud_per_min": 0.2, "facecam_ratio": 0.0}, "show"),
    ({"podcast_hits": 3, "speech_ratio": 0.9, "face_ratio": 0.9, "mean_face_area": 0.08, "loud_per_min": 0.1}, "podcast"),
    ({"interview_hits": 3, "speech_ratio": 0.9, "face_ratio": 0.9, "mean_face_area": 0.08, "multi_face_ratio": 0.6, "loud_per_min": 0.1}, "interview"),
])
def test_content_type_detection(over, expect):
    r = ctype.classify({**BASE, **over})
    assert r["content_type"] == expect and r["evidence"]


def test_ambiguous_source_is_flagged_low_confidence_not_guessed_silently():
    r = ctype.classify({**BASE, "speech_ratio": 0.45, "loud_per_min": 0.2, "cut_rate_per_min": 4, "duration_min": 5})
    assert r["low_confidence"] and "LOW CONFIDENCE" in " ".join(r["evidence"])


# ------------------------------------------------------------------ events
def test_cosine_and_word_edge_snapping_never_cut_a_word_in_half():
    assert events.cosine(["ghost", "huge"], ["ghost", "run"]) > 0.4 and events.cosine(["a"], []) == 0.0
    words = [(10.0, 10.5), (10.5, 11.0), (11.0, 14.0)]                   # last one is a stretched timing artifact (> 1.2 s): ignored
    assert events.snap_edge(10.7, "start", words) == 10.46               # inside a word -> keep the word whole (outward)
    assert events.snap_edge(10.7, "end", words) == 11.04
    assert events.snap_edge(12.0, "start", words) == 12.0                # artifact words never move a cut
    assert events.snap_edge(10.5, "start", words) == 10.5                # on a word boundary: untouched


def test_find_peaks_has_no_quota_flat_signal_has_none_and_many_equal_peaks_are_all_found():
    flat = np.full(200, 0.5, np.float32)
    assert events.find_peaks(flat) == []
    x = np.zeros(400, np.float32)
    for k in range(7):
        x[40 + k * 50] = 3.0
    assert len(events.find_peaks(x)) == 7                                # as many as the source honestly has, never a fixed count


def test_active_baseline_ignores_silence_so_shouts_stand_out():
    rng = np.random.default_rng(0)
    db = np.where(rng.random(2000) < 0.5, -54.0, -30.0) + rng.normal(0, 1, 2000)        # half silence, half speech
    db[1000:1008] = -10.0
    base = audio.active_baseline(db.astype(np.float32))
    assert -34 < base[500] < -26 and (db[1000:1008] - base[1004]).min() > 15


def test_robust_audio_events_find_silence_shout_impact_and_comic_pause():
    db = np.full(200, -30.0, np.float32)
    db[40:60] = -54.0                                                    # 5 s silence
    db[60:66] = -8.0                                                     # shout right after it
    ev = audio.robust_events(db)
    kinds = {e["type"] for e in ev}
    assert {"silence", "loud", "impact"} <= kinds
    assert any(e["type"] == "comic_pause" for e in audio.comic_pauses(ev))


def test_laughter_requires_rhythmic_non_speech():
    sr = 16000
    t = np.arange(4 * sr) / sr
    rng = np.random.default_rng(1)
    laugh = (rng.normal(0, 1, len(t)) * (0.5 + 0.5 * np.sin(2 * np.pi * 5.5 * t)) ** 2 * 0.3).astype(np.float32)
    x = np.concatenate([rng.normal(0, 0.001, 2 * sr).astype(np.float32), laugh, rng.normal(0, 0.001, 2 * sr).astype(np.float32)])
    mod = audio.modulation_series(x)
    assert audio.laughter_events(mod, [])                               # nobody speaking -> laughter-like
    spoken = [{"start": 2.0, "end": 6.0}]
    assert audio.laughter_events(mod, spoken) == []                      # same audio under transcribed words is speech, not laughter


# ------------------------------------------------------------------ reframing
def test_face_tracking_keyframes_deadband_and_fit_fallbacks():
    times = [i * 0.25 for i in range(40)]
    faces = [[[0.40 + 0.002 * i, 0.2, 0.1, 0.15, 0.9]] for i in range(40)]        # barely drifts
    path = reframe.subject_path(faces, times, 0, 10)
    p = reframe.plan(path, 1920, 1080, 0, 10)
    assert p["method"] == "face-track" and len(p["keyframes"]) <= 3
    moving = [[[0.2 + 0.015 * i, 0.2, 0.1, 0.15, 0.9]] for i in range(40)]
    assert len(reframe.plan(reframe.subject_path(moving, times, 0, 10), 1920, 1080, 0, 10)["keyframes"]) > 3
    wide = [[[0.05, 0.2, 0.1, 0.15, 0.9], [0.85, 0.2, 0.1, 0.15, 0.9]]] * 40            # two people far apart: one 9:16 window cannot hold them
    assert reframe.plan(reframe.subject_path(wide, times, 0, 10), 1920, 1080, 0, 10)["method"] == "fit"
    assert reframe.plan(reframe.subject_path([[]] * 40, times, 0, 10), 1920, 1080, 0, 10)["method"] == "fit"


def test_crop_expression_is_clamped_inside_the_frame():
    cw = reframe.window_width(1920, 1080)
    assert cw == 606
    e = reframe.crop_x_expr([(0.0, 0.0), (2.0, 1.0)], 1920, cw)
    assert e.startswith("0.0") and "clip((t-2.00)/0.6,0,1)" in e
    assert float(reframe.crop_x_expr([(0.0, 0.5)], 1920, cw)) == (1920 - cw) / 2


# ------------------------------------------------------------------ layout decisions + filter graphs
def ctx(**o):
    return {"src_w": 1920, "src_h": 1080, "bars": {"top_frac": 0.0, "bottom_frac": 0.0}, "faces": [], "times": [], "content_type": "movie", "impacts": [], **o}


def test_layout_decisions_and_graphs():
    assert render.decide_layout(ctx(), 0, 10, 5)["layout"] == "fit"
    assert render.decide_layout(ctx(src_w=1080, src_h=1920), 0, 10, 5)["layout"] == "fill"
    g = render.decide_layout(ctx(content_type="gaming", impacts=[5.2]), 0, 10, 5.0)
    assert g["layout"] == "fit" and g["effects"][0]["type"] == "punch_in" and g["effects"][0]["reason"]
    assert render.decide_layout(ctx(content_type="movie", impacts=[5.2]), 0, 10, 5.0)["effects"] == []       # no gimmicks on scene content
    times = [i * 0.25 for i in range(40)]
    faces = [[[0.45, 0.2, 0.1, 0.15, 0.9]]] * 40
    t = render.decide_layout(ctx(content_type="podcast", faces=faces, times=times), 0, 10, 5)
    assert t["layout"] == "face-track"
    lines, name = render.filter_graph(t, 1920, 1080, 10)
    assert name == "face-track" and "crop=w=606:h=1080" in lines[0] and "scale=1080:1920" in lines[0]
    lines, _ = render.filter_graph(render.decide_layout(ctx(bars={"top_frac": 0.12, "bottom_frac": 0.12}), 0, 10, 5), 1920, 1080, 10)
    assert lines[0].startswith("[0:v]crop=iw:") and "split=2" in lines[0]                                   # letterbox bars cropped out
    lines, _ = render.filter_graph(g, 1920, 1080, 10)
    assert "eval=frame" in "".join(lines)


# ------------------------------------------------------------------ source + rights
def test_movie_and_show_need_an_explicit_rights_status_and_nothing_is_assumed(tmp_path):
    v = make_mp4(tmp_path / "film.mp4", 1.0)
    with pytest.raises(RightsMissing):
        source.register(v, None, "movie", tmp_path)
    ok = source.register(v, "licensed: festival screener agreement", "movie", tmp_path)
    assert ok["rights"]["status"] == "licensed" and ok["source_id"] == source.register(v, None, "auto", tmp_path)["source_id"]      # idempotent, keeps rights
    (tmp_path / "talk.mp4").write_bytes(v.read_bytes() + b"\0")
    (tmp_path / "talk.mp4.rights.txt").write_text("permission: creator gave written permission\n")
    assert source.register(tmp_path / "talk.mp4", None, "show", tmp_path)["rights"]["status"] == "permission"
    with pytest.raises(RightsMissing):
        source.enforce_rights("show", None)
    source.enforce_rights("streamer", None)                                                                    # user-owned categories may proceed with a recorded note


# ------------------------------------------------------------------ review + passes
def cand(i, score, tags=("scream",), start=None, end=None, tight=None, text="what is that ghost"):
    start = 10.0 + 40 * i if start is None else start
    end = start + 14 if end is None else end
    ts, te = tight or (start + 5, start + 11)
    return {"id": f"C{i + 1:03d}", "start": start, "end": end, "tight_start": ts, "tight_end": te, "peak_t": start + 7, "runtime_sec": end - start, "score": score, "tags": list(tags),
            "evidence": {"audio": {"loud": 1, "laughter_like": 1}, "peak_components": {"energy": 2.0, "exclaim": 1.0, "laugh": 0.6, "pause": 0.5, "motion": 0.1, "turns": 0.0, "music": 0, "cuts": 0,
                                                                                       "onset": 1, }, "drivers": ["energy", "exclaim", "laugh"],
                         "transcript": f"I think it is empty. {text} number {i}!", "utterances": [], "speaker_changes": 0, "voices": [0], "cuts_inside": 0, "scene_changes_inside": 0, "max_motion": 0.2, "words": 12},
            "hook": "I think it is empty.", "setup": "I think it is empty.", "payoff": f"{text} number {i}!", "start_reason": "x", "end_reason": "y",
            "audio_evidence": ["loud x1"], "visual_evidence": "v"}


def plan_root(tmp_path, cands, ctype_="streamer", edits=None):
    sid = "sid000000001"
    d = tmp_path / "runs" / sid
    write_json(d / "candidates" / "ledger.json", {"source_id": sid, "content_type": ctype_, "candidates": cands, "passes": {}})
    taken = set()
    rvs = []
    for c in cands:
        rv = review.auto_review(c, ctype_, taken)
        rv.update((edits or {}).get(c["id"], {}))
        rvs.append(rv)
    write_json(d / "candidates" / "reviews.effective.json", rvs)
    return sid


def test_ranking_needs_three_honest_entries_and_is_never_padded(tmp_path):
    sid = plan_root(tmp_path, [cand(0, 0.9), cand(1, 0.8)])
    p = passes.build(sid, tmp_path)
    assert not [v for v in p["videos"] if v["format"] == "ranking"] and any("never padded" in w for w in p["ranking_none"])
    assert len([v for v in p["videos"] if v["format"] == "standalone"]) == 2                      # both still become standalones


def test_ranking_and_standalone_passes_with_recorded_reuse_and_countdown_order(tmp_path):
    sid = plan_root(tmp_path, [cand(i, 0.5 + 0.1 * i, text=f"unique words{chr(97 + i)}") for i in range(4)])
    p = passes.build(sid, tmp_path)
    rk = next(v for v in p["videos"] if v["format"] == "ranking")
    assert [m["rank"] for m in rk["moments"]] == [4, 3, 2, 1] and max(rk["moments"], key=lambda m: -m["rank"])["candidate_id"] == "C004"      # strongest is #1
    for v in p["videos"]:
        if v["format"] == "standalone":
            assert v["reuse"]["of"] == rk["id"] and "materially" in v["reuse"]["reason"] or "differs" in v["reuse"]["reason"]
    led = read_json(tmp_path / "runs" / sid / "candidates" / "recovery-ledger.json")
    assert sorted(r["candidate"] for r in led) == ["C001", "C002", "C003", "C004"]              # every candidate accounted for exactly once
    titles_ = [m["title"] for v in p["videos"] for m in v["moments"]]
    assert len(titles_) == len({t.lower() for t in titles_})                                     # no repeated title anywhere


def test_standalone_that_would_repeat_its_ranking_entry_is_not_made(tmp_path):
    cs = [cand(i, 0.6 + 0.05 * i, text=f"distinct phrase{chr(97 + i)}") for i in range(3)]
    cs[2].update(start=cs[2]["tight_start"], end=cs[2]["tight_end"])                              # full span == tight span
    sid = plan_root(tmp_path, cs)
    p = passes.build(sid, tmp_path)
    assert any("would repeat its ranking entry" in w for w in p["standalone_none"])
    assert len([v for v in p["videos"] if v["format"] == "standalone"]) == 2


def test_long_moments_are_kept_whole_runtime_is_only_metadata(tmp_path):
    long_ = cand(0, 0.9, start=100.0, end=100.0 + 200.0, tight=(150.0, 290.0))
    sid = plan_root(tmp_path, [long_, cand(1, 0.2)])
    p = passes.build(sid, tmp_path)
    s = next(v for v in p["videos"] if v["format"] == "standalone" and v["moments"][0]["candidate_id"] == "C001")
    assert s["runtime_sec"] == 200.0                                                              # a 3m20s scene is preserved; nothing trims it to a target


def test_auto_review_rejects_weak_filler_and_agent_reviews_win(tmp_path):
    weak = cand(0, 0.3)
    weak["evidence"]["audio"] = {}
    weak["evidence"]["peak_components"].update(energy=0.2, exclaim=0.0, laugh=0, pause=0)
    assert review.auto_review(weak, "streamer", set())["verdict"] == "reject"
    assert review.validate_review({"id": "C1", "verdict": "accept", "title": "Get It Off Me", "headline": "Get It Off Me Now", "theme": "Loudest Screams"}) == []
    assert review.validate_review({"id": "C1", "verdict": "accept", "title": "x", "headline": "Short", "theme": "t"})
    assert any("trim_reason" in e for e in review.validate_review({"id": "C1", "verdict": "accept", "title": "Get It Off", "headline": "Hallway Scare Now", "theme": "t", "start": 3.0}))
    sid = plan_root(tmp_path, [cand(0, 0.9), cand(1, 0.8), cand(2, 0.7)])
    write_json(tmp_path / "runs" / sid / "candidates" / "reviews.json", [{"id": "C002", "verdict": "reject", "reason": "agent saw nothing funny on screen"}])
    out = review.build_reviews(sid, tmp_path)
    assert out["agent"] == 1 and next(r for r in out["reviews"] if r["id"] == "C002")["reason"] == "agent saw nothing funny on screen"


def test_titles_quote_the_payoff_and_stay_within_limits():
    c = cand(0, 0.9, text="Why does it always explode")
    t = review.make_title(c, set())
    assert 2 <= len(t.split()) <= 5 and t == "I Think It Is Empty" or "Explode" in t or "Empty" in t
    h = review.make_headline(c, {t.lower()})
    assert 2 <= len(h.split()) <= 6 and h.lower() != t.lower()


# ------------------------------------------------------------------ qa decision
def test_qa_decision_allows_only_by_design_manual_items():
    ok = {"checks": [{"check": "a", "status": "pass", "detail": ""}, {"check": "faces_and_action_visible", "status": "manual_review", "detail": ""}]}
    assert clips_qa.decide(ok)[0]
    bad = {"checks": [{"check": "x", "status": "fail", "detail": "boom"}, {"check": "odd", "status": "manual_review", "detail": ""}]}
    ok2, why = clips_qa.decide(bad)
    assert not ok2 and len(why) == 2


# ------------------------------------------------------------------ source discovery: sourcevideo only, separate audio combined
def test_make_clips_without_arguments_scans_only_sourcevideo_and_combines_separate_audio(tmp_path):
    import subprocess
    (tmp_path / "sourcevideo" / "inbox").mkdir(parents=True)
    (tmp_path / "source_library").mkdir()
    (tmp_path / "source_library" / "loose.mp4").write_bytes(b"not for make-clips")                      # other project folders are never scanned
    v = make_mp4(tmp_path / "sourcevideo" / "show.mp4", 3.0)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(v), "-vn", "-c:a", "pcm_s16le", str(tmp_path / "sourcevideo" / "show.wav")], check=True)
    silent = tmp_path / "sourcevideo" / "inbox" / "b.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=2", "-an", "-c:v", "libx264", str(silent)], check=True)
    from clipagent.clips import pipeline
    found = pipeline.find_sources(None, tmp_path)
    names = sorted(p.name for p in found)
    assert names == ["b.mp4", "show.mp4"] and all("source_library" not in str(p) for p in found)
    muxed = next(p for p in found if p.name == "show.mp4")
    assert "_muxed" in str(muxed) and (tmp_path / "sourcevideo" / "show.mp4").exists()               # originals untouched, combined copy used
    assert pipeline.find_sources(None, tmp_path) == found                                            # idempotent: same combined file, no duplicates
    (tmp_path / "sourcevideo" / "bad.wav").write_bytes(b"x")
    long_wav = tmp_path / "sourcevideo" / "long.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=30", str(long_wav)], check=True)
    long_v = make_mp4(tmp_path / "sourcevideo" / "long.mp4", 3.0)
    with pytest.raises(ClipsError):
        source.mux_with_audio(long_v, long_wav, tmp_path)                                            # 27 s mismatch: refused, never guessed
