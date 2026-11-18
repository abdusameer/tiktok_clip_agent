"""Signal fusion and event recovery: from whole-source evidence to candidate events.

1. A 0.5 s timeline of interest is fused from: loudness relative to the LOCAL baseline (yelling), onsets, laughter-like bursts, comic pauses,
   transcript cues (exclamations, repeated words, profanity, laughter words), visual motion spikes, speaker turn-taking, score/music changes and
   cutting rate. Weights depend on the content type. Shots are INPUTS only.
2. Peaks of interest become seeds. Each seed grows backwards (setup: the utterances that lead into it) and forwards (escalation, payoff and
   reaction) until the same event stops: boundaries are silence gaps, speaker changes after a pause, topic (lexical) shifts and real scene
   changes (a cut whose before/after colour histograms differ). Boundaries never come from a duration.
3. Seeds whose events overlap or touch with no boundary between them are one event.

Length is recorded as metadata and plays no role in scoring or selection.
"""
import math
import re
from collections import Counter

import numpy as np

from .. import captions as captions_mod
from . import audio as audio_mod
from .common import read_json, run_dir, write_json

GRID = 0.5
WEIGHTS = {
    "streamer": {"energy": 1.0, "onset": 0.4, "exclaim": 0.8, "laugh": 0.6, "pause": 0.7, "motion": 0.2, "turns": 0.2, "music": 0.0, "cuts": 0.0},
    "gaming": {"energy": 0.9, "onset": 0.6, "exclaim": 0.5, "laugh": 0.3, "pause": 0.4, "motion": 0.8, "turns": 0.1, "music": 0.0, "cuts": 0.0},
    "movie": {"energy": 0.5, "onset": 0.5, "exclaim": 0.3, "laugh": 0.2, "pause": 0.9, "motion": 0.4, "turns": 0.6, "music": 0.5, "cuts": 0.4},
    "show": {"energy": 0.5, "onset": 0.5, "exclaim": 0.3, "laugh": 0.4, "pause": 0.9, "motion": 0.4, "turns": 0.6, "music": 0.5, "cuts": 0.4},
    "podcast": {"energy": 0.4, "onset": 0.3, "exclaim": 0.6, "laugh": 1.0, "pause": 0.9, "motion": 0.0, "turns": 0.5, "music": 0.0, "cuts": 0.0},
    "interview": {"energy": 0.4, "onset": 0.3, "exclaim": 0.6, "laugh": 1.0, "pause": 0.9, "motion": 0.0, "turns": 0.5, "music": 0.0, "cuts": 0.0},
}
SCENE_MODE = {"movie", "show"}                    # complete scenes: events extend to the surrounding scene boundaries
STOP = set("a an the and or but so if then to of in on at for with is are was were be been it its this that these those i you he she we they me my your his her our their "
           "do does did have has had not no yes just very really like okay ok oh uh um yeah".split())
EXCLAIM = re.compile(r"\b(oh my god|omg|wtf|what the|holy|no way|get (it|him|her) off|help|run|are you kidding|shut up|let'?s go|what is that|why|noo+|nooo+|ahh+|"
                     r"he+lp|dead|dying|insane|crazy|unbelievable|i can'?t|stop)\b", re.I)
LAUGH = re.compile(r"\b(ha(ha)+|lol|lmao|hehe|laughing|bruh)\b", re.I)
PROF = re.compile(r"\b(fuck\w*|shit\w*|damn|hell|ass(hole)?|bitch|crap|bastard)\b", re.I)


# ----------------------------------------------------------------------------- text utilities
def tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z']+", text.lower()) if w not in STOP and len(w) > 2]


def cosine(a: list[str], b: list[str]) -> float:
    ca, cb = Counter(a), Counter(b)
    if not ca or not cb:
        return 0.0
    dot = sum(ca[k] * cb.get(k, 0) for k in ca)
    return dot / (math.sqrt(sum(v * v for v in ca.values())) * math.sqrt(sum(v * v for v in cb.values())) + 1e-9)


