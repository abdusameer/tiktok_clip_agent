"""Audio evidence from the ORIGINAL source audio.

Events (all relative to the source's own noise floor and ACTIVE level, so a bimodal speech/silence mix does not hide anything):
  loud            - sustained level >= 12 dB above the typical active level (yelling / shouting / screaming)
  impact          - abrupt rise >= 15 dB to a level above the active level (bang, hit, sting, jump-scare)
  silence         - >= 0.75 s at the noise floor between active audio
  comic_pause     - a silence immediately followed (<= 1.5 s) by a loud burst, impact or laughter = a held beat before a payoff
  music_change    - timbre jump (from clipagent.audioevents)
  laughter_like   - 3-7 Hz envelope modulation (the "ha-ha-ha" rhythm) in a window where nobody is speaking; needs the transcript (see
                    `laughter_events`), because speech has the same syllable rate
Heuristics, not a trained tagger: music, applause and game audio can look like laughter or yelling. Documented limitation.
"""
from pathlib import Path

import numpy as np

from .. import audioevents as ae
from .common import read_json, write_json

HOP = ae.HOP
ENV_HZ = 50


def noise_floor(db: np.ndarray) -> float:
    return float(np.percentile(db, 3))                  # a LOW percentile: even a talkative source has its quietest ~3% near the true floor


