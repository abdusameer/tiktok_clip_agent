"""/make-clips orchestration: register -> analyse (whole source) -> candidates -> review -> ranking + standalone passes -> render -> QA -> organise.

No routine questions: it runs start to finish. Hard stops (exit code 2): missing rights status for a movie/show, a missing source, a failed policy check.
Nothing is uploaded, published or deleted; sources are never modified.
"""
import json
import shutil
import subprocess
import time
from pathlib import Path

from . import analysis, events, passes, qa as clips_qa, render as clips_render, review
from . import source as source_mod
from .common import ClipsError, RightsMissing, clock, ensure_layout, read_json, root as root_of, run_dir, write_json

STAGES = ("analysis", "candidates", "review-pack", "passes", "render", "qa")
HASHTAGS = {"streamer": "#streamer #funny #gaming", "gaming": "#gaming #gameplay #funny", "movie": "#movie #scene #cinema", "show": "#tvshow #scene #clip",
            "podcast": "#podcast #funny #clips", "interview": "#interview #clips #moments"}


class Log:
    def __init__(self, root, source_id, echo=print):
        self.path = root_of(root) / "logs" / f"make-clips-{source_id}-{time.strftime('%Y%m%d-%H%M%S')}.log"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.echo = echo

    def __call__(self, msg: str) -> None:
        with self.path.open("a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        if self.echo:
            self.echo(msg)


def _status(d: Path, stage: str, **extra) -> None:
    write_json(d / "STATUS.json", {"stage": stage, "updated": time.strftime("%Y-%m-%d %H:%M:%S"), **extra})


def apply_content_type(d: Path, requested: str) -> dict:
    ct = read_json(d / "analysis" / "content_type.json")
    if "detected" not in ct:
        ct["detected"] = ct["content_type"]
    if requested != "auto":
        ct["content_type"], ct["source"] = requested, "user"
        ct["low_confidence"] = False
    else:
        ct["content_type"], ct["source"] = ct["detected"], "auto-detected"
    write_json(d / "analysis" / "content_type.json", ct)
    return ct


def transcript_confidence(d: Path, a: float, b: float) -> float | None:
    p = d / "transcripts" / "pass1.json"
    if not p.is_file():
        return None
    probs = [w["prob"] for s in read_json(p)["segments"] for w in s.get("words", []) if a <= w["start"] <= b]
    return round(sum(probs) / len(probs), 3) if probs else None


def build_manifest(source_id: str, d: Path, meta: dict, ct: dict, project: dict, rec: dict, report: dict, cand: dict) -> dict:
    play = sorted(project["moments"], key=lambda m: -m["rank"])
    lay = {l["candidate_id"]: l for l in rec["layouts"]}
    caps = {c["rank"]: c for c in rec["captions"]}
    return {
        "output": Path(rec["output"]).name, "source_id": source_id, "source_file": meta["name"], "source_sha256": meta["sha256"], "content_type": ct["content_type"],
        "content_type_detection": {"source": ct.get("source"), "confidence": ct.get("confidence"), "evidence": ct.get("evidence")},
        "format": project["format"], "project_id": project["id"], "theme": project.get("category"), "headline": None if project["format"] == "ranking" else play[0]["title"],
        "candidate_ids": [m["candidate_id"] for m in play],
        "source_timecodes": [{"rank": m["rank"], "title": m["title"], "start": m["start"], "end": m["end"], "start_clock": clock(m["start"]), "end_clock": clock(m["end"]),
                              "candidate_id": m["candidate_id"], "peak_t": m.get("peak_t"), "trim_reason": m.get("trim_reason")} for m in play],
        "editorial": [{"rank": m["rank"], "candidate_id": m["candidate_id"], "candidate_score": cand[m["candidate_id"]]["score"], "tags": cand[m["candidate_id"]]["tags"],
                       "drivers": cand[m["candidate_id"]]["evidence"]["drivers"]} for m in play],
        "hook": [m["hook"] for m in play], "setup": [m["setup"] for m in play], "payoff": [m["payoff"] for m in play],
        "final_runtime_sec": round(sum(m["end"] - m["start"] for m in play), 2), "runtime_note": "metadata only; never a quality input",
        "transcript_confidence": [{"rank": m["rank"], "whisper_pass1_mean_word_probability": transcript_confidence(d, m["start"], m["end"]),
                                   **caps[m["rank"]]["confidence"]} for m in play],
        "visual_evidence": [{"rank": m["rank"], "candidate_id": m["candidate_id"], "evidence": cand[m["candidate_id"]]["visual_evidence"],
                             "visual_review": m.get("visual_review", "see reviews.effective.json"), "onscreen_text": next(s for s in rec["chat_scan"] if s["rank"] == m["rank"])} for m in play],
        "audio_evidence": [{"rank": m["rank"], "candidate_id": m["candidate_id"], "events": cand[m["candidate_id"]]["audio_evidence"],
                            "drivers": cand[m["candidate_id"]]["evidence"]["drivers"]} for m in play],
        "reframing": [{"rank": m["rank"], "layout": lay[m["candidate_id"]]["layout"], "reason": lay[m["candidate_id"]]["reason"], "detail": lay[m["candidate_id"]].get("reframe")} for m in play],
        "editing_decisions": [{"rank": m["rank"], "treatment": m.get("treatment"), "start_reason": cand[m["candidate_id"]]["start_reason"],
                               "end_reason": cand[m["candidate_id"]]["end_reason"], "effects": lay[m["candidate_id"]]["effects"],
                               "captions": "words confirmed by two independent Whisper decodes; profanity uncensored", "accent_color": rec.get("accent_color")} for m in play],
        "reused_material": project.get("reuse"),
        "rights": meta.get("rights"), "rights_note": meta.get("rights_note"),
        "qa": {"approved": report["approved"], "verdict": report["verdict"], "reasons": report["reasons"], "checks": report["checks"], "sha256": report["sha256"]},
        "status": "ready_for_draft_transfer" if report["approved"] else "needs_review",
        "publishing": "never uploaded or published by /make-clips; public publishing and draft transfer need the user's explicit approval",
    }


def organise(source_id: str, root, project: dict, rec: dict, report: dict, manifest: dict) -> Path:
    base = root_of(root)
    stem = f"{project['id']}-{project['name']}"
    if report["approved"]:
        dest = base / "approved" / ("rankings" if project["format"] == "ranking" else "standalone") / source_id / stem
    else:
        dest = base / "needs-review" / source_id / stem
    dest.mkdir(parents=True, exist_ok=True)
    mp4 = Path(rec["output"])
    shutil.move(str(mp4), dest / mp4.name)
    qa_json = run_dir(source_id, root) / "qa" / f"{mp4.stem}.qa.json"
    if qa_json.is_file():
        shutil.copy(qa_json, dest / "qa.json")
    sheet = run_dir(source_id, root) / "qa" / f"{mp4.stem}.contact.png"
    if sheet.is_file():
        shutil.copy(sheet, dest / "contact-sheet.png")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{manifest['final_runtime_sec'] * 0.4:.2f}", "-i", str(dest / mp4.name), "-frames:v", "1", "-q:v", "3", str(dest / "cover.jpg")], capture_output=True)
    title = manifest["headline"] or f"Ranking the {manifest['theme'].lower()}"
    (dest / "caption.txt").write_text(f"{title}\n\n{HASHTAGS[manifest['content_type']]}\n")
    manifest["location"] = str(dest.relative_to(base))
    write_json(dest / "manifest.json", manifest)
    if not report["approved"]:
        (dest / "FAILURE.txt").write_text("This video did not pass automatic QA and was NOT discarded.\n\n" + "\n".join(report["reasons"]) + "\n")
    write_json(run_dir(source_id, root) / "manifests" / f"{stem}.json", manifest)
    return dest


def process(video: Path, content_type: str = "auto", platform: str = "tiktok", root=None, rights: str | None = None, reviews: Path | None = None,
            until: str = "qa", force: bool = False, model: str = "small.en", hz: float = 4.0, echo=print) -> dict:
    if platform != "tiktok":
        raise ClipsError("only --platform tiktok is supported (1080x1920 H.264/AAC, safe areas from config/ranking_style.json)")
    meta = source_mod.register(video, rights, content_type, root)
    sid = meta["source_id"]
    d = ensure_layout(sid, root)
    log = Log(root, sid, echo)
    log(f"source {meta['name']} -> id {sid} ({clock(meta['probe']['duration_sec'])}, {meta['probe']['width']}x{meta['probe']['height']}, audio={meta['probe']['has_audio']})")
    result = {"source_id": sid, "source": meta["name"], "stopped": None, "approved": [], "needs_review": [], "log": str(log.path)}
    try:
        _status(d, "ANALYZING")
        log("analysing the complete source (shot cuts, audio events, transcript, speaker proxy, content type) ...")
        analysis.run(sid, root, force=force, model=model, hz=hz, progress=log)
        ct = apply_content_type(d, content_type)
        source_mod.enforce_rights(ct["content_type"], meta.get("rights"))
        log(f"content type: {ct['content_type']} ({ct['source']}, confidence {ct.get('confidence')}; {'; '.join(ct.get('evidence', [])[:3])})")
        result["content_type"] = ct["content_type"]
        if until == "analysis":
            return result
        _status(d, "CANDIDATES")
        led = events.build_candidates(sid, root, ct["content_type"])
        log(f"candidates: {led['n_candidates']} events recovered from {led['n_peaks']} interest peaks")
        result["candidates"] = led["n_candidates"]
        if until == "candidates":
            return result
        review.review_pack(sid, root)
        log(f"review pack written: runs/{sid}/candidates/review_pack/ (agent reviews go to candidates/reviews.json)")
        if until == "review-pack":
            return result
        rv = review.build_reviews(sid, root, reviews)
        acc = [r for r in rv["reviews"] if r["verdict"] == "accept"]
        log(f"reviews: {len(acc)} accepted, {len(rv['reviews']) - len(acc)} rejected ({rv['agent']} agent / {rv['auto']} auto)")
        _status(d, "PLANNING")
        plan = passes.build(sid, root)
        log(f"passes: {sum(v['format'] == 'ranking' for v in plan['videos'])} ranking(s), {sum(v['format'] == 'standalone' for v in plan['videos'])} standalone(s)")
        for why in plan["ranking_none"] + plan["standalone_none"]:
            log(f"  not made: {why}")
        result["planned"] = [v["id"] for v in plan["videos"]]
        if until == "passes":
            return result
        _status(d, "RENDERING")
        cands = {c["id"]: c for c in read_json(d / "candidates" / "ledger.json")["candidates"]}
        rvx = {r["id"]: r for r in rv["reviews"]}
        done = []
        for project in plan["videos"]:
            for m in project["moments"]:
                m["visual_review"] = rvx[m["candidate_id"]].get("visual_review", "agent")
            stem = f"{project['id']}-{project['name']}"
            if (d / "manifests" / f"{stem}.json").is_file():                   # finished in an earlier (interrupted) run: resume, never redo or duplicate
                log(f"  {project['id']} already produced: skipped")
                continue
            orphan = d / "renders" / f"{stem}.mp4"
            if orphan.exists():                                                 # a render with no manifest is a crash leftover: set it aside (never deleted) and redo
                orphan.rename(orphan.with_name(f"{stem}.stale-{int(time.time())}.mp4"))
            try:
                log(f"  render {project['id']} {project['format']} ({project['runtime_sec']}s)")
                rec = clips_render.render_project(sid, project, root, progress=log)
            except Exception as exc:
                log(f"  RENDER FAILED {project['id']}: {exc}")
                dest = root_of(root) / "needs-review" / sid / f"{project['id']}-{project['name']}"
                dest.mkdir(parents=True, exist_ok=True)
                (dest / "FAILURE.txt").write_text(f"render failed (not discarded): {type(exc).__name__}: {exc}\n")
                write_json(dest / "project.json", project)
                result["needs_review"].append({"id": project["id"], "reason": str(exc)})
                continue
            done.append((project, rec))
        if until == "render":
            return result
        _status(d, "QA")
        outs = [Path(r["output"]) for _, r in done]
        for project, rec in done:
            report = clips_qa.run(sid, project, rec, root, siblings=outs)
            manifest = build_manifest(sid, d, meta, ct, project, rec, report, cands)
            dest = organise(sid, root, project, rec, report, manifest)
            row = {"id": project["id"], "format": project["format"], "name": project["name"], "runtime_sec": manifest["final_runtime_sec"], "location": manifest["location"]}
            if report["approved"]:
                result["approved"].append(row)
                log(f"  APPROVED {project['id']} -> {manifest['location']}")
            else:
                result["needs_review"].append({**row, "reason": "; ".join(report["reasons"])})
                log(f"  NEEDS REVIEW {project['id']}: {'; '.join(report['reasons'])[:300]}")
        _status(d, "COMPLETE", approved=len(result["approved"]), needs_review=len(result["needs_review"]))
    except RightsMissing as exc:
        result["stopped"] = str(exc)
        _status(d, "STOPPED", reason=str(exc))
        log(f"STOP: {exc}")
    finally:
        write_report(d, result)
    return result


def write_report(d: Path, r: dict) -> None:
    lines = [f"# /make-clips report: {r['source']} ({r['source_id']})", "", f"content type: {r.get('content_type', 'n/a')}", f"candidates recovered: {r.get('candidates', 'n/a')}"]
    if r.get("stopped"):
        lines += ["", f"STOPPED: {r['stopped']}"]
    lines += ["", f"**Final videos approved: {len(r['approved'])}**  |  needs review: {len(r['needs_review'])}", ""]
    for a in r["approved"]:
        lines.append(f"- {a['id']} [{a['format']}] {a['name']}  {a['runtime_sec']}s  ->  {a['location']}")
    for n in r["needs_review"]:
        lines.append(f"- NEEDS REVIEW {n.get('id')}: {n.get('reason', '')[:300]}")
    lines += ["", f"log: {r['log']}", "Nothing was uploaded, published or deleted."]
    (d / "REPORT.md").write_text("\n".join(lines) + "\n")
    write_json(d / "result.json", r)


def find_sources(source: Path | None, root=None) -> list[Path]:
    """--source may be a video file, a folder, or omitted (-> everything in sourcevideo/)."""
    return source_mod.resolve_inputs(source, root_of(root))
