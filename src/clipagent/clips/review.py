"""Candidate review. Two interchangeable sources of reviews:

* the Claude agent (`/make-clips` skill): reads `candidates/review_pack/<id>/` (frames across the whole event, a contact sheet, the timed transcript,
  audio events, neighbours) and writes `candidates/reviews.json`; its verdicts always win;
* `auto_review`: a deterministic fallback that decides from measured evidence only (never from frames) and marks `visual_review: auto-metrics-only`,
  so the pipeline never blocks and tests are reproducible.

A review is {id, verdict: accept|reject, reason, theme, title (ranking entry, 2-5 words), headline (standalone, 2-6 words, different from the title),
treatment, standalone_only?, start?, end?, hook?, setup?, payoff?, visual_review}. Trims need a recorded reason.
"""
import re
import subprocess
from collections import Counter
from pathlib import Path

from .. import titles
from . import audio as audio_mod
from .common import ClipsError, read_json, run_dir, write_json

THEMES = {
    "streamer": {"argument": "Heated Arguments", "reveal": "Biggest Reveals", "scream": "Loudest Screams", "laugh": "Biggest Laugh Moments",
                 "comic-pause": "Best Comic Pauses", "banter": "Best Banter", "visual-chaos": "Wildest Chaos", "story": "Best Stories", "moment": "Standout Moments"},
}
THEMES["gaming"] = THEMES["streamer"]
THEMES["podcast"] = {**THEMES["streamer"], "scream": "Biggest Outbursts", "laugh": "Funniest Podcast Moments"}
THEMES["interview"] = {**THEMES["podcast"], "laugh": "Funniest Interview Moments"}
THEMES["movie"] = {**THEMES["streamer"], "scream": "Most Intense Outbursts", "laugh": "Funniest Scenes", "reveal": "Biggest Reveals", "comic-pause": "Best Tense Silences"}
THEMES["show"] = {**THEMES["movie"]}
PRIORITY = ["argument", "reveal", "scream", "laugh", "comic-pause", "banter", "visual-chaos", "story", "moment"]
DESCRIPTOR = {"scream": "Scream", "laugh": "Laugh Fit", "comic-pause": "Pause", "argument": "Argument", "banter": "Banter", "reveal": "Reveal",
              "visual-chaos": "Chaos", "story": "Story", "moment": "Moment"}
STOPWORDS = {"nothing", "check", "step", "completely", "something", "everything", "could", "would", "should", "think", "this", "that", "there", "here", "just", "like", "really", "going", "gonna", "want", "okay", "right", "know", "let", "get", "got", "oh"}


def primary_tag(tags: list[str]) -> str:
    return next((t for t in PRIORITY if t in tags), "moment")


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z']+", text)


def repeated_phrase(text: str, lo: int = 2, hi: int = 4) -> str | None:
    w = [x.lower() for x in _words(text)]
    for n in range(hi, lo - 1, -1):
        grams = Counter(tuple(w[i:i + n]) for i in range(len(w) - n + 1))
        g, c = next(iter(grams.most_common(1)), (None, 0))
        if g and c >= 2 and len(set(g)) > 1:
            return " ".join(g)
    return None


def _sentences(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"(?<=[.?!])\s+", text) if x.strip()]


def make_title(c: dict, taken: set[str]) -> str:
    """Ranking entry title (2-5 words) quoted from what is actually said at the payoff; never invented. Prefers a whole short sentence, then a repeated
    phrase, then the opening words."""
    pay = c.get("payoff") or c["evidence"]["transcript"]
    sents = [x for x in (_sentences(pay) + _sentences(c["evidence"]["transcript"])) if 2 <= len(_words(x)) <= 5]
    pick = sents[0] if sents else (repeated_phrase(pay) or " ".join(_words(pay)[:4]))
    w = _words(pick)[:5]
    if len(w) < 2:
        w = (w + _words(c["evidence"]["transcript"])[:3])[:3]
    for k in range(len(w), 1, -1):
        t = titles.normalize(" ".join(w[:k]))
        if t.lower() not in taken:
            return t
    return titles.normalize(f"{DESCRIPTOR[primary_tag(c['tags'])]} {c['id'][1:].lstrip('0') or '0'}x")


def make_headline(c: dict, taken: set[str]) -> str:
    """Standalone headline (2-6 words): a subject word from the scene (setup first, >= 5 letters) + what happens (from the tags). Truthful, different from the
    ranking title, and unique: later subject words are tried before a numeric suffix is added."""
    pool = _words(c.get("setup", "")) + _words(c["evidence"]["transcript"])
    nouns = []
    for w in pool:
        if len(w) >= 5 and w.lower() not in STOPWORDS and w.lower() not in nouns:
            nouns.append(w.lower())
    desc = DESCRIPTOR[primary_tag(c["tags"])]
    for noun in nouns or ["scene"]:
        h = titles.normalize(f"{noun} {desc}")
        if h.lower() not in taken:
            return h
    base = titles.normalize(f"{(nouns or ['scene'])[0]} {desc}")
    for k in range(2, 50):
        h = f"{base} {k}"
        if h.lower() not in taken:
            return h
    return base


def modalities(c: dict) -> set[str]:
    ev, d = c["evidence"], c["evidence"]["peak_components"]
    m = set()
    if any(k in ev["audio"] for k in ("loud", "impact", "laughter_like", "comic_pause")) or d["energy"] >= 1.0:
        m.add("audio")
    if d["exclaim"] >= 0.4:
        m.add("transcript")
    if d["motion"] >= 0.8 or ev["scene_changes_inside"]:
        m.add("visual")
    if ev["speaker_changes"] >= 3:
        m.add("dialogue")
    return m


