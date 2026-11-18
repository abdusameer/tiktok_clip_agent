"""On-device speaker-change PROXY (no torch, no tokens, no telemetry).

For each transcript segment of useful length it measures median pitch, spectral centroid and a 12-band spectral shape, standardises these across
the whole source and groups segments greedily (max 4 voices). It is labelled a proxy everywhere it is used: shouting changes pitch, so a very
loud passage can be assigned a second "voice". It is evidence for reframing and conversation structure, never ground truth. A real diarizer
(pyannote) may be plugged in through `external_labels` when the user installs it; see docs/make-clips.md.
"""
import numpy as np

SR = 16000
BANDS = [(100, 200), (200, 300), (300, 450), (450, 650), (650, 900), (900, 1250), (1250, 1700), (1700, 2300), (2300, 3000), (3000, 4000), (4000, 5500), (5500, 7500)]


def _f0(x: np.ndarray) -> float:
    frame, hop = int(0.04 * SR), int(0.02 * SR)
    vals = []
    for i in range(0, len(x) - frame, hop):
        w = x[i:i + frame] * np.hanning(frame)
        if np.sqrt((w ** 2).mean()) < 0.01:
            continue
        ac = np.correlate(w, w, "full")[frame - 1:]
        lo, hi = int(SR / 400), int(SR / 70)
        k = lo + int(np.argmax(ac[lo:hi]))
        if ac[k] > 0.3 * ac[0]:
            vals.append(SR / k)
    return float(np.median(vals)) if len(vals) >= 3 else float("nan")


def segment_features(x: np.ndarray, a: float, b: float) -> np.ndarray | None:
    mid = (a + b) / 2
    a, b = max(a, mid - 3.0), min(b, mid + 3.0)                                       # at most 6 s around the middle of the segment
    seg = x[int(a * SR):int(b * SR)]
    if len(seg) < int(0.6 * SR):
        return None
    f0 = _f0(seg)
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
    freqs = np.fft.rfftfreq(len(seg), 1 / SR)
    band = np.array([spec[(freqs >= lo) & (freqs < hi)].sum() for lo, hi in BANDS]) + 1e-12
    band = np.log(band / band.sum())
    cen = float((spec * freqs).sum() / (spec.sum() + 1e-12))
    return np.concatenate([[np.log(f0) if f0 == f0 else np.nan, cen / 1000.0], band])


def label_segments(x: np.ndarray, segments: list[dict], max_voices: int = 4, threshold: float = 1.6) -> dict:
    feats = [segment_features(x, s["start"], s["end"]) for s in segments]
    ok = [i for i, f in enumerate(feats) if f is not None]
    if len(ok) < 2:
        return {"method": "spectral-proxy", "voices": 1 if ok else 0, "labels": [0 if f is not None else None for f in feats], "changes": []}
    M = np.array([feats[i] for i in ok])
    col_med = np.nanmedian(M, axis=0)
    M = np.where(np.isnan(M), col_med, M)
    M = (M - M.mean(axis=0)) / (M.std(axis=0) + 1e-6)
    cents: list[np.ndarray] = []
    counts: list[int] = []
    lab = []
    for v in M:
        d = [np.linalg.norm(v - c) / np.sqrt(len(v)) * 3 for c in cents]
        j = int(np.argmin(d)) if d else -1
        if j >= 0 and d[j] <= threshold:
            counts[j] += 1
            cents[j] = cents[j] + (v - cents[j]) / counts[j]
            lab.append(j)
        elif len(cents) < max_voices:
            cents.append(v.copy())
            counts.append(1)
            lab.append(len(cents) - 1)
        else:
            lab.append(j)
    labels = [None] * len(segments)
    for i, l in zip(ok, lab):
        labels[i] = l
    changes, last = [], None
    for i, l in enumerate(labels):
        if l is None:
            continue
        if last is not None and l != labels[last]:
            changes.append({"t": segments[i]["start"], "from": labels[last], "to": l, "segment": i})
        last = i
    return {"method": "spectral-proxy", "voices": len(cents), "labels": labels, "changes": changes,
            "note": "proxy: pitch/spectral-shape clustering, not neural diarization"}
