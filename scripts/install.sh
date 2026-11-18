#!/bin/zsh
# Reproducible install for /make-clips and the zero-touch service. Idempotent. Runs nothing from third-party repositories:
# only PyPI packages pinned in requirements.lock and ONE model file whose SHA-256 is verified.
set -euo pipefail
cd "$(dirname "$0")/.."
command -v ffmpeg >/dev/null || { echo "ffmpeg missing: brew install ffmpeg"; exit 1; }
[ -d .venv ] || python3.12 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.lock
.venv/bin/python -m pip install --disable-pip-version-check --no-deps -e .
MODEL=models/yunet/face_detection_yunet_2023mar.onnx
SHA=8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4
if [ ! -f "$MODEL" ]; then
  mkdir -p models/yunet licenses
  curl -fL -o "$MODEL" https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
  curl -fL -o licenses/YuNet-LICENSE.txt https://raw.githubusercontent.com/opencv/opencv_zoo/main/models/face_detection_yunet/LICENSE
fi
[ "$(shasum -a 256 "$MODEL" | cut -d' ' -f1)" = "$SHA" ] || { echo "YuNet checksum mismatch: refusing"; exit 1; }
PYTHONPATH=src .venv/bin/python -m clipagent.cli service init
echo "installed. Next: edit config/service.json; run 'clipagent service auth' for TikTok delivery; see docs/zero-touch-service.md"
