"""Render /make-clips projects to 1080x1920 H.264/AAC using the approved style tokens (config/ranking_style.json) and the original audio.

Layouts (chosen per moment, recorded in the manifest):
  fit         - blurred full-frame backdrop + the whole picture scaled to the canvas width (letterbox bars cropped out). Default; never destroys a scene.
  fill        - portrait / near-portrait sources fill the canvas.
  face-track  - 9:16 window that follows the subject's face (podcast/interview/facecam close-ups) when one window can hold the faces.
Optional effect: punch_in (a 10 % push-in over 0.35 s at an impact peak, fit layout only). Headline pill and captions are drawn as PNG overlays
(ffmpeg here has no text filters) by the project's Pillow helper. Captions contain ONLY words both Whisper decodes agree on; profanity is not censored.
Nothing is uploaded or published.
"""
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .. import captions as captions_mod
from .. import colors as colors_mod
from .. import render as base
from .. import style, textsafety
from . import reframe
from .common import ClipsError, read_json, run_dir, write_json

_S = style.load()
W, H, FPS = base.W, base.H, base.FPS
BAND_Y = base._S["framing"]["band_y"]
CAP_Y, CAP_H = 1245, 200
HALLUCINATION = re.compile(r"thanks for watching|subscribe|like and|^bye\b", re.I)


class RenderError(ClipsError):
    pass


# ---------------------------------------------------------------- captions
def caption_lines(media: Path, start: float, end: float, base_segments: list[dict], cache: Path, model: str = "medium.en") -> dict:
    """Two independent decodes (beam 1 and 5) of the moment; a caption word must be confirmed by both and by the pass-1 transcript."""
    if cache.is_file():
        return read_json(cache)
    dec = captions_mod.decode_moment(media, start, end, model_name=model, pad=1.0)["decodes"]
    ok = {w for s in base_segments if s["end"] >= start - 1.5 and s["start"] <= end + 1.5 for w in captions_mod.tokens(s["text"])}
    agreed = [w for w in captions_mod.agreed_words(dec[0], dec[1]) if start - 0.1 <= w["start"] and w["end"] <= end + 0.3 and w["w"] in ok]
    groups, cur = [], []
    for w in agreed:
        if cur and (w["start"] - cur[-1]["end"] > 0.6 or len(cur) >= 8):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    words, lines = [], []
    for g in groups:
        if len(g) < 2 or HALLUCINATION.search(" ".join(x["w"] for x in g)):
            continue
        first = len(words)
        words += [[x["w"], x["start"], x["end"]] for x in g]
        text = " ".join(x["w"] for x in g)
        lines.append({"text": text[:1].upper() + text[1:], "first": first, "last": len(words) - 1})
    sample = [w["prob"] for s in base_segments if s["end"] > start and s["start"] < end for w in s.get("words", []) if start <= w["start"] <= end]
    heard = sum(len(captions_mod.tokens(s["text"])) for s in base_segments if s["end"] > start and s["start"] < end)
    out = {"decodes": dec, "words": words, "lines": lines, "model": model,
           "confidence": {"mean_word_probability": round(sum(sample) / len(sample), 3) if sample else None,
                          "decode_agreement": round(len(agreed) / max(heard, 1), 3), "caption_words": len(words)}}
    write_json(cache, out)
    return out


def severe_caption_findings(lines: list[dict]) -> list[dict]:
    return [f for ln in lines for f in textsafety.scan_text(ln["text"])]


# ---------------------------------------------------------------- layout
def decide_layout(ctx: dict, a: float, b: float, peak_t: float) -> dict:
    """ctx: {src_w, src_h, bars:{top_frac,bottom_frac}, faces, times, content_type, impacts:[t...]}"""
    sw, sh = ctx["src_w"], ctx["src_h"]
    bars = ctx.get("bars", {"top_frac": 0, "bottom_frac": 0})
    effects = []
    if sw / sh < 1.2:
        return {"layout": "fill", "bars": bars, "effects": effects, "reason": "portrait/near-portrait source fills the canvas"}
    if ctx["content_type"] in ("podcast", "interview", "streamer") and ctx.get("faces"):
        path = reframe.subject_path(ctx["faces"], ctx["times"], a, b)
        p = reframe.plan(path, sw, sh, a, b)
        if p["method"] == "face-track":
            return {"layout": "face-track", "bars": bars, "effects": effects, "reframe": p, "reason": "a single 9:16 window holds the subject for the whole clip"}
        fallback = p["reason"]
    else:
        fallback = "no faces to follow" if ctx["content_type"] in ("podcast", "interview", "streamer") else "scene content: keep the whole picture"
    if ctx["content_type"] in ("gaming", "streamer") and any(abs(t - peak_t) <= 0.6 for t in ctx.get("impacts", [])):
        effects.append({"type": "punch_in", "t": round(max(peak_t - a - 0.1, 0), 2), "reason": "sudden impact/scream at the payoff; 10% push-in over 0.35 s"})
    return {"layout": "fit", "bars": bars, "effects": effects, "reframe": {"method": "fit", "reason": fallback}, "reason": f"blur-fit: {fallback}"}


