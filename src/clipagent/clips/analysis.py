"""Whole-source analysis: runs the visual and transcription workers (separate processes), audio events, speaker proxy and content-type detection.

Every stage caches its output under runs/<source-id>/ and is skipped on re-run (pass force=True to redo). The source is analysed from
beginning to end; nothing is sampled or truncated.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from .. import audioevents
from . import audio as audio_mod
from . import ctype, speakers
from .common import ClipsError, read_json, run_dir, write_json

HALLUCINATIONS = {"thank you.", "thanks for watching.", "you", "bye.", "thank you for watching."}


def _worker(module: str, args: list[str], log: Path) -> subprocess.Popen:
    log.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(__file__).resolve().parents[2]), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)}
    return subprocess.Popen([sys.executable, "-m", f"clipagent.clips.{module}", *args], stdout=subprocess.DEVNULL, stderr=open(log, "w"), env=env)


def _finish(p: subprocess.Popen, name: str, log: Path) -> None:
    rc = p.wait()
    if rc:
        tail = "\n".join(l for l in Path(log).read_text().splitlines() if "objc" not in l and "WARN" not in l)[-600:]
        raise ClipsError(f"{name} worker failed ({rc}): {tail}")


def clean_transcript(tr: dict) -> dict:
    """Drop Whisper's well-known silence/music hallucinations; keep everything else verbatim."""
    keep = [s for s in tr["segments"] if s["text"].strip().lower() not in HALLUCINATIONS and s["end"] > s["start"]]
    for i, s in enumerate(keep):
        s["id"] = i
    return {**tr, "segments": keep}


def run(source_id: str, root=None, force: bool = False, model: str = "small.en", hz: float = 4.0, progress=print) -> dict:
    d = run_dir(source_id, root)
    meta = read_json(d / "source.json")
    media = Path(meta["path"])
    if not media.is_file():
        raise ClipsError(f"source file moved or missing: {media}")
    a = d / "analysis"
    t0 = time.time()
    timings = {}
    pv, pt = None, None
    need_visual = force or not (a / "visual.json").is_file()
    need_tr = meta["probe"]["has_audio"] and (force or not (d / "transcripts" / "pass1.json").is_file())
    if need_visual:
        progress("  visual worker: shot cuts (PySceneDetect), motion, faces (YuNet), letterbox ...")
        pv = _worker("visual_worker", [str(media), str(a / "visual"), "--hz", str(hz)], d / "logs_visual.txt")
    if need_tr:
        progress(f"  transcript worker: {model}, word timestamps, VAD ...")
        pt = _worker("transcript_worker", [str(media), str(d / "transcripts" / "pass1.raw.json"), "--model", model], d / "logs_asr.txt")
    if meta["probe"]["has_audio"] and (force or not (a / "audio.json").is_file()):
        s = time.time()
        progress("  audio events (loudness, yelling, impacts, silence, laughter-like, comic pauses) ...")
        audio_mod.analyze(media, a / "audio")
        timings["audio_sec"] = round(time.time() - s, 1)
    if pv:
        s = time.time()
        _finish(pv, "visual", d / "logs_visual.txt")
        timings["visual_wait_sec"] = round(time.time() - s, 1)
    if pt:
        s = time.time()
        _finish(pt, "transcript", d / "logs_asr.txt")
        timings["transcript_wait_sec"] = round(time.time() - s, 1)
        raw = read_json(d / "transcripts" / "pass1.raw.json")
        write_json(d / "transcripts" / "pass1.json", clean_transcript(raw))
    transcript = read_json(d / "transcripts" / "pass1.json") if (d / "transcripts" / "pass1.json").is_file() else {"segments": []}
    if (a / "audio.json").is_file():
        audio_mod.add_laughter(a / "audio", [w for s in transcript["segments"] for w in s.get("words", [])])
    audio_meta = audio_mod.load_meta(a / "audio") if (a / "audio.json").is_file() else {"events": [], "duration_sec": meta["probe"]["duration_sec"]}
    if force or not (a / "speakers.json").is_file():
        if meta["probe"]["has_audio"] and transcript["segments"]:
            x = audioevents.load_mono(media)
            write_json(a / "speakers.json", speakers.label_segments(x, transcript["segments"]))
        else:
            write_json(a / "speakers.json", {"method": "none", "voices": 0, "labels": [], "changes": []})
    spk = read_json(a / "speakers.json")
    visual = read_json(a / "visual.json")
    feats = ctype.features(meta["probe"], visual, audio_meta, transcript, spk)
    ct = ctype.classify(feats)
    write_json(a / "content_type.json", ct)
    write_json(a / "analysis_summary.json", {"source_id": source_id, "duration_sec": meta["probe"]["duration_sec"], "segments": len(transcript["segments"]),
                                            "words": sum(len(s.get("words", [])) for s in transcript["segments"]), "cuts": len(visual["cuts"]),
                                            "audio_events": len(audio_meta["events"]), "voices_proxy": spk.get("voices"), "timings": timings,
                                            "wall_sec": round(time.time() - t0, 1), "whisper_model": model, "visual_hz": hz})
    return {"content_type": ct, "summary": read_json(a / "analysis_summary.json")}
