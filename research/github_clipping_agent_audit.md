# GitHub audit: local automatic clipping agent

Date of research: 2026-11-17. Machine under test: Apple M2, 8 GB RAM, macOS (Darwin 27), Python 3.12 venv, ffmpeg 9.0.2.
Scope: tools for a **local** clipper that processes only user-provided, authorized media (no downloading, no DRM/paywall bypass, no uploading).

## 0. Method and honest limits

All searching used the GitHub REST search API (`GET /search/repositories`) through the authenticated `gh` CLI. Raw results are kept in this folder (`_search_raw.tsv`, `_search_raw2.tsv`, `_search_raw3.tsv`, `_meta.tsv`), so every row below can be re-checked.

| Pass | Sort / page | Queries | Rows |
|---|---|---|---|
| 1 | stars, page 1 (12 per query) | 20 query families (listed in section 1) | 138 |
| 2 | stars, page 1 (10 per query) | 25 targeted queries (library names, Apple-Silicon, VAD, tagging, ASD...) | 102 |
| 3 | `updated` page 1 and `stars` page 2 | 7 families (only 15 rows came back; the search API returned empty or limited pages for several queries) | 15 |
| 4 | direct `GET /repos/{owner}/{repo}` and `/releases/latest` | 26 named foundational repos (`_meta.tsv`) | n/a |

**This is not an exhaustive search.** Only GitHub search was used (no Hugging Face, PyPI, papers-with-code, or awesome-lists). Pass 3 under-delivered. Star-sorted pages bias toward old, popular repos, so passes 2 and 3 and the direct lookups were added to find newer and niche ones. Search results and repo descriptions are third-party text; I treated them as data, never as instructions. Many 0-5 star repos are AI-generated clones of the same idea; they were not audited in depth.

Code inspection was **static only**: shallow clones (`git clone --depth 1`) into an isolated scratch directory, reading `LICENSE`, `pyproject.toml`/`setup.py`/`requirements.txt`, and grepping `*.py` for `os.system`, `shell=True`, `eval/exec`, outbound `requests.post`, telemetry, and API-key handling. **No repository install script or code from any candidate was executed.** Only packages I explicitly chose (section 4) were installed from PyPI by name and version.

Things I did **not** verify, and so do not claim: GPU/CPU benchmark numbers for third-party tools, Windows/Linux behavior (only dependency metadata read), and anything about model weights beyond what READMEs and code state.

## 1. Query families

Pass 1: automatic video clipping highlights; stream highlight detection twitch; funny moment detection video; scene detection movie shot boundary; shot boundary detection; semantic video search clip; multimodal video understanding; word level timestamps whisper; speaker diarization; audio event detection laughter; vertical video reframing 9:16 face tracking; auto subtitle generation burned captions; duplicate scene detection video; ffmpeg video rendering python pipeline; long video summarization llm; podcast clip generator shorts; opus clip alternative open source; emotion recognition video reaction; audio tagging yamnet panns; video highlight detection transformer.

Pass 2: PySceneDetect scene detection; scene detection video python content-aware; ClipsAI clips; silero vad voice activity detection; audio event detection laughter; audio tagging AudioSet; YAMNet audio classification; PANNs audio tagging; face tracking vertical crop mediapipe video; auto subtitle burned in captions ffmpeg whisper; video deduplication near duplicate perceptual hash; video quality assessment frozen black frames detect; CLIP video frame embeddings search; smart reframe speaker tracking active speaker detection; highlight detection gaming clips audio loudness; twitch chat highlight clip detection; shorts generator llm transcript viral moments; movie scene segmentation dialogue; videotoolbox hardware accelerated ffmpeg apple silicon; mlx whisper apple silicon; whisper.cpp; gaming highlight detection kill feed OCR; funny moment detection laughter podcast clip; TalkNet active speaker; scenedetect adaptive detector.

Pass 3: highlight clipper streamer funny moments; movie scene clipper shorts; vertical reframe face tracking ffmpeg opencv; laughter detection audio; shot boundary detection pytorch; video highlight detection audio loudness whisper; auto clip podcast face tracking whisperx.