def _even(x: float) -> int:
    return int(round(x / 2)) * 2


def filter_graph(layout: dict, src_w: int, src_h: int, dur: float) -> tuple[list[str], str]:
    """Returns (filter lines producing [v0], description)."""
    bg = base._S["background"]
    bars = layout["bars"]
    if layout["layout"] == "face-track":
        cw = layout["reframe"]["window_w"]
        x = reframe.crop_x_expr(layout["reframe"]["keyframes"], src_w, cw)
        return [f"[0:v]crop=w={cw}:h={src_h}:x='{x}':y=0,scale={W}:{H}:flags=lanczos,setsar=1[v0]"], "face-track"
    ch = src_h * (1 - bars["top_frac"] - bars["bottom_frac"])
    pre = (f"crop=iw:{_even(ch)}:0:{_even(src_h * bars['top_frac'])}," if bars["top_frac"] + bars["bottom_frac"] > 0.02 else "")
    if layout["layout"] == "fill":
        return [f"[0:v]{pre}scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[v0]"], "fill"
    fgh = _even(W * ch / src_w)
    fx = next((e for e in layout["effects"] if e["type"] == "punch_in"), None)
    if fx:
        fg = (f"[b]scale=w='{W}*(1+0.10*clip((t-{fx['t']})/0.35,0,1))':h=-2:eval=frame,"
              f"crop={W}:{fgh}:(iw-{W})/2:(ih-{fgh})/2,setsar=1[fg]")
    else:
        fg = f"[b]scale={W}:{fgh},setsar=1[fg]"
    y = BAND_Y
    return [f"[0:v]{pre}split=2[a][b]",
            f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},gblur=sigma={bg['blur_sigma']},eq=brightness={bg['brightness']}:saturation={bg['saturation']},setsar=1[bg]",
            fg, f"[bg][fg]overlay=0:{y}[v0]"], "fit"


# ---------------------------------------------------------------- overlays
def overlay_items(project: dict, moments: list[dict], work: Path, accent: str | None) -> dict:
    """PNG items: ranking header/badge/hook (approved template), headline pill, caption lines."""
    items, cp, lb = [], base._S["captions"], base._S["label"]
    if project["format"] == "ranking":
        order = [str(m["rank"]) for m in sorted(moments, key=lambda m: -m["rank"])]
        spec = {"title": f"Ranking the {project['category'].lower()}", "moments": {str(m["rank"]): {"words": [], "lines": [], "start": m["start"], "end": m["end"]} for m in moments}}
        labels = {str(m["rank"]): m["title"] for m in moments}
        colors = {r: accent for r in order} if accent else None
        items += base.png_spec(spec, order, work, labels, colors)["items"]
    else:
        m = moments[0]
        items.append({"out": str(work / "label1.png"), "w": W, "h": base.LABEL_H, "pill": dict(lb["background"]),
                      "blocks": [{"text": m["title"], "font": lb["font"], "size": lb["size"], "cx": W // 2, "y": lb["text_y"],
                                  "fill": [*colors_mod.to_rgb(accent), 255] if accent else lb["color"], "stroke": 0}]})
    for m in moments:
        for i, (text, _, _) in enumerate(base.caption_timing(m)):
            items.append({"out": str(work / f"cap{m['rank']}_{i:02d}.png"), "w": W, "h": CAP_H,
                          "blocks": [{"text": text, "font": cp["font"], "size": cp["size"], "cx": W // 2, "y": 10, "stroke": cp["stroke"], "shadow": cp["shadow"],
                                      "max_width": base.CAP_MAX_W, "line_spacing": cp["line_spacing"]}]})
    return {"items": items}


