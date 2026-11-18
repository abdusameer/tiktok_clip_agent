# Third-party components and licenses (/make-clips)

| Component | Version | License | Use | Source |
|---|---|---|---|---|
| FFmpeg / ffprobe | 9.0.2 (Homebrew) | LGPL/GPL (build dependent) | external executable only (not linked, not redistributed) | https://ffmpeg.org |
| faster-whisper | 1.2.1 | MIT | transcripts, word timestamps, VAD | https://github.com/SYSTRAN/faster-whisper |
| CTranslate2 | 4.8.2 | MIT | ASR runtime | https://github.com/OpenNMT/CTranslate2 |
| Whisper models (small.en, medium.en) | Systran conversions | MIT (OpenAI Whisper weights) | downloaded to the local Hugging Face cache on first use | https://huggingface.co/Systran |
| PySceneDetect | 0.7.3 | BSD-3-Clause | shot boundaries (AdaptiveDetector) | https://github.com/Breakthrough/PySceneDetect |
| opencv-python | 5.1.0.95 | Apache-2.0 (wheel bundles LGPL FFmpeg libs) | frame sampling, motion, face detection runtime | https://github.com/opencv/opencv |
| YuNet `face_detection_yunet_2023mar.onnx` | opencv_zoo | MIT (see `licenses/YuNet-LICENSE.txt`), sha256 `8f2383e4...2fa4` | face tracking | https://github.com/opencv/opencv_zoo |
| NumPy | 2.5.4 | BSD-3-Clause | signal math | https://numpy.org |
| Pillow (system python) | system | HPND | text overlays | https://python-pillow.org |
| Typer | project dependency | MIT | CLI | https://typer.tiangolo.com |

Audited but NOT used (reasons in `research/github_clipping_agent_audit.md`): whisperX, pyannote-audio (telemetry + gated models), TransNetV2, MediaPipe, PANNs, ClipsAI, AI-Youtube-Shorts-Generator (cloud-first), and every AGPL or unlicensed clipper. No code was copied from any repository.

Test media: `tests/fixtures/make_fixture.py` generates a synthetic stream with macOS text-to-speech (`say`), synthesized screams/laughter and ffmpeg test patterns. The script and output are dedicated to the public domain (CC0). No third-party footage is used.