Pages: page 1 of every query (pass 1: 12 results, pass 2: 10, pass 3: 10) plus page 2 and `sort=updated` for the pass-3 families.

## 2. Initial candidate list (35 repositories)

Facts (stars, license, last push) are from the API on 2026-11-17. "Decision" is the final call; reasons for the 11 audited in depth are in section 3.

| # | Repository | Purpose | License | Last push | Decision |
|---|---|---|---|---|---|
| 1 | Breakthrough/PySceneDetect | Shot/scene cut detection (Content/Adaptive/Threshold/Hash, optional TransNetV2) | BSD-3-Clause | 2026-11-18 | **Adopt** |
| 2 | SYSTRAN/faster-whisper | Whisper inference, word timestamps, built-in VAD | MIT | 2026-11-12 | **Adopt** (already in project) |
| 3 | snakers4/silero-vad | Voice activity detection | MIT | 2026-10-22 | **Adopt indirectly** (bundled ONNX VAD inside faster-whisper) |
| 4 | opencv/opencv | Image/video primitives, DNN face detection | Apache-2.0 | 2026-11-17 | **Adopt** |
| 5 | opencv/opencv_zoo (YuNet face detector ONNX) | Small face detector model | Apache-2.0 repo (model file license to be recorded at download) | 2026-10-20 | **Adopt** (model fetched once, checksum recorded) |
| 6 | m-bain/whisperX | Forced-alignment word timestamps + diarization | BSD-2-Clause | 2026-11-12 | **Reject for core** (torch 2.8 stack, duplicates faster-whisper timestamps) |
| 7 | pyannote/pyannote-audio | Neural speaker diarization | MIT | 2026-11-17 | **Reject for core; optional adapter only** |
| 8 | soCzech/TransNetV2 | Neural shot-boundary detector | MIT | 2023-12-04 | **Reject** (stale; PySceneDetect 0.7 already embeds a TransNetV2 detector) |
| 9 | google-ai-edge/mediapipe | Face/pose/landmark tasks | Apache-2.0 | active | **Reject** (YuNet via OpenCV does the one job needed with far fewer dependencies) |
| 10 | qiuqiangkong/audioset_tagging_cnn (PANNs) | AudioSet tagging (laughter, screams...) | MIT | 2024-07-25 | **Reject for core** (pinned to torch 1.0.1; heavy). Pattern adopted: in-house heuristic events, with a documented ONNX tagger hook |
| 11 | ClipsAI/clipsai | Clip finding + reframing library | MIT | 2024-01-17 | **Reject** (unmaintained 2+ years; pulls pyannote, facenet-pytorch, mediapipe) |
| 12 | Anil-matcha/AI-Youtube-Shorts-Generator | LLM clip selection + shorts | MIT | 2026-11-12 | **Reject** (cloud-first, see section 3) |
| 13 | ggml-org/whisper.cpp | C++ Whisper, Metal + Core ML on Apple Silicon | MIT | 2026-11-17 | **Defer**: strong alternative ASR backend; not needed until benchmarks show faster-whisper is the bottleneck |
| 14 | ClipsAI/clips-studio ("Clips Kitty") | Local clipper app | AGPL-3.0 | 2026-11-17 | **Reject** (AGPL copyleft; no code reuse) |
| 15 | ultralytics/ultralytics | YOLO detection | AGPL-3.0 | 2026-11-17 | **Reject** (AGPL) |
| 16 | deepinsight/insightface | Face analysis | NONE at repo level (models non-commercial per project notes, not re-verified here) | 2026-10-19 | **Reject** (no license / model terms) |
| 17 | xixihhhh/hotclip | Livestream clip tool | AGPL-3.0 | 2026-10-26 | **Reject** (AGPL) |
| 18 | linto-ai/whisper-timestamped | Word timestamps | AGPL-3.0 | 2026-09-28 | **Reject** (AGPL) |
| 19 | artkulak/twitch-stream-highlights-detection | Twitch highlights from chat/audio features | Unlicense | 2021-05-26 | **Idea only** (public domain; stale; uses downloaded VODs + chat logs) |
| 20 | kiy0ni/auto-video-editor | VOD to highlight reel | MIT | 2026-10-26 | **Not audited in depth**; reference only |
| 21 | fralapo/clippyme | Self-hosted Opus-style clipper | MIT | 2026-10-14 | **Not audited in depth** |
| 22 | kleZ799/clipmint | Opus-style clipper | MIT | 2026-11-16 | **Not audited in depth** |
| 23 | hector-ai-91/video-podcast-clipper | Face-tracking podcast clipper | MIT | 2026-10-14 | **Not audited in depth** |
| 24 | OpenGVLab/InternVideo | Video foundation models | Apache-2.0 | 2026-07-02 | **Reject** (research-scale; GPU) |
| 25 | QwenLM/Qwen2.5-Omni, DAMO-NLP-SG/VideoLLaMA3, OpenMOSS/MOSS-VL | Multimodal video LLMs | Apache-2.0 | 2025-2026 | **Reject for core** (multi-GB models vs 8 GB RAM). The orchestrating Claude agent does visual review instead |
| 26 | UVA-Computer-Vision-Lab/OmniShotCut | SOTA shot-boundary | MIT | 2026-11-01 | **Watch**: GPU research code; revisit if PySceneDetect misses cuts |
| 27 | wentaozhu/AutoShot | Shot-boundary + dataset | MIT | 2023-04-26 | **Reject** (stale) |
| 28 | FireRedTeam/FireRedVAD | VAD + audio events | Apache-2.0 | 2026-05-06 | **Watch** for future audio-event model |
| 29 | k2-fsa/sherpa-onnx | ONNX ASR/diarization/VAD runtime | Apache-2.0 | 2026-11-17 | **Watch**: possible torch-free diarization path |
| 30 | FluidInference/FluidAudio | CoreML audio models (Swift) | Apache-2.0 | 2026-11-17 | **Watch**: Apple-native diarization, but Swift integration cost |
| 31 | TaoRuijie/TalkNet-ASD | Active-speaker detection | MIT | 2023-10-23 | **Reject** (stale, torch) |
| 32 | remotion-dev/template-tiktok | TikTok-style captions | NONE | 2026-11-09 | **Reject** (no license) |
| 33 | Breakthrough/PySceneDetect forks / "cutdetective", "scene-cutter" | Wrappers | NOASSERTION/NONE | 2025-26 | **Reject** (no clear license) |
| 34 | LasseWolter/laughter-detection-icsi | Laughter detection training pipeline | MIT | 2023-01-29 | **Idea only** (needs ICSI corpus) |
| 35 | metaleey/AI-auto-segment-edit-video-pipeline, doaneruby970-hub/video-clipper, rai2009p/clipflop, and similar | AI clipper clones | NONE | 2026 | **Reject** (no license; avoid copying) |