def refine_words(segments: list[dict], db: np.ndarray, hop: float) -> None:
    """Whisper word times can stretch over silence ('Oh' reported 0.6 s before it is heard). Pull word edges onto the first/last frame that is
    actually louder than the local noise floor. Edits the segments in place."""
    for s in segments:
        for w in s.get("words", []):
            a, b = int(w["start"] / hop), max(int(w["end"] / hop), int(w["start"] / hop) + 1)
            lo, hi = max(a - 4, 0), min(b + 4, len(db))
            if hi - lo < 3:
                continue
            floor = float(np.percentile(db[lo:hi], 15))
            loud = np.where(db[a:b] > floor + 10)[0] if b > a else []
            if len(loud) and (w["end"] - w["start"]) > 0.45:
                w["start"] = round(max(w["start"], (a + int(loud[0])) * hop), 3)
                w["end"] = round(min(w["end"], (a + int(loud[-1]) + 1) * hop + 0.05), 3)


def _word_db(db, hop, w) -> float:
    if db is None or not len(db):
        return 0.0
    a = min(int(w["start"] / hop), len(db) - 1)
    return float(db[a:max(int(w["end"] / hop), a + 1)].mean())


def utterances(segments: list[dict], labels: list, db=None, hop: float = 0.25) -> list[dict]:
    """Sentences from word timestamps. A new utterance starts at sentence punctuation, a >= 0.7 s gap, or a >= 12 dB jump in loudness (a shout
    starting inside an unpunctuated run of words). Each carries its speaker-proxy label."""
    out, cur = [], []
    for sid, s in enumerate(segments):
        for w in s.get("words", []):
            if cur and (w["start"] - cur[-1]["end"] >= 0.7 or cur[-1]["word"].strip()[-1:] in ".?!" or _word_db(db, hop, w) - _word_db(db, hop, cur[-1]) >= 12):
                out.append(cur)
                cur = []
            cur.append({**w, "seg": sid})
        if cur and s is segments[-1]:
            out.append(cur)
    if cur and (not out or cur is not out[-1]):
        out.append(cur)
    res = []
    for i, ws in enumerate(out):
        text = " ".join(w["word"].strip() for w in ws).strip()
        lab = labels[ws[0]["seg"]] if ws[0]["seg"] < len(labels) else None
        res.append({"id": i, "start": ws[0]["start"], "end": ws[-1]["end"], "text": text, "speaker": lab, "tokens": tokens(text), "words": ws})
    return res


# ----------------------------------------------------------------------------- timeline
def _pool(x: np.ndarray, factor: int) -> np.ndarray:
    n = len(x) // factor * factor
    return x[:n].reshape(-1, factor).max(axis=1) if n else x[:0]


