"""Run layout and small shared helpers."""
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from .. import config

SUBDIRS = ("analysis", "transcripts", "candidates", "projects/rankings", "projects/standalone", "renders", "manifests", "qa", "work")


class ClipsError(Exception):
    pass


class RightsMissing(ClipsError):
    """Copyrighted-type source (movie/show) without an explicit rights status: the run must stop, never guess."""


def root(r=None) -> Path:
    return Path(r) if r else config.project_root()


def run_dir(source_id: str, r=None) -> Path:
    return root(r) / "runs" / source_id


def ensure_layout(source_id: str, r=None) -> Path:
    d = run_dir(source_id, r)
    for s in SUBDIRS:
        (d / s).mkdir(parents=True, exist_ok=True)
    for top in ("approved/rankings", "approved/standalone", "needs-review", "logs"):
        (root(r) / top).mkdir(parents=True, exist_ok=True)
    return d


def write_json(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=_default))
    os.replace(tmp, path)
    return path


def _default(o):
    try:
        import numpy as np
        if isinstance(o, np.generic):
            return o.item()
        if isinstance(o, np.ndarray):
            return o.tolist()
    except ImportError:
        pass
    raise TypeError(f"not JSON serializable: {type(o)}")


def read_json(path: Path):
    return json.loads(Path(path).read_text())


def sha256_file(path: Path, block: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(block):
            h.update(chunk)
    return h.hexdigest()


def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit] or "clip"


def probe(path: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], capture_output=True, text=True)
    if r.returncode:
        raise ClipsError(f"ffprobe failed for {path.name}: {r.stderr.strip()}")
    info = json.loads(r.stdout)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if v is None:
        raise ClipsError(f"{path.name} has no video stream")
    num, den = (v.get("avg_frame_rate") or "0/1").split("/")
    fps = float(num) / float(den) if float(den) else 0.0
    return {"duration_sec": float(info["format"]["duration"]), "width": int(v["width"]), "height": int(v["height"]), "fps": round(fps, 3),
            "video_codec": v["codec_name"], "has_audio": a is not None, "audio_codec": a["codec_name"] if a else None,
            "size_bytes": int(info["format"].get("size", 0))}


def clock(sec: float) -> str:
    sec = max(0.0, float(sec))
    return f"{int(sec // 3600):02d}:{int(sec % 3600 // 60):02d}:{sec % 60:05.2f}"
