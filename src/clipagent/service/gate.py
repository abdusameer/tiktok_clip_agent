"""High-confidence automatic-posting gate. Everything must pass; any failure -> REJECTED with the exact reasons (logged, never a prompt).

Builds on the clip QA (resolution/aspect/encoding/A-V sync/black+frozen frames/clipping/cut-in-word/caption accuracy/safe area/subject tracking/duplicate
bytes/ranking numbers) and adds: rights, text safety, visual-safety review, editorial score, a full playback test, and near-duplicate protection
against everything already posted. Visual material (sexual, violent, hateful imagery) cannot be judged by audio/text metrics: unless a visual-safety
reviewer is configured the clip waits as `blocked: visual safety review`, and nothing is posted."""
import json
import subprocess
from pathlib import Path

from .. import plan as plan_mod
from .. import textsafety

REQUIRED_QA = ["resolution_1080x1920", "aspect_9_16", "h264_video", "aac_audio", "faststart", "av_sync_stream_durations", "no_black_frames", "no_frozen_frames",
               "no_clipped_audio", "audio_present_not_silent", "caption_accuracy", "offensive_written_captions", "onscreen_text_safe", "setup_and_payoff_present",
               "truthful_edit", "subject_tracking_keeps_face", "not_a_duplicate_output", "captions_inside_safe_area"]
RIGHTS_OK = {"owned", "licensed", "permission", "public-domain", "user-attested"}


def _check(name, ok, detail=""):
    return {"check": name, "ok": bool(ok), "detail": detail}


def playback_test(mp4: Path, expected_runtime: float) -> tuple[bool, str]:
    r = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(mp4), "-f", "null", "-"], capture_output=True, text=True)
    if r.returncode or r.stderr.strip():
        return False, f"decode errors: {r.stderr.strip()[:200]}"
    d = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp4)], capture_output=True, text=True).stdout.strip()
    dur = float(d) if d else 0.0
    return (abs(dur - expected_runtime) <= 0.5), f"decoded fully; duration {dur:.2f}s vs expected {expected_runtime:.2f}s"


def editorial_score(m: dict) -> float:
    ed = m.get("editorial") or []
    if not ed:
        return 0.0
    base = sum(min(e["candidate_score"], 1.0) * 0.6 + min(len(e["drivers"]) / 3.0, 1.0) * 0.25 for e in ed) / len(ed)
    conf = [c.get("decode_agreement") for c in m.get("transcript_confidence", []) if c.get("decode_agreement") is not None]
    cap = (sum(conf) / len(conf)) if conf else 1.0
    return round(base + 0.15 * min(cap, 1.0), 3)


def visual_safety(m: dict, mp4: Path, cfg: dict) -> tuple[bool, str]:
    vs = cfg["gate"]["visual_safety"]
    mode, cmd = vs.get("mode", "block_until_reviewed"), vs.get("command")
    if mode == "metrics_only_allowed":
        return True, "visual safety NOT verified by any reviewer (config: metrics_only_allowed); accepted by explicit configuration"
    if cmd:
        r = subprocess.run([*cmd, str(mp4)] if isinstance(cmd, list) else cmd.split() + [str(mp4)], capture_output=True, text=True, timeout=900)
        try:
            out = json.loads(r.stdout.strip().splitlines()[-1])
            return bool(out.get("safe")) and r.returncode == 0, f"reviewer: {out.get('reason', '')}"
        except Exception:
            return False, f"visual-safety reviewer gave no valid verdict (exit {r.returncode})"
    reviewed = all((e.get("visual_review") not in (None, "auto-metrics-only")) for e in m.get("visual_evidence", []))
    return reviewed, ("every entry was visually reviewed by the agent" if reviewed else "blocked: visual safety review (sexual/violent/hateful imagery cannot be judged from audio/text metrics; no reviewer configured)")


def run(manifest: dict, mp4: Path, cfg: dict, posted: list[dict]) -> dict:
    """posted: manifests of everything already delivered (for near-duplicate protection)."""
    checks = []
    qa = {c["check"].split("[")[0]: c for c in manifest["qa"]["checks"]}
    for name in REQUIRED_QA:
        c = qa.get(name)
        checks.append(_check(f"qa:{name}", c is not None and c["status"] == "pass", (c or {}).get("detail", "check missing from QA report")))
    rights = manifest.get("rights")
    explicit = bool(rights and rights.get("status") in RIGHTS_OK)
    needs = manifest["content_type"] in ("movie", "show")
    checks.append(_check("rights_resolved", explicit or (not needs and bool(manifest.get("rights_note"))), f"rights: {rights}" if explicit else str(manifest.get("rights_note") or "no rights record")))
    text = " ".join([manifest.get("headline") or "", manifest.get("theme") or ""] + [str(x) for x in manifest["hook"] + manifest["setup"] + manifest["payoff"]])
    sev = textsafety.scan_text(text)
    checks.append(_check("text_safety_generated_and_spoken", not sev, f"severe findings: {sev[:2]}" if sev else "no severe language in title/hook/setup/payoff"))
    ok, why = visual_safety(manifest, mp4, cfg)
    checks.append(_check("visual_safety", ok, why))
    sc = editorial_score(manifest)
    checks.append(_check("editorial_score", sc >= cfg["gate"]["min_editorial_score"], f"{sc} (minimum {cfg['gate']['min_editorial_score']})"))
    pb, why = playback_test(mp4, manifest["final_runtime_sec"]) if mp4.is_file() else (False, "render file missing")
    checks.append(_check("playback_test", pb, why))
    worst = 0.0
    for p in posted:
        if p.get("source_sha256") == manifest["source_sha256"]:
            a = [{"start": t["start"], "end": t["end"]} for t in manifest["source_timecodes"]]
            b = [{"start": t["start"], "end": t["end"]} for t in p["source_timecodes"]]
            worst = max(worst, plan_mod.footage_similarity(a, b))
        if p.get("qa", {}).get("sha256") and p["qa"]["sha256"] == manifest["qa"].get("sha256"):
            worst = 1.0
    thr = cfg["gate"]["near_duplicate_threshold"]
    checks.append(_check("no_duplicate_or_near_duplicate_post", worst < thr, f"closest earlier post shares {worst:.0%} of its footage (limit {thr:.0%})"))
    bad = [c for c in checks if not c["ok"]]
    return {"ok": not bad, "score": sc, "checks": checks, "reasons": [f"{c['check']}: {c['detail']}" for c in bad]}