Roughly 20 further results were listed in the raw files and rejected on sight for being unrelated (summarizers, dictation apps, bindings for other languages) or having no license.

## 3. Deep audit (11 candidates)

Format per candidate: facts from API and source files, then verdict. "Local/cloud" is what the code does by default.

### 3.1 Breakthrough/PySceneDetect
- URL: https://github.com/Breakthrough/PySceneDetect. Purpose: scene cut and fade detection. License: BSD-3-Clause (LICENSE file read). Latest release v0.7.3 (2026-11-08); pushed 2026-11-18; 62 open issues; created 2014. Language: Python on OpenCV.
- Source: `scenedetect/detectors/` has `ContentDetector`, `AdaptiveDetector`, `ThresholdDetector`, `HistogramDetector`, `HashDetector`, and `transnet_v2.py` (embedded neural option). Dependencies: click, numpy, opencv-python, platformdirs, tqdm. Python >= 3.10. Pure local. No outbound network or telemetry found in the package source (grep hit only a docs generator).
- Platforms: classifiers say OS independent; Apple Silicon works through opencv-python wheels (not benchmarked yet; benchmark in section 7 of the test report).
- Strengths: mature, permissive license, Python API, adaptive threshold handles flashy content, multiple detectors. Weakness: it detects visual cuts, not editorial events; in a compilation or a fast-cut stream layout it over-segments. Needs the event-merging logic we add.
- Decision: **Adopt** as the shot-boundary source of truth. Reason: best license/maintenance/cost balance; cuts are an input signal, never an event boundary on their own.

