"""9:16 reframing math: a smoothed face path -> a few keyframes -> one ffmpeg crop-x expression.

Subject choice per sample: the largest face, with a preference for the face nearest the previous centre (no jumping between two people on every
frame). Gaps hold the last centre. A dead band stops micro-jitter: the window only glides when the subject drifts out of it. If the faces of the
clip do not fit one 9:16 window, `plan()` returns method "fit" instead of a tracked crop (cutting between people is a layout decision we do not
make blindly).
"""
import numpy as np


def window_width(src_w: int, src_h: int) -> int:
    return int(src_h * 9 / 16) // 2 * 2


def subject_path(faces: list[list], times: list[float], a: float, b: float) -> list[tuple[float, float, float]]:
    """[(t, cx, spread)] for samples in [a, b]: normalised centre x of the chosen face and the horizontal extent of ALL faces in that sample."""
    out, prev = [], None
    for t, fs in zip(times, faces):
        if t < a or t > b:
            continue
        if not fs:
            if prev is not None:
                out.append((t - a, prev, 0.0))
            continue
        cands = [(f[0] + f[2] / 2, f[2] * f[3]) for f in fs]
        if prev is None:
            cx = max(cands, key=lambda c: c[1])[0]
        else:
            cx = max(cands, key=lambda c: c[1] - 0.5 * abs(c[0] - prev))[0]
        spread = (max(f[0] + f[2] for f in fs) - min(f[0] for f in fs))
        out.append((t - a, cx, spread))
        prev = cx
    return out


def keyframes(path: list[tuple[float, float, float]], dead_band: float = 0.04, glide: float = 0.6) -> list[tuple[float, float]]:
    """EMA-smooth the path and emit a keyframe only when the subject leaves the dead band around the current window centre."""
    if not path:
        return []
    ema, kf, cur = path[0][1], [], path[0][1]
    kf.append((0.0, cur))
    for t, cx, _ in path:
        ema = 0.7 * ema + 0.3 * cx
        if abs(ema - cur) > dead_band:
            cur = ema
            kf.append((t, cur))
    return kf


def plan(path, src_w: int, src_h: int, a: float, b: float) -> dict:
    cw = window_width(src_w, src_h)
    frac = cw / src_w
    covered = sum(1 for _, _, _ in path)
    if covered < 0.6 * max(1, (b - a) * 2):                      # faces not found for most of the clip: tracking has nothing to follow
        return {"method": "fit", "reason": "no stable face found for most of the clip"}
    if max((s for _, _, s in path), default=0.0) > frac * 0.9:
        return {"method": "fit", "reason": "faces are spread wider than one 9:16 window"}
    kf = keyframes(path)
    return {"method": "face-track", "window_w": cw, "keyframes": [(round(t, 2), round(c, 4)) for t, c in kf],
            "details": "YuNet face centres at 4 Hz, EMA 0.3, dead band 4% of width, 0.6 s glides"}


def crop_x_expr(kf: list[tuple[float, float]], src_w: int, cw: int, glide: float = 0.6) -> str:
    """Piecewise-linear glides between keyframes as one ffmpeg expression in `t` (seconds from the cut start), clamped inside the frame."""
    def px(c):
        return min(max(c * src_w - cw / 2, 0), src_w - cw)
    if not kf:
        return f"{(src_w - cw) / 2:.1f}"
    x0 = px(kf[0][1])
    terms = []
    prev = x0
    for t, c in kf[1:]:
        nxt = px(c)
        d = nxt - prev
        if abs(d) >= 1:
            terms.append(f"({d:.1f})*clip((t-{t:.2f})/{glide},0,1)")
        prev = nxt
    return f"{x0:.1f}" + ("+" + "+".join(terms) if terms else "")
