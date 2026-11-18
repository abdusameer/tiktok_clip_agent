"""Automatic post package: short searchable title, natural truthful description, relevant hashtags, cover timestamp, interaction settings.

Everything is built from facts in the manifest and the source's metadata (streamer / game or topic / event, from `<file>.meta.json` or the service config);
nothing is invented. Generic reach tags (#fyp, #viral, #foryou, trending topics) are never added.
"""
import re

BLOCKED_TAGS = {"fyp", "foryou", "foryoupage", "viral", "trending", "xyzbca", "fy", "viralvideo", "explore", "blowthisup", "goviral"}
TYPE_TAGS = {"streamer": ["streamer", "streamerclips"], "gaming": ["gaming", "gameplay"], "movie": ["movieclips"], "show": ["tvshow"], "podcast": ["podcastclips"],
             "interview": ["interview"]}
TAG_FROM_REVIEW = {"laugh": "funnymoments", "scream": "reactions", "comic-pause": "funnymoments", "banter": "banter", "reveal": "plottwist"}
MAX_TITLE = 90


def _clean_tag(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _sentence(text: str, n: int = 14) -> str:
    w = re.findall(r"[A-Za-z0-9'’,\-?!.]+", text or "")
    return " ".join(w[:n]).strip(" ,-")


def build(manifest: dict, cfg: dict, meta: dict | None = None, style: str = "with_context") -> dict:
    meta = {**{k: v for k, v in cfg.get("metadata", {}).items() if v}, **{k: v for k, v in (meta or {}).items() if v}}
    streamer, topic, event = meta.get("streamer"), meta.get("game_or_topic"), meta.get("event")
    ctype, ranking = manifest["content_type"], manifest["format"] == "ranking"
    head = manifest["theme"] if ranking else manifest["headline"]
    parts = [head]
    if style == "with_context" and topic:
        parts.append(topic)
    if streamer and (style == "with_context" or not ranking):
        parts.append(streamer)
    title = " | ".join(p for p in parts if p)
    title = title if len(title) <= MAX_TITLE else head[:MAX_TITLE]
    n = len(manifest["source_timecodes"])
    who = f"{streamer}'s" if streamer else "this"
    if ranking:
        desc = f"{n} moments ranked: {manifest['theme'].lower()}" + (f" from {topic}" if topic else "") + (f" ({who} stream)" if streamer else "") + "."
    else:
        quote = _sentence((manifest["payoff"] or [""])[0])
        desc = (f'"{quote}"' if quote else head) + (f" - {topic}" if topic else "") + (f", {streamer}" if streamer else "") + (f" ({event})" if event else "") + "."
    tags = []
    for t in (meta.get("hashtags") or []):
        tags.append(_clean_tag(t))
    tags += [_clean_tag(topic)] if topic else []
    tags += [_clean_tag(streamer)] if streamer else []
    tags += TYPE_TAGS.get(ctype, [])
    for e in manifest.get("editorial", []):
        for t in e.get("tags", []):
            if t in TAG_FROM_REVIEW:
                tags.append(TAG_FROM_REVIEW[t])
    seen, out = set(), []
    for t in tags:
        if t and t not in BLOCKED_TAGS and t not in seen and 2 < len(t) <= 30:
            seen.add(t)
            out.append(t)
    out = out[:5]
    # cover: the strongest entry's peak, as an offset inside the output (entries play in countdown order, rank 1 last)
    play = sorted(manifest["source_timecodes"], key=lambda m: -m["rank"])
    off, cover = 0.0, 0
    for m in play:
        dur = m["end"] - m["start"]
        if m["rank"] == 1:
            cover = int(1000 * (off + max(0.0, min(dur - 0.1, (m.get("peak_t") or m["start"] + dur / 2) - m["start"]))))
        off += dur
    full = f"{desc}\n\n" + " ".join("#" + t for t in out) if out else desc
    assert not any(t in BLOCKED_TAGS for t in out)
    return {"title": title, "description": desc, "caption": full, "hashtags": out, "creator": streamer, "topic": topic, "event": event, "cover_timestamp_ms": cover,
            "style": style, "settings": {k: cfg["post_defaults"][k] for k in ("privacy_level", "allow_comment", "allow_duet", "allow_stitch")}}