def active_baseline(db: np.ndarray, win: int = 601) -> np.ndarray:
    """Typical level of ACTIVE audio (frames well above the noise floor) in a ~5 min window around each frame; silences and a few shouts do not
    move it. Windows with no active audio fall back to the whole-source active median."""
    active = db > noise_floor(db) + 10
    glob = float(np.median(db[active])) if active.any() else float(np.median(db))
    x = np.where(active, db, np.nan).astype(np.float32)
    pad = np.pad(x, (win // 2, win // 2), mode="constant", constant_values=np.nan)
    out = np.full(len(db), glob, np.float32)
    step = 20                                                                    # the baseline moves slowly: evaluate every 5 s and hold
    for i in range(0, len(db), step):
        w = pad[i:i + win]
        w = w[~np.isnan(w)]
        if len(w) >= 20:
            out[i:i + step] = np.median(w)
    return out


def _runs(mask: np.ndarray, min_len: int):
    i = 0
    while i < len(mask):
        if mask[i]:
            j = i
            while j < len(mask) and mask[j]:
                j += 1
            if j - i >= min_len:
                yield i, j
            i = j
        else:
            i += 1


def robust_events(db: np.ndarray) -> list[dict]:
    nf, base = noise_floor(db), active_baseline(db)
    ev = []
    for a, b in _runs(db > base + 12, 2):
        ev.append({"type": "loud", "t0": a * HOP, "t1": b * HOP, "strength": round(float((db[a:b] - base[a:b]).max()), 1)})
    d = np.r_[0, np.diff(db)]
    for i in np.where((d >= 15) & (db > base + 8))[0]:
        ev.append({"type": "impact", "t0": i * HOP, "t1": (i + 1) * HOP, "strength": round(float(d[i]), 1)})
    for a, b in _runs(db < nf + 8, 3):
        if 0 < a and b < len(db):                                              # between active audio, not the file's lead-in / tail
            ev.append({"type": "silence", "t0": a * HOP, "t1": b * HOP, "strength": round((b - a) * HOP, 2)})
    return ev


def comic_pauses(events: list[dict]) -> list[dict]:
    out = []
    hits = [e for e in events if e["type"] in ("loud", "impact", "laughter_like")]
    for s in (e for e in events if e["type"] == "silence"):
        nxt = next((h for h in hits if 0 <= h["t0"] - s["t1"] <= 1.5), None)
        if nxt is not None:
            out.append({"type": "comic_pause", "t0": s["t0"], "t1": nxt["t0"], "strength": round(min(s["strength"], 4.0) + nxt["strength"] / 10, 2)})
    return out


def modulation_series(x: np.ndarray, sr: int = ae.SR, win: float = 2.0, hop: float = 0.5) -> dict:
    """Per 0.5 s step: share of envelope power at 3-7 Hz in a 2 s window, and the window's level above the noise floor (dB)."""
    n = int(sr / ENV_HZ)
    env = np.sqrt((x[: len(x) // n * n].reshape(-1, n) ** 2).mean(axis=1) + 1e-12)
    w, h = int(win * ENV_HZ), int(hop * ENV_HZ)
    freqs = np.fft.rfftfreq(w, 1 / ENV_HZ)
    band = (freqs >= 3) & (freqs <= 7)
    db = 20 * np.log10(env)
    nf = float(np.percentile(db, 5))
    ratio, level = [], []
    for i in range(0, max(len(env) - w, 0) + 1, h):
        seg = env[i:i + w]
        sp = np.abs(np.fft.rfft((seg - seg.mean()) * np.hanning(len(seg)))) ** 2
        ratio.append(float(sp[band].sum() / (sp[1:].sum() + 1e-12)))
        level.append(float(db[i:i + w].mean() - nf))
    return {"ratio": np.array(ratio, np.float32), "level": np.array(level, np.float32), "hop": hop, "win": win}


def laughter_events(mod: dict, words: list[dict]) -> list[dict]:
    """Rhythmic non-speech bursts: modulation ratio high, clearly above the floor, and (almost) no transcribed words in the window."""
    ev, hop, win = [], mod["hop"], mod["win"]
    ws = np.array([[w["start"], w["end"]] for w in words]) if words else np.zeros((0, 2))
    flags = []
    for i, (r, lv) in enumerate(zip(mod["ratio"], mod["level"])):
        t0 = i * hop
        spoken = 0.0
        if len(ws):
            ov = np.clip(np.minimum(ws[:, 1], t0 + win) - np.maximum(ws[:, 0], t0), 0, None)
            spoken = float(ov.sum()) / win
        flags.append(r > 0.55 and lv > 15 and spoken < 0.25)
    for a, b in _runs(np.array(flags), 2):
        ev.append({"type": "laughter_like", "t0": a * hop, "t1": b * hop + win, "strength": round(float(mod["level"][a:b].mean()) / 2, 1)})
    return ev


def analyze(media, out_prefix: Path) -> dict:
    """Writes <out_prefix>.json (events + summary) and <out_prefix>.npz (db, centroid, flux, modulation series). Laughter is added once the
    transcript exists (`add_laughter`)."""
    out_prefix = Path(out_prefix)
    x = ae.load_mono(media)
    f = ae.features(x)
    mod = modulation_series(x)
    ev = [e for e in ae.detect(f) if e["type"] == "music_change"] + robust_events(f["db"])
    ev = sorted(ev + comic_pauses(ev), key=lambda e: e["t0"])
    meta = {"hop_sec": HOP, "duration_sec": round(len(x) / ae.SR, 2), "noise_floor_db": round(noise_floor(f["db"]), 1),
            "active_level_db": round(float(np.median(f["db"][f["db"] > noise_floor(f["db"]) + 10])), 1) if (f["db"] > noise_floor(f["db"]) + 10).any() else None,
            "events": ev, "method": "ffmpeg 16 kHz mono; noise-floor/active-level relative loud, impact, silence, comic_pause; envelope-modulation laughter",
            "limitations": "heuristic event detection, not a trained audio tagger; music, applause and game audio can look like laughter or yelling"}
    np.savez_compressed(str(out_prefix) + ".npz", db=f["db"].astype(np.float32), cen=f["cen"].astype(np.float32), flux=f["flux"].astype(np.float32),
                        mod_ratio=mod["ratio"], mod_level=mod["level"])
    write_json(out_prefix.with_suffix(".json"), meta)
    return meta


def add_laughter(prefix: Path, words: list[dict]) -> dict:
    """Second step (needs the transcript): rhythmic non-speech bursts -> laughter_like events, then recompute comic pauses."""
    prefix = Path(prefix)
    meta = read_json(prefix.with_suffix(".json"))
    z = np.load(str(prefix) + ".npz")
    mod = {"ratio": z["mod_ratio"], "level": z["mod_level"], "hop": 0.5, "win": 2.0}
    keep = [e for e in meta["events"] if e["type"] not in ("laughter_like", "comic_pause")]
    ev = keep + laughter_events(mod, words)
    meta["events"] = sorted(ev + comic_pauses(ev), key=lambda e: e["t0"])
    write_json(prefix.with_suffix(".json"), meta)
    return meta


def load_series(prefix) -> dict:
    z = np.load(str(prefix) + ".npz")
    return {k: z[k] for k in z.files}


def events_in(events: list[dict], a: float, b: float, kinds=None) -> list[dict]:
    return [e for e in events if e["t1"] > a and e["t0"] < b and (kinds is None or e["type"] in kinds)]


def load_meta(prefix) -> dict:
    return read_json(Path(prefix).with_suffix(".json"))