### 3.2 SYSTRAN/faster-whisper
- MIT; v1.2.1 (2025-10-31); pushed 2026-11-12; 21 open issues; 26.8k stars. Deps: ctranslate2 4.x, huggingface_hub, tokenizers, onnxruntime (for its bundled Silero VAD), av, tqdm. Local inference; model download from Hugging Face on first use. No telemetry found.
- Apple Silicon: CTranslate2 runs on CPU on macOS (int8 works); no Metal path, so it is slower than whisper.cpp on this machine. The project already runs it (cached `medium.en`, `small.en` models). NVIDIA: CUDA supported.
- Strengths: word timestamps (`word_timestamps=True`), VAD filter, already integrated and tested here. Weakness: CPU-bound on M2.
- Decision: **Adopt (already in use)**. Keep the project's existing two-decode transcript confirmation.

### 3.3 snakers4/silero-vad
- MIT; v6.2.3; pushed 2026-10-22; 17 open issues. pyproject depends on torch; ONNX runtime options exist. faster-whisper bundles the ONNX VAD model, so no separate install is needed.
- Decision: **Adopt indirectly** through faster-whisper `vad_filter`; do not add torch for it.

### 3.4 m-bain/whisperX
- BSD-2-Clause; v3.8.6; pushed 2026-11-12; 219 open issues. `pyproject.toml` pins `torch~=2.8.0`, torchvision, transformers, pandas, nltk, and pulls `pyannote-audio>=4.0.0` for diarization. Python 3.10-3.13.
- Strength: forced alignment gives tighter word times than Whisper's cross-attention. Weakness: roughly a multi-GB dependency stack on an 8 GB machine; diarization needs a Hugging Face token and gated models.
- Decision: **Reject for core.** Reason: faster-whisper word timestamps plus the project's second decode and edge snapping already meet the cut-in-word QA; adding torch costs more than it returns. Revisit only if QA shows word-boundary misses.

### 3.5 pyannote/pyannote-audio
- MIT; 4.1.0 (2026-11-17); pushed 2026-11-17. Requires Python >= 3.11, torch >= 2.8, torchaudio, lightning, and **`opentelemetry-exporter-otlp`**; `src/pyannote/audio/telemetry` tracks pipeline init/apply and file duration. Pretrained pipelines are gated behind a Hugging Face token (credential stop condition).
- Decision: **Reject for core; optional adapter.** Reasons: privacy (telemetry code and OTLP exporter ship by default; usable only if disabled), credential requirement, and torch footprint. Core uses an on-device spectral speaker-change proxy and labels it as such; a pyannote adapter is allowed only when the user installs it and supplies a token.

### 3.6 soCzech/TransNetV2
- MIT; last push 2023-12-04; weights in-repo (`inference/transnetv2-weights`); TensorFlow-based inference plus a separate PyTorch folder.
- Decision: **Reject as direct dependency.** PySceneDetect 0.7.x exposes a `TransnetV2Detector`, so a neural option remains reachable through the adopted library without a stale repo.

### 3.7 google-ai-edge/mediapipe
- Apache-2.0; v1.2.3 (2026-11-11). Bazel monorepo; Python wheels are large and the API has changed repeatedly (Solutions to Tasks).
- Decision: **Reject.** Only need: face boxes per sampled frame. OpenCV's `FaceDetectorYN` with the YuNet ONNX model does that with one dependency we already adopt.

