"""Benchmark the /make-clips stages on this machine using a slice of any authorized local video.

    PYTHONPATH=src .venv/bin/python scripts/benchmark.py --source <video> --minutes 10 [--out docs/benchmarks.json]

Times each stage on its own (separate worker processes, exactly as in production), reports the real-time factor (stage seconds per media second) and
projects wall time for 2 h and 4 h sources. The slice is cut with `-c copy` into a temp folder and removed afterwards (only that temp file).
"""
import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def run(args, **kw):
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    t = time.time()
    r = subprocess.run(args, capture_output=True, text=True, env=env, **kw)
    return time.time() - t, r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--start", type=float, default=0.0, help="slice start, seconds")
    ap.add_argument("--model", default="small.en")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    dur = a.minutes * 60
    with tempfile.TemporaryDirectory(prefix="clipbench-") as td:
        sl = Path(td) / "slice.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(a.start), "-t", str(dur), "-i", a.source, "-c", "copy", str(sl)], check=True)
        info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(sl)], capture_output=True, text=True).stdout)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        media_sec = float(info["format"]["duration"])
        out = {"machine": platform.platform(), "cpu": subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip(),
               "ram_gb": round(int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout) / 2 ** 30, 1),
               "slice": {"seconds": round(media_sec, 1), "resolution": f"{v['width']}x{v['height']}", "codec": v["codec_name"]}, "stages": {}}
        stages = {
            "visual (PySceneDetect + 4 Hz motion/faces/bars)": [sys.executable, "-m", "clipagent.clips.visual_worker", str(sl), str(Path(td) / "vis"), "--hz", "4"],
            f"transcript ({a.model}, word timestamps, VAD)": [sys.executable, "-m", "clipagent.clips.transcript_worker", str(sl), str(Path(td) / "tr.json"), "--model", a.model],
        }
        for name, cmd in stages.items():
            sec, r = run(cmd)
            out["stages"][name] = {"seconds": round(sec, 1), "rtf": round(sec / media_sec, 3), "ok": r.returncode == 0}
        code = ("from pathlib import Path;from clipagent.clips import audio;import time;t=time.time();audio.analyze(Path(%r), Path(%r)/'audio');print(time.time()-t)" % (str(sl), td))
        sec, r = run([sys.executable, "-c", code])
        out["stages"]["audio events (loudness, laughter rhythm, silences)"] = {"seconds": round(sec, 1), "rtf": round(sec / media_sec, 4), "ok": r.returncode == 0}
        total_rtf = sum(s["rtf"] for s in out["stages"].values())
        out["projection_hours_wall_if_run_serially"] = {f"{h} h source": round(total_rtf * h, 2) for h in (1, 2, 4)}
        out["note"] = "visual and transcript workers run in parallel in production, so wall time is nearer max(stage) than the sum; render/QA time scales with the number of videos, not source length"
    text = json.dumps(out, indent=2)
    print(text)
    if a.out:
        Path(a.out).write_text(text + "\n")


if __name__ == "__main__":
    main()