def rolling_median(x: np.ndarray, win: int) -> np.ndarray:
    pad = np.pad(x, (win // 2, win // 2), mode="edge")
    return np.median(np.lib.stride_tricks.sliding_window_view(pad, win), axis=1)[: len(x)]


def build_timeline(ctx: dict) -> dict:
    dur = ctx["duration"]
    n = int(math.ceil(dur / GRID))
    comps = {k: np.zeros(n, np.float32) for k in WEIGHTS["streamer"]}
    at = lambda t: min(max(int(t / GRID), 0), n - 1)
    if ctx.get("series"):
        db = _pool(ctx["series"]["db"], int(GRID / audio_mod.HOP))
        db = np.pad(db, (0, max(n - len(db), 0)), constant_values=float(db.min()) if len(db) else -80)[:n]
        base = audio_mod.active_baseline(db)
        comps["energy"] = np.clip((db - base) / 8.0, 0, 3.0)                  # +8 dB over the typical ACTIVE level = 1.0; silence never lowers the baseline
        comps["onset"] = np.clip(np.r_[0, np.diff(db)] / 12.0, 0, 3) * (db > base + 6)
        ctx["db_grid"] = db
    for e in ctx["audio_events"]:
        if e["type"] == "laughter_like":
            comps["laugh"][at(e["t0"]):at(e["t1"]) + 1] += min(e["strength"] / 10.0, 2.0)
        elif e["type"] == "comic_pause":
            comps["pause"][at(e["t1"])] += min(e["strength"], 4.0) / 2.0
        elif e["type"] == "music_change":
            comps["music"][at(e["t0"])] += min(e["strength"] / 400.0, 1.5)
    for u in ctx["utterances"]:
        hits = len(EXCLAIM.findall(u["text"])) + 1.5 * len(PROF.findall(u["text"])) + 1.5 * len(LAUGH.findall(u["text"])) + 0.5 * u["text"].count("!")
        words = [w["word"].strip(" ,.!?").lower() for w in u["words"]]
        run = 1
        for a, b in zip(words, words[1:]):                                   # "no no no no": repeated word runs
            run = run + 1 if a == b else 1
            if run >= 3:
                hits += 0.7
        if hits:
            comps["exclaim"][at(u["start"]):at(u["end"]) + 1] += min(hits, 4.0) / 2.0
    vis = ctx.get("visual_arrays")
    if vis is not None and len(vis["motion"]) > 3:
        hz = ctx["visual_hz"]
        m = vis["motion"].astype(np.float32)
        z = np.clip((m - np.median(m)) / (np.std(m) + 1e-6), 0, 4) / 2.0
        step = max(int(round(hz * GRID)), 1)
        zz = _pool(z, step)
        comps["motion"][: min(n, len(zz))] = zz[:n]
    for c in ctx["speaker_changes"]:
        comps["turns"][at(c["t"])] += 0.8
    for c in ctx["visual_cuts"]:
        comps["cuts"][at(c["t"])] += 0.5
    w = WEIGHTS[ctx["content_type"]]
    raw = sum(comps[k] * w[k] for k in comps)
    k = np.array([0.25, 0.5, 0.25], np.float32)
    interest = np.convolve(np.convolve(raw, k, "same"), k, "same")
    return {"n": n, "comps": comps, "interest": interest, "raw": raw}


def find_peaks(interest: np.ndarray, sep_bins: int = 4) -> list[int]:
    if len(interest) < 5 or float(interest.max()) <= 0:
        return []
    med = float(np.median(interest))
    mad = float(np.median(np.abs(interest - med))) * 1.4826
    thr = max(med + 5 * mad, 0.30 * float(interest.max()), 1.0)       # robust to a few big peaks; no percentile, so no fixed event quota
    peaks = []
    for i in range(1, len(interest) - 1):
        if interest[i] >= thr and interest[i] >= interest[i - 1] and interest[i] > interest[i + 1]:
            lo, hi = max(0, i - sep_bins), min(len(interest), i + sep_bins + 1)
            if interest[i] >= interest[lo:hi].max():
                peaks.append(i)
    return peaks


# ----------------------------------------------------------------------------- boundaries
def boundary_list(ctx: dict) -> list[dict]:
    out = []
    U = ctx["utterances"]
    for i in range(len(U) - 1):
        a, b = U[i], U[i + 1]
        gap = b["start"] - a["end"]
        if gap < 0.5:
            continue
        s = min(gap, 4.0) * 0.6
        left = [t for u in U[max(0, i - 2):i + 1] for t in u["tokens"]]
        right = [t for u in U[i + 1:i + 4] for t in u["tokens"]]
        if cosine(left, right) < 0.06:
            s += 0.6
        if a["speaker"] is not None and b["speaker"] is not None and a["speaker"] != b["speaker"] and gap >= 1.5:
            s += 0.4
        out.append({"t": (a["end"] + b["start"]) / 2, "strength": round(s, 2), "kind": "pause/topic", "gap": round(gap, 2)})
    hist, t = ctx.get("hist"), ctx.get("visual_t")
    for c in ctx["visual_cuts"]:
        cont = c.get("continuity")
        if cont is None and hist is not None and t is not None and len(t) > 3:
            i = int(np.searchsorted(t, c["t"]))
            if 1 <= i < len(t) - 1:
                before, after = hist[i - 1].astype(np.float32), hist[min(i + 1, len(t) - 1)].astype(np.float32)
                cont = float(np.minimum(before, after).sum() / max(before.sum(), 1e-6))       # histogram intersection 0..1
        c["continuity"] = round(cont, 3) if cont is not None else None
        real = cont is not None and cont < 0.6
        out.append({"t": c["t"], "strength": 1.2 if real else 0.15, "kind": "scene-change" if real else "camera-angle", "continuity": c["continuity"]})
    for e in ctx["audio_events"]:
        if e["type"] == "silence" and e["t1"] - e["t0"] >= 1.0:
            out.append({"t": (e["t0"] + e["t1"]) / 2, "strength": min(e["t1"] - e["t0"], 4.0) * 0.5, "kind": "silence"})
    return sorted(out, key=lambda b: b["t"])


def _prev_b(bs: list[dict], t: float, min_strength: float):
    for b in reversed(bs):
        if b["t"] < t and b["strength"] >= min_strength:
            return b
    return None


def _next_b(bs: list[dict], t: float, min_strength: float):
    for b in bs:
        if b["t"] > t and b["strength"] >= min_strength:
            return b
    return None


# ----------------------------------------------------------------------------- expansion
def _utt_at(U: list[dict], t: float) -> int | None:
    for u in U:
        if u["start"] - 0.05 <= t <= u["end"] + 0.05:
            return u["id"]
    return None


def grow(ctx: dict, tl: dict, peak: int) -> dict:
    """Grow one seed into an event. Returns start/end plus the tight (build-up to reaction) span and the reasons for each edge."""
    U, bs, dur = ctx["utterances"], ctx["boundaries"], ctx["duration"]
    interest = tl["interest"]
    pk = float(interest[peak])
    t_peak = peak * GRID
    i0 = peak
    while i0 > 0 and interest[i0 - 1] >= 0.35 * pk:                       # build-up: interest still above 35 % of the peak
        i0 -= 1
    i1 = peak
    quiet = 0
    while i1 < len(interest) - 1 and quiet < 3:                           # payoff + reaction tail: stop after 1.5 s below 30 % of the peak
        i1 += 1
        quiet = quiet + 1 if interest[i1] < 0.30 * pk else 0
    i1 = max(peak, i1 - quiet)
    t_build, t_end = i0 * GRID, (i1 + 1) * GRID
    scene_mode = ctx["content_type"] in SCENE_MODE
    # tight span: whole utterances touching the build-up .. reaction
    k0 = _utt_at(U, t_build)
    tight_start = U[k0]["start"] if k0 is not None else t_build
    k1 = _utt_at(U, t_end)
    tight_end = max(t_end, U[k1]["end"]) if k1 is not None else t_end
    start, start_reason = tight_start, "build-up onset"
    # setup: the utterances that lead in (the immediately preceding one when speech is continuous, further ones while the same exchange continues)
    pause_before = any(e["type"] == "comic_pause" and abs(e["t1"] - t_build) <= 1.5 for e in ctx["audio_events"])    # a held beat before the payoff belongs to the joke
    first = next((u["id"] for u in U if u["end"] > tight_start - 1e-6), None)
    if first is not None:
        j, included = first - 1, [first]
        while j >= 0:
            gap = U[included[0]]["start"] - U[j]["end"]
            bnd = _prev_b(bs, U[included[0]]["start"], 1.0)
            crossing = bnd is not None and U[j]["end"] <= bnd["t"] <= U[included[0]]["start"]
            limit = 4.5 if (len(included) == 1 and pause_before) else 3.0
            if crossing and not (len(included) == 1 and gap <= limit and bnd["kind"] != "scene-change"):
                break
            if gap > limit:
                break
            if len(included) >= 2 and not scene_mode:
                joined = [t for u in U if u["id"] in included for t in u["tokens"]]
                if cosine(U[j]["tokens"], joined) < 0.12 or gap > 1.2:
                    break
            if len(included) >= 12 and not scene_mode:                      # an exchange this long without a topic shift is a conversation: the agent reviews it as a scene
                break
            included.insert(0, j)
            j -= 1
        if len(included) > 1 or included[0] != first:
            start, start_reason = U[included[0]]["start"], f"setup: {len(included)} utterance(s) leading in"
    if scene_mode:
        sb = _prev_b(bs, t_build, 1.0)
        if sb is not None and sb["t"] < start:
            start, start_reason = sb["t"] + 0.1, f"scene start ({sb['kind']})"
    end, end_reason = tight_end, "payoff + reaction decay"
    nb = _next_b(bs, t_end, 1.0)
    if scene_mode and nb is not None:
        end, end_reason = max(end, nb["t"] - 0.1), f"scene end ({nb['kind']})"
    else:                                                                   # continuing speech that is part of the reaction (same topic, small gap)
        last = _utt_at(U, end)
        k = (last + 1) if last is not None else next((u["id"] for u in U if u["start"] >= end), None)
        while k is not None and k < len(U):
            gap = U[k]["start"] - end
            if gap > 1.2 or (nb is not None and U[k]["start"] > nb["t"]) or cosine(U[k]["tokens"], [t for u in U[max(0, k - 3):k] for t in u["tokens"]]) < 0.1 and gap > 0.5:
                break
            if k > 0 and U[k]["speaker"] is not None and U[k - 1]["speaker"] is not None and U[k]["speaker"] != U[k - 1]["speaker"] and gap > 0.6:
                break
            end = U[k]["end"]
            end_reason = "reaction continues (same exchange)"
            k += 1
    start, end = max(0.0, start - 0.12), min(dur, end + 0.30)                # breathing room that never lands inside a word (utterance edges are word edges)
    tight_start, tight_end = max(0.0, max(tight_start - 0.12, start)), min(dur, min(tight_end + 0.30, end))
    wd = ctx["words_flat"]
    start, end = snap_edge(start, "start", wd), snap_edge(end, "end", wd)
    tight_start, tight_end = max(snap_edge(tight_start, "start", wd), start), min(snap_edge(tight_end, "end", wd), end)
    return {"peak_t": t_peak, "peak_interest": pk, "start": round(start, 2), "end": round(end, 2), "tight_start": round(tight_start, 2),
            "tight_end": round(tight_end, 2), "start_reason": start_reason, "end_reason": end_reason, "build_t": round(t_build, 2)}


MAX_WORD_SEC = 1.2                                   # a "word" longer than this is a timing artifact (stretched over silence): nothing can be cut in half


def snap_edge(t: float, kind: str, words: list[tuple[float, float]], margin: float = 0.05, max_move: float = 0.7) -> float:
    """Never cut inside a spoken word: move the edge outward to the word's edge (keeping the word whole) when that is a small move, else inward."""
    for a, b in words:
        if b - a <= MAX_WORD_SEC and a + margin < t < b - margin:
            out, inn = ((a - 0.04, b + 0.02) if kind == "start" else (b + 0.04, a - 0.02))
            return round(out if abs(out - t) <= max_move else inn, 2)
    return t


def merge_events(evs: list[dict]) -> list[dict]:
    evs = sorted(evs, key=lambda e: e["start"])
    out = []
    for e in evs:
        if out and e["start"] <= out[-1]["end"] + 0.5:
            m = out[-1]
            m["end"] = max(m["end"], e["end"])
            m["tight_end"] = max(m["tight_end"], e["tight_end"])
            m["seeds"].append({"t": e["peak_t"], "interest": e["peak_interest"]})
            if e["peak_interest"] > m["peak_interest"]:
                m["peak_t"], m["peak_interest"], m["tight_start"] = e["peak_t"], e["peak_interest"], min(m["tight_start"], e["tight_start"])
            m["end_reason"] = e["end_reason"]
        else:
            out.append({**e, "seeds": [{"t": e["peak_t"], "interest": e["peak_interest"]}]})
    return out


# ----------------------------------------------------------------------------- evidence + tags
def evidence_for(ctx: dict, tl: dict, ev: dict) -> dict:
    a, b, pk = ev["start"], ev["end"], ev["peak_t"]
    kinds = audio_mod.events_in(ctx["audio_events"], a, b)
    counts = Counter(e["type"] for e in kinds)
    texts = [u for u in ctx["utterances"] if u["end"] > a and u["start"] < b]
    i, j = int(a / GRID), int(math.ceil(b / GRID))
    comp_peak = {k: round(float(v[max(0, int(pk / GRID) - 2):int(pk / GRID) + 3].max()), 2) for k, v in tl["comps"].items()}
    top = [k for k, v in sorted(comp_peak.items(), key=lambda kv: -kv[1] * WEIGHTS[ctx["content_type"]][kv[0]]) if v * WEIGHTS[ctx["content_type"]][k] > 0.25][:4]
    changes = [c for c in ctx["speaker_changes"] if a <= c["t"] <= b]
    cuts = [c for c in ctx["visual_cuts"] if a < c["t"] < b]
    real_cuts = [c for c in cuts if (c.get("continuity") is not None and c["continuity"] < 0.6)]
    return {"audio": dict(counts), "peak_components": comp_peak, "drivers": top,
            "transcript": " ".join(u["text"] for u in texts), "utterances": [u["id"] for u in texts],
            "speaker_changes": len(changes), "voices": sorted({u["speaker"] for u in texts if u["speaker"] is not None}),
            "cuts_inside": len(cuts), "scene_changes_inside": len(real_cuts), "max_motion": round(float(tl["comps"]["motion"][i:j].max()), 2) if j > i else 0.0,
            "words": sum(len(u["words"]) for u in texts)}


def tags_for(ev: dict, evd: dict, ctype: str) -> list[str]:
    t, c, d = [], evd["audio"], evd["peak_components"]
    if d["energy"] >= 1.2 and (d["exclaim"] >= 0.4 or c.get("loud")):
        t.append("scream")
    if c.get("laughter_like") or d["laugh"] >= 0.5:
        t.append("laugh")
    if c.get("comic_pause") or d["pause"] >= 0.5:
        t.append("comic-pause")
    if evd["speaker_changes"] >= 3 and d["energy"] >= 0.6:
        t.append("argument")
    elif evd["speaker_changes"] >= 3:
        t.append("banter")
    if d["motion"] >= 0.8:
        t.append("visual-chaos")
    if ctype in SCENE_MODE and (c.get("music_change") or d["music"] >= 0.3) and (c.get("silence") or d["pause"] >= 0.3):
        t.append("reveal")
    if not t and evd["words"] >= 25:
        t.append("story")
    return t or ["moment"]


def to_candidates(ctx: dict, tl: dict, merged: list[dict]) -> list[dict]:
    peaks = np.array([e["peak_interest"] for e in merged]) if merged else np.array([1.0])
    top = float(peaks.max()) or 1.0
    out = []
    for n, ev in enumerate(sorted(merged, key=lambda e: e["start"]), 1):
        evd = evidence_for(ctx, tl, ev)
        kinds = sum(1 for k in ("energy", "laugh", "pause", "exclaim", "motion", "turns", "music") if evd["peak_components"].get(k, 0) * WEIGHTS[ctx["content_type"]][k] > 0.25)
        score = round(0.7 * ev["peak_interest"] / top + 0.3 * min(kinds / 4.0, 1.0), 3)
        U = [u for u in ctx["utterances"] if u["id"] in evd["utterances"]]
        near = [u for u in U if u["start"] <= ev["peak_t"] + 2.5 and u["end"] >= ev["peak_t"] - 1.0]      # what is said AT the peak is the payoff
        if not near and U:
            near = [min(U, key=lambda u: abs((u["start"] + u["end"]) / 2 - ev["peak_t"]))]
        first_near = near[0]["id"] if near else None
        pre = [u for u in U if first_near is not None and u["id"] < first_near]
        post = near
        after = [u for u in U if near and u["id"] > near[-1]["id"]]
        out.append({
            "id": f"C{n:03d}", "start": ev["start"], "end": ev["end"], "tight_start": ev["tight_start"], "tight_end": ev["tight_end"], "peak_t": round(ev["peak_t"], 2),
            "runtime_sec": round(ev["end"] - ev["start"], 2), "score": score, "score_note": "priority only; length is metadata and never part of the score",
            "tags": tags_for(ev, evd, ctx["content_type"]), "evidence": evd, "seeds": ev["seeds"], "start_reason": ev["start_reason"], "end_reason": ev["end_reason"],
            "hook": (U[0]["text"] if U else ""), "setup": " ".join(u["text"] for u in pre)[:400],
            "payoff": " ".join(u["text"] for u in post)[:400], "aftermath": " ".join(u["text"] for u in after)[:400],
            "audio_evidence": [f"{k} x{v}" for k, v in evd["audio"].items()] or ["none above baseline"],
            "visual_evidence": f"motion spike {evd['max_motion']}, {evd['cuts_inside']} cuts ({evd['scene_changes_inside']} real scene changes) inside; frames to be reviewed",
        })
    return out


# ----------------------------------------------------------------------------- orchestration
def load_context(source_id: str, root=None, content_type: str | None = None) -> dict:
    d = run_dir(source_id, root)
    meta = read_json(d / "source.json")
    a = d / "analysis"
    ct = content_type or read_json(a / "content_type.json")["content_type"]
    tr = read_json(d / "transcripts" / "pass1.json") if (d / "transcripts" / "pass1.json").is_file() else {"segments": []}
    spk = read_json(a / "speakers.json") if (a / "speakers.json").is_file() else {"labels": [], "changes": []}
    am = audio_mod.load_meta(a / "audio") if (a / "audio.json").is_file() else {"events": []}
    series = audio_mod.load_series(a / "audio") if (a / "audio.npz").is_file() else None
    vis = read_json(a / "visual.json")
    z = np.load(a / "visual.npz")
    if series is not None:
        refine_words(tr["segments"], series["db"], audio_mod.HOP)
    if series is not None:
        write_json(d / "transcripts" / "pass1.refined.json", tr)          # word edges pulled onto the audible speech: planning and QA share one timing
    U = utterances(tr["segments"], spk["labels"], series["db"] if series is not None else None, audio_mod.HOP)
    ctx = {"source_id": source_id, "content_type": ct, "duration": meta["probe"]["duration_sec"], "series": series, "audio_events": am["events"], "utterances": U,
           "speaker_changes": spk["changes"], "visual_cuts": vis["cuts"], "visual_arrays": {"motion": z["motion"]}, "visual_hz": vis["hz"], "hist": z["hist"],
           "visual_t": z["t"], "faces": vis["faces"], "meta": meta, "segments": tr["segments"],
           "words_flat": [(w["start"], w["end"]) for sg in tr["segments"] for w in sg.get("words", [])]}
    ctx["boundaries"] = boundary_list(ctx)
    return ctx


def build_candidates(source_id: str, root=None, content_type: str | None = None) -> dict:
    ctx = load_context(source_id, root, content_type)
    tl = build_timeline(ctx)
    peaks = find_peaks(tl["interest"])
    grown = [grow(ctx, tl, p) for p in peaks]
    merged = merge_events(grown)
    cands = to_candidates(ctx, tl, merged)
    d = run_dir(source_id, root)
    write_json(d / "analysis" / "timeline.json", {"grid_sec": GRID, "interest": np.round(tl["interest"], 3).tolist(),
                                                  "peaks": [round(p * GRID, 2) for p in peaks], "boundaries": ctx["boundaries"]})
    ledger = {"source_id": source_id, "content_type": ctx["content_type"], "n_peaks": len(peaks), "n_candidates": len(cands), "candidates": cands,
              "passes": {"ranking": None, "standalone": None},
              "method": "fused interest timeline (loudness vs local baseline, onsets, laughter-like, comic pauses, transcript cues, motion, turns, music, cuts) -> seeds -> grown by boundaries"}
    write_json(d / "candidates" / "ledger.json", ledger)
    return ledger
