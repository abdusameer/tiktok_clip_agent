"""Content-type detection for `--content-type auto`: measured features -> additive evidence points per type. Pure functions (unit-tested).

Types: streamer, gaming, movie, show, podcast, interview. The result always carries the evidence and a confidence; a low-confidence result is
flagged so the reviewing agent can override it with `--content-type`.
"""
import re

CHAT = re.compile(r"\b(chat|streamers?|subs?|subscribers?|donat\w+|twitch|viewers?|clip (it|that)|raid|bits|emotes?|stream)\b", re.I)
GAME = re.compile(r"\b(game|level|boss|respawn|loot|kills?|ranked|controller|enemy|enemies|spawn|checkpoint|inventory|health bar|chest|quest)\b", re.I)
PODCAST = re.compile(r"\b(podcast|episode|welcome (back )?to the (show|podcast)|today'?s guest|our guest|listeners?)\b", re.I)
INTERVIEW = re.compile(r"\b(interview|tell me about|thanks for (having|joining) (me|us)|nice to meet you|my guest)\b", re.I)


def features(meta: dict, visual: dict, audio: dict, transcript: dict, speakers: dict) -> dict:
    """Condense the analysis outputs into the numbers `classify` needs."""
    dur = max(meta["duration_sec"], 1.0)
    mins = dur / 60.0
    segs = transcript.get("segments", [])
    words = [w for s in segs for w in s.get("words", [])]
    speech = sum(s["end"] - s["start"] for s in segs)
    text = " ".join(s["text"] for s in segs)
    ev = audio.get("events", [])
    count = lambda *kinds: sum(1 for e in ev if e["type"] in kinds)
    faces = visual.get("faces", [])
    n = max(len(faces), 1)
    with_face = [f for f in faces if f]
    areas = [max(b[2] * b[3] for b in f) for f in with_face]
    corner = 0
    for f in with_face:
        big = max(f, key=lambda b: b[2] * b[3])
        cx, cy = big[0] + big[2] / 2, big[1] + big[3] / 2
        if (cx < 0.3 or cx > 0.7) and (cy < 0.3 or cy > 0.7) and big[2] * big[3] < 0.06:
            corner += 1
    bars = visual.get("bars", {})
    return {
        "duration_min": round(mins, 2), "aspect": round(meta["width"] / meta["height"], 3),
        "letterboxed": (bars.get("top_frac", 0) + bars.get("bottom_frac", 0)) > 0.08,
        "cut_rate_per_min": round(len(visual.get("cuts", [])) / mins, 2),
        "speech_ratio": round(min(speech / dur, 1.0), 3),
        "words_per_speech_sec": round(len(words) / speech, 2) if speech else 0.0,
        "speaker_changes_per_min": round(len(speakers.get("changes", [])) / mins, 2),
        "loud_per_min": round(count("loud", "impact") / mins, 2), "laugh_per_min": round(count("laughter_like") / mins, 2),
        "music_change_per_min": round(count("music_change") / mins, 2),
        "face_ratio": round(len(with_face) / n, 3), "mean_face_area": round(sum(areas) / len(areas), 4) if areas else 0.0,
        "facecam_ratio": round(corner / n, 3),
        "multi_face_ratio": round(sum(1 for f in faces if len(f) >= 2) / n, 3),
        "chat_hits_per_10min": round(len(CHAT.findall(text)) / mins * 10, 2), "game_hits_per_10min": round(len(GAME.findall(text)) / mins * 10, 2),
        "podcast_hits": len(PODCAST.findall(text)), "interview_hits": len(INTERVIEW.findall(text)),
    }


def classify(f: dict) -> dict:
    pts = {t: 0.0 for t in ("streamer", "gaming", "movie", "show", "podcast", "interview")}
    why = {t: [] for t in pts}

    def add(t, p, reason):
        pts[t] += p
        why[t].append(f"+{p:g} {reason}")

    chat = min(f["chat_hits_per_10min"], 6) / 2                                       # up to +3
    if chat:
        add("streamer", chat, f"chat/stream vocabulary ({f['chat_hits_per_10min']}/10min)")
    if f["cut_rate_per_min"] < 3 and not f["letterboxed"]:
        add("streamer", 1.0, "few cuts (continuous capture)")
        add("gaming", 1.0, "few cuts (continuous capture)")
    if f["loud_per_min"] >= 1.0:
        add("streamer", 1.0, "frequent loud reactions")
        add("gaming", 0.5, "frequent loud events")
    if f["facecam_ratio"] > 0.3:
        add("streamer", 1.5, "small corner facecam")
        add("gaming", 0.5, "small corner facecam")
    if f["speech_ratio"] > 0.5:
        add("streamer", 1.0, "talk-driven")
    game = min(f["game_hits_per_10min"], 6) / 2
    if game:
        add("gaming", game, f"game vocabulary ({f['game_hits_per_10min']}/10min)")
        add("streamer", game / 3, "game vocabulary")
    if f["speech_ratio"] < 0.4:
        add("gaming", 1.5, "mostly non-speech audio")
    if f["letterboxed"]:
        add("movie", 2.5, "letterbox bars")
        add("show", 1.0, "letterbox bars")
    if f["cut_rate_per_min"] >= 6:
        add("movie", 1.5, "high cutting rate")
        add("show", 1.5, "high cutting rate")
    if f["music_change_per_min"] >= 0.5:
        add("movie", 1.0, "score/music changes")
        add("show", 1.0, "score/music changes")
    if f["speaker_changes_per_min"] >= 2 and not f["chat_hits_per_10min"]:
        add("movie", 1.0, "dialogue turn-taking")
        add("show", 1.0, "dialogue turn-taking")
    if not f["chat_hits_per_10min"] and f["facecam_ratio"] < 0.1:
        add("movie", 0.5, "no stream vocabulary or facecam")
        add("show", 0.5, "no stream vocabulary or facecam")
    if f["duration_min"] >= 75:
        add("movie", 1.5, "feature length")
    elif f["duration_min"] >= 8:
        add("show", 1.5, "episode length")
    if f["podcast_hits"]:
        add("podcast", 2.5, "podcast vocabulary")
    if f["interview_hits"]:
        add("interview", 2.5, "interview vocabulary")
    if f["speech_ratio"] > 0.7 and f["cut_rate_per_min"] < 2.5:
        add("podcast", 1.5, "near-continuous speech, static camera")
        add("interview", 1.5, "near-continuous speech, static camera")
    if f["face_ratio"] > 0.6 and f["mean_face_area"] > 0.04:
        add("podcast", 1.0, "large face(s) on screen")
        add("interview", 1.0, "large face(s) on screen")
    if f["multi_face_ratio"] > 0.3:
        add("interview", 1.0, "two or more people on screen")
        add("podcast", 0.5, "two or more people on screen")
    if f["loud_per_min"] < 0.5 and f["speech_ratio"] > 0.6:
        add("podcast", 0.5, "calm delivery")
        add("interview", 0.5, "calm delivery")
    ranked = sorted(pts.items(), key=lambda kv: -kv[1])
    (best, top), (second, sec) = ranked[0], ranked[1]
    conf = round((top - sec) / (top + 1e-6), 2) if top else 0.0
    low = top < 2.0 or conf < 0.15
    if low:
        why[best].append("LOW CONFIDENCE: confirm or pass --content-type explicitly")
    return {"content_type": best, "confidence": conf, "low_confidence": low, "points": {k: round(v, 2) for k, v in pts.items()},
            "evidence": why[best], "runner_up": second, "features": f}