def auto_review(c: dict, ctype: str, taken: set[str]) -> dict:
    mods = modalities(c)
    r = {"id": c["id"], "visual_review": "auto-metrics-only", "reviewer": "auto", "modalities": sorted(mods)}
    if len(mods) < 2:
        return {**r, "verdict": "reject", "reason": f"only {sorted(mods) or 'no'} evidence of a payoff; needs at least two independent kinds (audio, transcript, visual, dialogue)"}
    theme = THEMES[ctype][primary_tag(c["tags"])]
    title, head = make_title(c, taken), None
    taken.add(title.lower())
    head = make_headline(c, taken)
    taken.add(head.lower())
    return {**r, "verdict": "accept", "reason": f"payoff supported by {', '.join(sorted(mods))}", "theme": theme, "title": title, "headline": head,
            "treatment": "tightened_continuous", "hook": c["hook"], "setup": c["setup"], "payoff": c["payoff"]}


def validate_review(rv: dict) -> list[str]:
    errs = []
    if rv.get("verdict") not in ("accept", "reject"):
        errs.append(f"{rv.get('id')}: verdict must be accept or reject")
    if rv.get("verdict") == "accept":
        try:
            titles.validate_title(rv.get("title", ""))
            titles.validate_headline(rv.get("headline", ""))
        except titles.TitleError as exc:
            errs.append(f"{rv.get('id')}: {exc}")
        if not rv.get("theme"):
            errs.append(f"{rv.get('id')}: accepted candidates need a theme")
        if ("start" in rv or "end" in rv) and not rv.get("trim_reason"):
            errs.append(f"{rv.get('id')}: a changed start/end needs a recorded trim_reason")
    return errs


def build_reviews(source_id: str, root=None, agent_reviews: Path | None = None) -> dict:
    d = run_dir(source_id, root)
    led = read_json(d / "candidates" / "ledger.json")
    agent = {}
    p = Path(agent_reviews) if agent_reviews else d / "candidates" / "reviews.json"
    if p.is_file():
        for rv in read_json(p):
            errs = validate_review(rv)
            if errs:
                raise ClipsError("invalid review: " + "; ".join(errs))
            agent[rv["id"]] = {**rv, "reviewer": rv.get("reviewer", "claude-agent")}
    taken: set[str] = {rv["title"].lower() for rv in agent.values() if rv.get("title")} | {rv["headline"].lower() for rv in agent.values() if rv.get("headline")}
    out = []
    for c in led["candidates"]:
        out.append(agent.get(c["id"]) or auto_review(c, led["content_type"], taken))
    write_json(d / "candidates" / "reviews.effective.json", out)
    return {"reviews": out, "agent": len(agent), "auto": len(out) - len(agent)}


def review_pack(source_id: str, root=None, frames: int = 6) -> Path:
    """Frames across each whole event + contact sheet + timed transcript/audio/neighbour evidence, for the agent's visual and audio review."""
    d = run_dir(source_id, root)
    led = read_json(d / "candidates" / "ledger.json")
    src = Path(read_json(d / "source.json")["path"])
    tr = read_json(d / "transcripts" / "pass1.json") if (d / "transcripts" / "pass1.json").is_file() else {"segments": []}
    am = audio_mod.load_meta(d / "analysis" / "audio") if (d / "analysis" / "audio.json").is_file() else {"events": []}
    cands = led["candidates"]
    for i, c in enumerate(cands):
        pk = d / "candidates" / "review_pack" / c["id"]
        pk.mkdir(parents=True, exist_ok=True)
        ts = [c["start"] + (c["end"] - c["start"]) * k / (frames - 1) for k in range(frames)] + [c["peak_t"]]
        for k, t in enumerate(ts):
            out = pk / (f"frame_{k}.jpg" if k < frames else "frame_peak.jpg")
            if not out.exists():
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1", "-vf", "scale=480:-2", str(out)], capture_output=True)
        sheet = pk / "sheet.jpg"
        if not sheet.exists():
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{c['start']:.2f}", "-t", f"{max(c['end'] - c['start'], 0.5):.2f}", "-i", str(src), "-vf",
                            f"fps={frames / max(c['end'] - c['start'], 0.5):.4f},scale=320:-2,tile={frames}x1", "-frames:v", "1", str(sheet)], capture_output=True)
        words = [{"w": w["word"].strip(), "t": round(w["start"], 2)} for s in tr["segments"] if s["end"] > c["start"] and s["start"] < c["end"] for w in s["words"]
                 if c["start"] - 0.1 <= w["start"] <= c["end"] + 0.1]
        write_json(pk / "evidence.json", {
            "candidate": {k: c[k] for k in ("id", "start", "end", "tight_start", "tight_end", "peak_t", "runtime_sec", "tags", "score", "start_reason", "end_reason")},
            "timed_words": words, "transcript": c["evidence"]["transcript"], "audio_events": [e for e in am["events"] if e["t1"] > c["start"] and e["t0"] < c["end"]],
            "drivers": c["evidence"]["drivers"], "peak_components": c["evidence"]["peak_components"],
            "neighbours": {"previous": cands[i - 1]["id"] if i else None, "next": cands[i + 1]["id"] if i + 1 < len(cands) else None},
            "instructions": "View the frames and sheet, read the words against the audio events, then accept/reject; merge or trim only with a recorded reason."})
    return d / "candidates" / "review_pack"