### 3.8 qiuqiangkong/audioset_tagging_cnn (PANNs)
- MIT; last push 2024-07-25. `requirements.txt` pins torch==1.0.1.post2, librosa==0.6.3, matplotlib 3.0.3: unusable on current Python without patching. Needs a checkpoint download.
- Decision: **Reject for core.** The pipeline keeps and extends the in-house heuristic audio events (`audioevents.py`: loud/yell, impact, silence, laughter-like bursts) and exposes a hook for an ONNX tagger later. Documented as a limitation: laughter/scream classification is heuristic, not learned.

### 3.9 ClipsAI/clipsai
- MIT; version 0.2.1; **last push 2024-01-17**; 575 stars. `setup.py` requires facenet-pytorch, mediapipe, pyannote.audio, pynvml, scenedetect, etc. A code-injection-prone `eval("numpy." + window + "(window_len)")` in `clip/texttiler.py:481` (window name is internal there, but it is a pattern to avoid copying).
- Decision: **Reject.** Unmaintained, heavy, and its clip-selection is transcript-topic tiling (TextTiling), which does not find funny moments or audio payoffs.

### 3.10 Anil-matcha/AI-Youtube-Shorts-Generator
- MIT; pushed 2026-11-12; 6.2k stars. Default `--mode api`: uses the MuAPI paid service for download/transcription/cropping, plus OpenAI/Gemini keys for highlight scoring (`shorts_generator/config.py`). README is largely marketing for the vendor's API. Local mode exists (yt-dlp, faster-whisper, OpenCV face tracking).
- Decision: **Reject.** Violates the local-only/privacy/no-paid-service requirements by default, and its input is YouTube downloads, which this project must not use for unauthorized media. Useful ideas noted: overlap chunking for long videos and score-based dedupe; both re-implemented in-house.

### 3.11 ggml-org/whisper.cpp
- MIT; v1.9.5 (2026-10-06); pushed 2026-11-17. README states Metal and Core ML (Apple Neural Engine, "more than x3 faster" vs CPU) support. Needs a C++ build or Homebrew package plus ggml model files.
- Decision: **Defer.** Strong fallback if the benchmark in section 7 shows faster-whisper too slow for multi-hour streams on this M2. Installation complexity is higher and the project's tests/caches are built on faster-whisper.

## 4. Adopted components and records

| Component | Version | License | Role | Source of install |
|---|---|---|---|---|
| ffmpeg / ffprobe | 9.0.2 (existing Homebrew) | LGPL/GPL build; used as an external executable, not linked | decode, filters, render, QA probes | existing |
| faster-whisper | 1.2.1 (existing) | MIT | transcripts, word timing, VAD | existing |
| ctranslate2, onnxruntime-backed VAD | existing | MIT / MIT | ASR runtime | existing |
| PySceneDetect | 0.7.3 | BSD-3-Clause | shot boundaries | PyPI by exact version |
| opencv-python | latest at install (pinned in lock file) | Apache-2.0 | frames, motion, YuNet face detection | PyPI |
| YuNet `face_detection_yunet_2023mar.onnx` | from opencv_zoo | verify the `LICENSE` file in the model folder at download time and store it under `licenses/` | face tracking for reframing | single file download, SHA-256 recorded |
| numpy | 2.5.4 (existing) | BSD-3-Clause | signal math | existing |

Everything else (event fusion, content-type detection, ranking/standalone passes, QA additions) is original code in this repository.

## 5. Not adopted, with the license risk called out

AGPL (ultralytics, whisper-timestamped, hotclip, clips-studio) cannot be mixed into this project without copyleft obligations. Repos with `NONE`/missing license (insightface at repo level, remotion template, many small clippers) are not used and no code was copied. Gated or telemetry-bearing stacks (pyannote) are out of the default path.

## 6. Known gaps found by the audit

1. No permissively licensed, maintained, torch-free **laughter/scream audio tagger** surfaced; heuristics stay, flagged as a limitation.
2. No permissively licensed, lightweight **speaker diarization** without telemetry or gated tokens; spectral proxy plus optional pyannote adapter.
3. Search coverage is limited to GitHub (see section 0).
