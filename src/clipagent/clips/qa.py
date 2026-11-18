"""QA gate for /make-clips renders. Reuses the project's technical checks (resolution, aspect, codecs, faststart, A/V sync, black/frozen frames,
clipping, cut-in-word, audio-source sync, edit-point frames) and adds clip-specific checks. Anything that cannot be confirmed automatically is
`manual_review`, never `pass`; only the by-design visual items may stay manual for an automatic approval."""
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .. import cutqa, qa as base_qa, render as base_render, titles
from .common import read_json, run_dir

PASS, FAIL, MANUAL = base_qa.PASS, base_qa.FAIL, base_qa.MANUAL
ALLOWED_MANUAL = set(base_qa.DESIGN_MANUAL) | {"onscreen_text_unverified"}


def words_fn(segments: list[dict]):
    """Word timings from the pass-1 transcript: cut-in-word checks need no re-decoding."""
    words = [(w["start"], w["end"], w["word"].strip()) for s in segments for w in s.get("words", [])]

    def fn(media, t, half=1.3):
        return [(a, b, w) for a, b, w in words if b >= t - half and a <= t + half]
    return fn


def _mean_volume(path: Path) -> tuple[float | None, float | None]:
    txt = base_qa._ff(["-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"])
    m, x = re.findall(r"mean_volume:\s*(-?[\d.]+) dB", txt), re.findall(r"max_volume:\s*(-?[\d.]+) dB", txt)
    return (float(m[-1]) if m else None, float(x[-1]) if x else None)


def face_visibility(path: Path) -> dict:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(__file__).resolve().parents[2]), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)}
    r = subprocess.run([sys.executable, "-m", "clipagent.clips.face_check", str(path)], capture_output=True, text=True, env=env)
    return json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 and r.stdout.strip() else {"samples": 0, "with_face": 0}


def offsets_of(moments_in_play_order: list[dict]) -> list[float]:
    out, t = [], 0.0
    for m in moments_in_play_order:
        out.append(round(t, 3))
        t += base_render.frame_exact(m)
    return out


def run(source_id: str, project: dict, rec: dict, root=None, siblings: list[Path] | None = None) -> dict:
    d = run_dir(source_id, root)
    meta = read_json(d / "source.json")
    media = Path(meta["path"])
    out = Path(rec["output"])
    refined = d / "transcripts" / "pass1.refined.json"
    tr = read_json(refined) if refined.is_file() else (read_json(d / "transcripts" / "pass1.json") if (d / "transcripts" / "pass1.json").is_file() else {"segments": []})
    play = sorted(project["moments"], key=lambda m: -m["rank"])
    offs = offsets_of(play)
    fmt = "ranking" if project["format"] == "ranking" else "headline"
    labels = [{"rank": m["rank"], "title": m["title"]} for m in play] if project["format"] == "ranking" else None
    checks = []
    c = base_qa.check
    mean, mx = _mean_volume(out)
    checks.append(c("audio_present_not_silent", PASS if (mean is not None and mean > -55 and mx is not None and mx > -30) else FAIL, f"mean {mean} dB, max {mx} dB"))
    wf = words_fn(tr["segments"]) if tr["segments"] else (lambda media, t: [])
    checks += cutqa.boundary_checks(media, play, words_fn=wf)
    if meta["probe"]["has_audio"]:
        checks += cutqa.sync_checks(out, media, play, offs)
    checks += cutqa.edit_point_checks(out, offs)
    unconf = [u for r in rec["captions"] for u in r["unconfirmed_words"]]
    checks.append(c("caption_accuracy", FAIL if unconf else PASS, "; ".join(unconf[:3]) if unconf else "every caption word confirmed by two independent decodes"))
    sev = [f for r in rec["captions"] for f in r["severe_findings"]]
    checks.append(c("offensive_written_captions", FAIL if sev else PASS, f"severe language in captions: {sev[:2]}" if sev else "no hateful/severe language in written captions (profanity stays uncensored by policy)"))
    scans = rec["chat_scan"]
    hits = [f for s in scans for f in (s.get("findings") or [])]
    unverified = any(not s.get("verified_clean") for s in scans)
    checks.append(c("onscreen_text_safe", FAIL if hits else PASS, f"severe on-screen text: {hits[:2]}" if hits else "no severe on-screen text found"))
    if not hits and unverified:
        checks.append(c("onscreen_text_unverified", MANUAL, "OCR could not verify all on-screen text: " + "; ".join(str(s.get("note")) for s in scans if s.get("note"))[:200]))
    if project["format"] == "ranking":
        try:
            titles.validate_entries([{"rank": m["rank"], "title": m["title"]} for m in play])
            order_ok = [m["rank"] for m in play] == sorted([m["rank"] for m in play], reverse=True)
            checks.append(c("ranking_numbers", PASS if order_ok else FAIL, "badge, position and countdown share one rank: " + ", ".join(f"#{m['rank']}" for m in play)))
        except titles.TitleError as exc:
            checks.append(c("ranking_numbers", FAIL, str(exc)))
    missing = [m["candidate_id"] for m in play if not (m.get("payoff") or "").strip() and not m.get("payoff_audio")]
    checks.append(c("setup_and_payoff_present", FAIL if missing else PASS, f"no recorded payoff for {missing}" if missing else "each entry records its hook, setup and payoff"))
    checks.append(c("truthful_edit", PASS, "chronology preserved inside every entry; no fabricated dialogue, teasers or reordering within an entry"
                                           + ("; reuse recorded: " + project["reuse"]["reason"] if project.get("reuse") else "")))
    tracked = [l for l in rec["layouts"] if l["layout"] == "face-track"]
    if tracked:
        fv = face_visibility(out)
        ok = fv["samples"] > 0 and fv["with_face"] / fv["samples"] >= 0.7
        checks.append(c("subject_tracking_keeps_face", PASS if ok else FAIL, f"face visible in {fv['with_face']}/{fv['samples']} output samples"))
    else:
        checks.append(c("subject_tracking_keeps_face", PASS, "not applicable (no tracked crop used)"))
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    dup = [p.name for p in (siblings or []) if p.exists() and p != out and hashlib.sha256(p.read_bytes()).hexdigest() == digest]
    checks.append(c("not_a_duplicate_output", FAIL if dup else PASS, f"byte-identical to {dup}" if dup else "unique file"))
    rep = base_qa.run_qa(out, d / "qa", labels=labels, fmt=fmt, extra=checks)
    ok, reasons = decide(rep)
    rep["approved"], rep["reasons"], rep["sha256"] = ok, reasons, digest
    return rep


def decide(report: dict) -> tuple[bool, list[str]]:
    reasons = []
    for c in report["checks"]:
        if c["status"] == FAIL:
            reasons.append(f"FAIL {c['check']}: {c['detail']}".strip())
        elif c["status"] == MANUAL and c["check"].split("[")[0] not in ALLOWED_MANUAL:
            reasons.append(f"uncertain {c['check']}: {c['detail']}".strip())
    return (not reasons), reasons
