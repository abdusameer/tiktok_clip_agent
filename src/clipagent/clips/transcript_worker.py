"""Transcription worker (own process; faster-whisper + PyAV, never loaded together with OpenCV).
`python -m clipagent.clips.transcript_worker MEDIA OUT.json [--model small.en]`"""
import argparse
import json
import sys
from pathlib import Path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("media")
    ap.add_argument("out")
    ap.add_argument("--model", default="small.en")
    a = ap.parse_args(argv)
    from .. import transcribe as tr
    segs, info = tr._whisper_run(Path(a.media), a.model, lambda t: print(f"asr {t:.0f}s", file=sys.stderr, flush=True))
    Path(a.out).write_text(json.dumps({"model": a.model, **info, "pass": 1, "segments": segs}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
