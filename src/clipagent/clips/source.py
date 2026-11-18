"""Register a user-provided local source: identity, probe, rights status. Never copies, moves or deletes the original."""
import subprocess
import time
from pathlib import Path

from .. import intake
from . import CONTENT_TYPES
from .common import ClipsError, RightsMissing, clock, ensure_layout, probe, read_json, sha256_file, write_json

RIGHTS_REQUIRED_TYPES = ("movie", "show")
RIGHTS_VALUES = ("owned", "licensed", "permission", "public-domain", "user-attested")
VIDEO_EXT = intake.VIDEO_EXT


def rights_from_sidecar(video: Path) -> str | None:
    """`<video>.rights.txt` or `<stem>.rights.txt` next to the media: one line, e.g. `licensed: ...`."""
    for p in (video.with_name(video.name + ".rights.txt"), video.with_suffix(".rights.txt")):
        if p.is_file():
            line = p.read_text().strip().splitlines()
            if line:
                return line[0].strip()
    return None


def parse_rights(text: str | None) -> dict | None:
    if not text:
        return None
    kind = text.split(":", 1)[0].strip().lower()
    return {"status": kind if kind in RIGHTS_VALUES else "user-attested", "detail": text.strip(), "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S")}


def enforce_rights(content_type: str, rights: dict | None) -> None:
    if content_type in RIGHTS_REQUIRED_TYPES and not rights:
        raise RightsMissing(f"{content_type} sources need an explicit rights status (--rights 'owned|licensed|permission|public-domain: details' "
                            f"or a '<file>.rights.txt'). Nothing is assumed to be fair use. Run stopped before any output.")


AUDIO_EXT = intake.AUDIO_EXT


def _pair(files: list[Path]) -> list[tuple[Path, Path | None]]:
    """Pair each video with its separate audio file (same stem / shared prefix; a lone video + lone audio pair up), like the original intake does."""
    videos = [p for p in files if p.suffix.lower() in VIDEO_EXT]
    audios = [p for p in files if p.suffix.lower() in AUDIO_EXT]
    out = []
    for v in videos:
        mate = next((a for a in audios if a.stem == v.stem or a.stem.startswith(v.stem) or v.stem.startswith(a.stem)), None)
        if mate is None and len(videos) == 1 and len(audios) == 1:
            mate = audios[0]
        out.append((v, mate))
    return out


def mux_with_audio(video: Path, audio: Path, root_dir: Path) -> Path:
    """Combine a video and its separate audio into runs/_muxed/<stem>.mp4 (video stream copied; originals untouched). The audio is assumed to start with the
    video: a duration mismatch over 2 s is refused rather than guessed."""
    dv, da = probe(video)["duration_sec"], probe_audio_duration(audio)
    if abs(dv - da) > 2.0:
        raise ClipsError(f"{video.name} ({dv:.1f}s) and {audio.name} ({da:.1f}s) differ by more than 2 s: cannot assume they are in sync")
    out = root_dir / "runs" / "_muxed" / f"{video.stem}.mp4"
    if not out.is_file():
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".part.mp4")
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                            "-shortest", "-movflags", "+faststart", str(tmp)], capture_output=True, text=True)
        if r.returncode:
            raise ClipsError(f"could not combine {video.name} with {audio.name}: {r.stderr.strip()[-300:]}")
        tmp.rename(out)
    return out


def probe_audio_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    if r.returncode or not r.stdout.strip():
        raise ClipsError(f"cannot read audio file {path.name}")
    return float(r.stdout.strip())


def _list(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file() and not p.is_symlink() and not p.name.startswith(".")) if folder.is_dir() else []


def resolve_inputs(source: Path | None, root_dir: Path) -> list[Path]:
    """--source may be a video file, a folder, or omitted (-> sourcevideo/ and sourcevideo/inbox/ only, never other project folders). A video with a
    separate audio file in the same folder is combined first."""
    if source is not None and source.is_file():
        if source.suffix.lower() not in VIDEO_EXT:
            raise ClipsError(f"unsupported source type {source.suffix}; expected one of {sorted(VIDEO_EXT)}")
        return [source]
    if source is not None and not source.is_dir():
        raise ClipsError(f"source not found: {source}")
    folders = [source] if source is not None else [root_dir / intake.INBOX, root_dir / intake.INBOX / "inbox"]
    out = []
    for folder in folders:
        for v, a in _pair(_list(folder)):
            out.append(mux_with_audio(v, a, root_dir) if a is not None else v)
    return out


def register(video: Path, rights: str | None, content_type: str, root_dir: Path | None = None) -> dict:
    """Idempotent: same bytes -> same source id (first 12 hex of sha256). Writes runs/<id>/source.json."""
    video = Path(video).resolve()
    if content_type not in CONTENT_TYPES + ("auto",):
        raise ClipsError(f"unknown content type {content_type!r}")
    rec = parse_rights(rights or rights_from_sidecar(video))
    if content_type != "auto":
        enforce_rights(content_type, rec)
    info = probe(video)
    sha = sha256_file(video)
    sid = sha[:12]
    d = ensure_layout(sid, root_dir)
    p = d / "source.json"
    if p.is_file():
        old = read_json(p)
        if old.get("sha256") == sha:
            if rec and not old.get("rights"):
                old["rights"] = rec
                write_json(p, old)
            return old
    meta = {"source_id": sid, "path": str(video), "name": video.name, "sha256": sha, "probe": info, "duration_clock": clock(info["duration_sec"]),
            "requested_content_type": content_type, "rights": rec,
            "rights_note": None if rec else "no rights statement; processed only because the content type is a user-owned/streamed category (user placed the file in the project)",
            "registered_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if not info["has_audio"]:
        meta["warnings"] = ["source has no audio stream: transcript, audio events and captions are unavailable"]
    write_json(p, meta)
    return meta
