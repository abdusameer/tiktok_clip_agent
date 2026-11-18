# Architecture decision: `/make-clips`

Evidence base: `research/github_clipping_agent_audit.md`. Machine: Apple M2, 8 GB RAM, no discrete GPU, 28 GB free disk.

## Architectures compared

| | A. Mostly existing open source | B. **Hybrid (chosen)** | C. Mostly custom on foundations |
|---|---|---|---|
| What it is | Adopt a finished clipper (ClipsAI, AI-Youtube-Shorts-Generator, clips-studio, clippyme...) and wrap it | Proven libraries for the hard signal problems (ffmpeg, faster-whisper, PySceneDetect, OpenCV+YuNet) + our own editorial logic, passes, QA and renderer, orchestrated by the Claude agent | ffmpeg + numpy only; write our own cut detector, face finder, ASR glue |
| Clip quality | Generic "viral transcript" scoring; none of the candidates finds audio payoffs, reactions or movie scenes; ClipsAI uses transcript topic tiling | Fuses audio events, transcript, cuts, faces and motion; the agent's visual review closes the gap heuristics leave | Same editorial logic as B but weaker shot/face signals |
| Local / privacy | Mixed: the most popular one defaults to a paid cloud API; pyannote-based ones ship telemetry and need a gated token | Fully local; no accounts, keys or telemetry | Fully local |
| Licensing | 2 of 4 serious clippers are AGPL (cannot be mixed in); others are stale or unlicensed | MIT/BSD/Apache only; ffmpeg used as an external executable | Cleanest |
| Hardware fit (8 GB, no CUDA) | torch stacks (whisperX, pyannote, facenet) strain 8 GB | Light: ONNX/OpenCV/CTranslate2, run in separate worker processes | Lightest |
| Reliability / maintenance | Depends on projects whose last commit is 2024 or on SaaS endpoints | Depends on four actively maintained libraries (all pushed within 2026) | Bugs in re-implemented vision code |
| Speed | Unknown, mostly API-bound | Scene detection and face sampling run once at 2 samples/s; transcripts cached | Slowest to build |
| Creative flexibility | Fixed templates | Reuses the approved visual style tokens and adds new layouts (split, tracked crop, blur-fit, punch-in) | Same |
| Cost | Paid APIs likely | $0 ongoing | $0 ongoing |
| Time to a working, tested system | Fast, but cannot meet the editorial brief | Moderate | Slow |

## Decision: B (hybrid)

Adopted libraries: **ffmpeg** (decode/render/QA probes), **faster-whisper** (transcripts, word timing, VAD), **PySceneDetect** (shot boundaries), **OpenCV + YuNet** (frame sampling, motion energy, face tracking). Everything that makes a clip good is our code:

1. **Content-type detection** (`auto`) from measured features.
2. **Signal fusion and event recovery**: shots are inputs, never boundaries. Events grow backwards and forwards from interest peaks until the conversation, joke, reaction or scene actually ends (boundary strength = silence + lexical topic shift + speaker change + continuity-breaking cut), never by a runtime.
3. **Two independent passes** (ranking, standalone) with the reuse and near-duplicate rules from `CONTENT_EDITING_RULES.md` (runtime is metadata, minimum 3 per ranking, no padding).
4. **Renderer** with content-aware layouts and the approved caption/headline/badge styling; captions only from words both Whisper decodes agree on.
5. **QA gate** with explicit pass/needs-review routing and exact reasons.
6. **Agent review layer**: the pipeline writes a review pack (frames, transcript, audio events, neighbours) for every candidate. The Claude agent performs the visual and audio-aware review the editorial rules demand (`/make-clips` skill). A deterministic `auto` review exists so the pipeline is testable and never blocks; it marks its manifests `visual_review: auto-metrics-only`.

### Why not the others
- **A** fails on privacy, licensing and, most importantly, on the brief: none of the audited clippers detects funny moments from audio payoffs, reactions or scene structure, or treats runtime as metadata.
- **C** re-implements things (PySceneDetect's detectors, YuNet) that are mature, permissively licensed and small. That adds risk without improving a single finished video.

### What was deliberately left out (and the trigger to revisit)
- **whisperX / pyannote / mediapipe / TransNetV2 / PANNs**: reasons in the audit. Revisit whisperX or pyannote only if QA shows word-boundary misses or speaker-change errors in real outputs, and only behind an opt-in extra.
- **whisper.cpp (Metal)**: revisit when the benchmark shows transcription dominates run time for multi-hour streams.

## Process model (memory discipline on 8 GB)
- Stage workers run as **separate processes**: audio analysis, visual sampling (OpenCV), transcription (faster-whisper/PyAV). This also avoids the duplicate `libavdevice` clash between OpenCV's and PyAV's bundled ffmpeg libraries that macOS warns about when both are loaded in one process.
- Every stage writes its JSON to `runs/<source-id>/analysis/` and is skipped when that cache matches the source hash, so a failed run resumes.

## Data flow
```
sourcevideo/<file> --register--> runs/<id>/source.json (sha256, probe, rights)
  -> analysis/{audio.json, visual.json, transcript.json, speakers.json, content_type.json}
  -> candidates/{ledger.json, review_pack/Cxxx/...}
  -> (agent review or auto review) candidates/reviews.json
  -> projects/{rankings,standalone}/*.json   (passes + duplicate/reuse policy)
  -> renders/*.mp4  -> QA -> approved/{rankings,standalone}/  |  needs-review/<run>/<video>/FAILURE.txt
  -> manifests/*.json, logs/<run>.log, runs/<id>/REPORT.md
```

## Rollback plan
All work lives on branch `feature/make-clips` and in new files (`src/clipagent/clips/`, one new CLI command, one new skill, new tests and docs). The existing pipeline modules are not edited, except one additive `make-clips` command registration in `cli.py`. Rollback is `git checkout main` (nothing on `main` changes until merged); to remove only the new dependencies: `pip uninstall scenedetect opencv-python` and delete `models/yunet/`. Existing `runs/`, `source_library/`, `approved_clips/` and `registry/` are never written by `/make-clips` (it uses its own `runs/<source-id>/`, `approved/`, `needs-review/`).
