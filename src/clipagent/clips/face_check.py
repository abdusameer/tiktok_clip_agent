"""QA worker: how often is a face visible in a rendered clip? `python -m clipagent.clips.face_check VIDEO` prints {"samples": n, "with_face": k}.
Own process (OpenCV must not share a process with faster-whisper's PyAV)."""
import json
import sys

from . import visual_worker as vw


def main(argv=None) -> int:
    import cv2
    from .common import probe
    from pathlib import Path
    video = (argv or sys.argv[1:])[0]
    info = probe(Path(video))
    det = cv2.FaceDetectorYN.create(str(vw.MODEL), "", (vw.SAMPLE_W, 360), score_threshold=0.7, nms_threshold=0.3, top_k=8)
    n = k = 0
    for _, fr, (w, h) in vw.sample_frames(video, 2.0, info["width"], info["height"]):
        det.setInputSize((w, h))
        _, res = det.detect(fr)
        n += 1
        k += int(res is not None and len(res) > 0)
    print(json.dumps({"samples": n, "with_face": k}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