def part_command(project: dict, m: dict, first: bool, work: Path, media: Path, out: Path, layout: dict, dims: tuple[int, int]) -> list[str]:
    dur = base.frame_exact(m)
    rank = str(m["rank"])
    ve = base._S["video_encode"]
    lines, _ = filter_graph(layout, dims[0], dims[1], dur)
    inputs = ["-ss", f"{m['start']}", "-t", f"{dur}", "-i", str(media)]
    f = list(lines)
    last, idx = "v0", 1
    if project["format"] == "ranking":
        bd, hk = base._S["badge"], base._S["hook"]
        inputs += ["-i", str(work / f"header{rank}.png"), "-loop", "1", "-framerate", str(FPS), "-t", f"{dur}", "-i", str(work / f"badge{rank}.png")]
        f += [f"[{last}][{idx}:v]overlay=0:0[h1]", f"[{idx + 1}:v]format=rgba,fade=t=in:st=0:d={bd['fade_in']}:alpha=1[bd]",
              f"[h1][bd]overlay=x=0:y='{base.BADGE_Y}+{bd['slide_px']}*pow(max(0,1-t/{bd['slide_seconds']}),2)'[v2]"]
        last, idx = "v2", idx + 2
        if first:
            inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{dur}", "-i", str(work / "hook.png")]
            f += [f"[{idx}:v]format=rgba,fade=t=in:st={hk['fade_in_start']}:d={hk['fade_in']}:alpha=1,fade=t=out:st={hk['fade_out_start']}:d={hk['fade_out']}:alpha=1[hk]",
                  f"[{last}][hk]overlay=0:{hk['y'] + base.HDR_DY}[vh]"]
            last, idx = "vh", idx + 1
        show = base.LABEL_SHOW
    else:
        show = base._S["label"]["single_show_seconds"]
    inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{show}", "-i", str(work / (f"label{rank}.png"))]
    f += [f"[{idx}:v]format=rgba,fade=t=in:st=0:d={base.LABEL_FADE}:alpha=1,fade=t=out:st={show - base.LABEL_FADE}:d={base.LABEL_FADE}:alpha=1[lb]",
          f"[{last}][lb]overlay=x=0:y='{base.LABEL_Y}+{base.LABEL_RISE}*pow(max(0,1-t/{base.LABEL_FADE}),2)':eof_action=pass[vl]"]
    last, idx = "vl", idx + 1
    for i, (_, a, b) in enumerate(base.caption_timing(m)):
        inputs += ["-i", str(work / f"cap{rank}_{i:02d}.png")]
        f.append(f"[{last}][{idx}:v]overlay=0:{CAP_Y}:enable='between(t,{a:.3f},{b:.3f})'[c{i}]")
        last, idx = f"c{i}", idx + 1
    f.append(f"[{last}]fps={FPS},format={ve['pix_fmt']}[vout]")
    preset = os.environ.get("CLIPAGENT_PRESET", ve["preset"])
    return (["ffmpeg", "-v", "error", "-y"] + inputs + ["-filter_complex", ";".join(f), "-map", "[vout]", "-an", "-c:v", ve["codec"], "-preset", preset,
                                                        "-crf", str(ve["crf"]), "-profile:v", ve["profile"], "-pix_fmt", ve["pix_fmt"], str(out)])


def _run(cmd: list[str], what: str) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RenderError(f"{what} failed: {r.stderr.strip()[-600:]}")


