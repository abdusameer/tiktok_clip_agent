"""Visual analysis worker. Runs in its OWN process (`python -m clipagent.clips.visual_worker VIDEO OUT_PREFIX [--hz 4]`):
OpenCV and faster-whisper's PyAV each bundle ffmpeg libraries, and macOS warns about loading both in one process.

Outputs OUT_PREFIX.json (cuts, faces, letterbox bars, sample geometry) and OUT_PREFIX.npz (t, motion, hist, nfaces arrays).
  cuts   - PySceneDetect AdaptiveDetector (shot boundaries; inputs only, never event boundaries)
  motion - mean absolute frame difference on a 160x90 luma image, per sample
  hist   - 48-bin colour histogram per sample (used to tell a camera-angle change from a real scene change)
  faces  - YuNet detections per sample, normalised [x, y, w, h, score]
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

SAMPLE_W = 640
MODEL = Path(__file__).resolve().parents[3] / "models" / "yunet" / "face_detection_yunet_2023mar.onnx"


def detect_cuts(video: str, frame_skip: int = 0) -> list[dict]:
    from scenedetect import AdaptiveDetector, SceneManager, open_video
    vid = open_video(video)
    sm = SceneManager()
    sm.add_detector(AdaptiveDetector())
    sm.detect_scenes(video=vid, frame_skip=frame_skip, show_progress=False)
    scenes = sm.get_scene_list()
    return [{"t": round(s.seconds, 3), "frame": s.frame_num} for s, _ in scenes[1:]]


def sample_frames(video: str, hz: float, width: int, height: int):
    """Yield (t, bgr frame at SAMPLE_W) from an ffmpeg pipe."""
    h = max(2, round(SAMPLE_W * height / width / 2) * 2)
    cmd = ["ffmpeg", "-v", "error", "-i", video, "-an", "-vf", f"fps={hz},scale={SAMPLE_W}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = SAMPLE_W * h * 3
    i = 0
    while True:
        buf = p.stdout.read(size)
        if len(buf) < size:
            break
        yield i / hz, np.frombuffer(buf, np.uint8).reshape(h, SAMPLE_W, 3), (SAMPLE_W, h)
        i += 1
    p.stdout.close()
    p.wait()


def analyze(video: str, hz: float, frame_skip: int = 0, progress=None) -> tuple[dict, dict]:
    import cv2
    from .common import probe
    info = probe(Path(video))
    det = cv2.FaceDetectorYN.create(str(MODEL), "", (SAMPLE_W, 360), score_threshold=0.7, nms_threshold=0.3, top_k=8) if MODEL.is_file() else None
    ts, motion, hist, nfaces, faces = [], [], [], [], []
    prev = None
    row_dark = None
    n_rows = None
    for t, fr, (w, h) in sample_frames(video, hz, info["width"], info["height"]):
        gray = cv2.cvtColor(cv2.resize(fr, (160, 90)), cv2.COLOR_BGR2GRAY).astype(np.float32)
        motion.append(0.0 if prev is None else float(np.abs(gray - prev).mean()))
        prev = gray
        hh = np.concatenate([np.histogram(fr[:, :, c], bins=16, range=(0, 256))[0] for c in range(3)]).astype(np.float32)
        hist.append((hh / hh.sum()).astype(np.float16))
        found = []
        if det is not None:
            det.setInputSize((w, h))
            _, res = det.detect(fr)
            if res is not None:
                found = [[float(r[0]) / w, float(r[1]) / h, float(r[2]) / w, float(r[3]) / h, float(r[14])] for r in res]
        faces.append(found)
        nfaces.append(len(found))
        ts.append(t)
        rows = (gray_rows := cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY).mean(axis=1)) < 16          # near-black rows (letterbox)
        row_dark = rows.astype(np.float32) if row_dark is None else row_dark + rows
        n_rows = len(rows)
        if progress and len(ts) % 400 == 0:
            progress(t)
    n = max(len(ts), 1)
    dark = (row_dark / n) > 0.95 if row_dark is not None else np.zeros(1, bool)
    top = int(np.argmin(dark)) if dark.any() and not dark.all() else 0
    bottom = int(np.argmin(dark[::-1])) if dark.any() and not dark.all() else 0
    meta = {"hz": hz, "n_samples": len(ts), "sample_w": SAMPLE_W, "sample_h": n_rows, "faces": faces,
            "bars": {"top_frac": round(top / n_rows, 4) if n_rows else 0.0, "bottom_frac": round(bottom / n_rows, 4) if n_rows else 0.0},
            "face_model": MODEL.name if det is not None else None, "source_dims": [info["width"], info["height"]]}
    meta["cuts"] = detect_cuts(video, frame_skip)
    arrays = {"t": np.array(ts, np.float32), "motion": np.array(motion, np.float32), "hist": np.array(hist, np.float16), "nfaces": np.array(nfaces, np.int16)}
    return meta, arrays


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("out_prefix")
    ap.add_argument("--hz", type=float, default=4.0)
    ap.add_argument("--frame-skip", type=int, default=0)
    a = ap.parse_args(argv)
    meta, arr = analyze(a.video, a.hz, a.frame_skip, progress=lambda t: print(f"visual {t:.0f}s", file=sys.stderr, flush=True))
    Path(a.out_prefix).parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(a.out_prefix + ".npz", **arr)
    Path(a.out_prefix + ".json").write_text(json.dumps(meta))
    return 0


if __name__ == "__main__":
    sys.exit(main())