def render_project(source_id: str, project: dict, root=None, progress=print) -> dict:
    """Render one ranking or standalone project to runs/<id>/renders/<id>-<name>.mp4. Returns the render record (layouts, captions, evidence)."""
    d = run_dir(source_id, root)
    meta = read_json(d / "source.json")
    media = Path(meta["path"])
    a = d / "analysis"
    vis = read_json(a / "visual.json")
    z = __import__("numpy").load(a / "visual.npz")
    times = [float(t) for t in z["t"]]
    tr = read_json(d / "transcripts" / "pass1.json") if (d / "transcripts" / "pass1.json").is_file() else {"segments": []}
    am = read_json(a / "audio.json") if (a / "audio.json").is_file() else {"events": []}
    ctype = read_json(a / "content_type.json")["content_type"]
    impacts = [e["t0"] for e in am["events"] if e["type"] in ("impact", "loud")]
    dims = (meta["probe"]["width"], meta["probe"]["height"])
    out = d / "renders" / f"{project['id']}-{project['name']}.mp4"
    if out.exists():
        raise RenderError(f"{out.name} already exists; refusing to overwrite")
    moments = sorted(project["moments"], key=lambda m: -m["rank"])
    rec = {"project": project["id"], "layouts": [], "captions": [], "chat_scan": [], "output": str(out)}
    prepared = []
    for m in moments:
        cap = ({"decodes": [[], []], "words": [], "lines": [], "confidence": {"mean_word_probability": None, "decode_agreement": None, "caption_words": 0}}
               if not meta["probe"]["has_audio"] or not tr["segments"] else
               caption_lines(media, m["start"], m["end"], tr["segments"], d / "work" / f"decode_{m['candidate_id']}_{m['start']:.2f}_{m['end']:.2f}.json"))
        mm = {**m, "words": cap["words"], "lines": cap["lines"], "decodes": cap["decodes"]}
        bad = captions_mod.problems({"moments": {str(m["rank"]): mm}})
        severe = severe_caption_findings(cap["lines"])
        lay = decide_layout({"src_w": dims[0], "src_h": dims[1], "bars": vis["bars"], "faces": vis["faces"], "times": times, "content_type": ctype, "impacts": impacts},
                            m["start"], m["end"], m["peak_t"])
        try:
            scan = textsafety.scan_moment(media, m["start"], m["end"], step=2.0, root=Path(__file__).resolve().parents[3], src_dims=dims)
        except Exception as exc:                                          # OCR helper missing or failing: recorded as unverified, never as clean
            scan = {"ocr_available": False, "verified_clean": False, "findings": [], "note": f"on-screen text not scanned: {exc}"}
        rec["layouts"].append({"rank": m["rank"], "candidate_id": m["candidate_id"], **{k: lay[k] for k in ("layout", "reason", "effects")}, "reframe": lay.get("reframe")})
        rec["captions"].append({"rank": m["rank"], "lines": [l["text"] for l in cap["lines"]], "confidence": cap["confidence"], "unconfirmed_words": bad, "severe_findings": severe})
        rec["chat_scan"].append({"rank": m["rank"], **{k: scan.get(k) for k in ("ocr_available", "verified_clean", "findings", "note", "chat_unverified")}})
        prepared.append((mm, lay))
    accent = None
    if project["format"] == "ranking" or _S["label"]["title_accent"]["enabled"]:
        seed = colors_mod.seed_for(source_id, project["id"], project.get("category") or moments[0]["title"])
        accent = colors_mod.resolve(None, seed, ["1"])["1"]
    rec["accent_color"] = accent
    with tempfile.TemporaryDirectory(prefix="clips-render-") as tmp:
        work = Path(tmp)
        (work / "spec.json").write_text(json.dumps(overlay_items(project, [p[0] for p in prepared], work, accent)))
        r = subprocess.run([base._S["fonts"]["renderer_python"], str(base.HELPER), str(work / "spec.json")], capture_output=True, text=True)
        if r.returncode:
            raise RenderError(f"text rendering failed: {r.stderr.strip()}")
        parts = []
        for k, (mm, lay) in enumerate(prepared):
            part = work / f"part{k}.mp4"
            _run(part_command(project, mm, k == 0, work, media, part, lay, dims), f"video for entry {mm['rank']}")
            parts.append(part)
            progress(f"    {project['id']} entry {mm['rank']}: {lay['layout']} {mm['end'] - mm['start']:.1f}s")
        (work / "list.txt").write_text("".join(f"file '{p}'\n" for p in parts))
        video, audio = work / "video.mp4", work / "audio.m4a"
        _run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(work / "list.txt"), "-c", "copy", str(video)], "concat")
        spec = {"moments": {str(p[0]["rank"]): p[0] for p in prepared}}
        _run(base.audio_command(spec, [str(p[0]["rank"]) for p in prepared], media, audio), "audio")
        _run(["ffmpeg", "-v", "error", "-n", "-i", str(video), "-i", str(audio), "-map", "0:v", "-map", "1:a", "-c", "copy", "-movflags", "+faststart", str(out)], "mux")
    write_json(d / "renders" / f"{out.stem}.render.json", rec)
    return rec
