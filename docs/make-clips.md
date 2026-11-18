# /make-clips

Local, autonomous clipping of **authorized** long-form media into TikTok-ready ranking and standalone videos. Research: `research/github_clipping_agent_audit.md`. Architecture decision: `research/architecture_decision.md`. Editorial policy: `CONTENT_EDITING_RULES.md` (runtime is metadata, never a score). Style: `config/ranking_style.json`.

## Install (reproducible)
Requirements: macOS on Apple Silicon (tested M2 / 8 GB), Homebrew `ffmpeg`, Python 3.12, `/usr/bin/python3` with Pillow (text overlays).
```
python3.12 -m venv .venv
.venv/bin/pip install -e .                       # typer, faster-whisper
.venv/bin/pip install -r requirements.lock       # exact versions used in testing (scenedetect 0.7.3, opencv-python, ...)
mkdir -p models/yunet && curl -L -o models/yunet/face_detection_yunet_2023mar.onnx \
  https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
shasum -a 256 models/yunet/*.onnx                # expect 8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4
```
Whisper models (`small.en` for the full-source pass, `medium.en` for caption decodes) download to the local Hugging Face cache on first use. Nothing else is installed or run from third-party repositories.

## Use
```
PYTHONPATH=src .venv/bin/python -m clipagent.cli make-clips --source <file|folder> --content-type auto --platform tiktok [--rights "owned: ..."]
PYTHONPATH=src .venv/bin/python -m clipagent.cli make-clips                       # processes everything in sourcevideo/
... make-clips --source X --until review-pack      # stop after candidate recovery so the agent can review (see the skill)
```
Content types: `streamer gaming movie show podcast interview auto` (auto = measured evidence, confidence reported; low confidence is flagged). `movie`/`show` require `--rights` or `<file>.rights.txt`; without it the run stops (exit 2) before any output.

## What it does
1. **Whole-source analysis** (separate worker processes, cached): PySceneDetect shot cuts (inputs, never event boundaries), motion energy, YuNet faces, letterbox bars, audio events relative to the source's own noise floor and active level (loud, impact, silence, comic pause, 3-7 Hz laughter rhythm in non-speech), full transcript with word timing (pass 1), speaker-change proxy, content type.
2. **Event recovery**: fused interest timeline → peaks (robust threshold, no quota) → each grown backwards (setup) and forwards (escalation, payoff, reaction) until the conversation/joke/scene actually ends (silence, topic shift, speaker change after a pause, real scene change); movies/shows extend to scene boundaries. Cuts never land inside a spoken word.
3. **Review**: a review pack per candidate for the agent (frames, sheet, timed words, audio events); a metrics-only auto review as the fallback.
4. **Ranking pass and standalone pass** with the reuse/duplicate policy; **render** (blur-fit, fill, face-track, punch-in at a real impact; approved caption/headline/badge styling; original audio; captions only from words two decodes agree on); **QA** (resolution, 9:16, codecs, A/V sync, black/frozen frames, clipping, silence, cut-in-word, caption accuracy, safe areas, on-screen text safety, subject tracking, ranking numbers, duplicates); routing to `approved/{rankings,standalone}/<source-id>/<id-slug>/` or `needs-review/<source-id>/<id-slug>/FAILURE.txt`.

Outputs per video: `.mp4`, `manifest.json` (source id, timecodes, content type, candidate ids, format, hook, setup, payoff, runtime, transcript confidence, visual/audio evidence, reframing, editing decisions, reuse, rights, QA), `qa.json`, `cover.jpg`, `caption.txt`, `contact-sheet.png`. Run folders: `runs/<source-id>/{analysis,transcripts,candidates,projects/{rankings,standalone},renders,manifests,qa}`, logs in `logs/`.

## Tests
`PYTHONPATH=src .venv/bin/python -m pytest tests -q` (unit + service simulations), `tests/test_clips_e2e.py` (full pipeline on the legal fixture, ~3 min). Fixture generator: `tests/fixtures/make_fixture.py` (CC0, synthetic). Results and benchmarks: `docs/test-results.md`.

## Rollback
Everything lives on branch `feature/make-clips`; `main` is untouched until merged. The original pipeline modules are unchanged (the only edit to existing code is the additive `make-clips` and `service` command registration in `cli.py`). To remove: `git checkout main`; optionally `pip uninstall scenedetect opencv-python` and delete `models/yunet/`. `/make-clips` never writes to `source_library/`, `approved_clips/` or `registry/`.

## Known limitations
* English only (`.en` Whisper models); non-English sources need `--model` changes and caption-decode changes.
* Audio events are heuristics (no trained tagger): music, applause and game audio can look like yelling or laughter. Speaker changes are a spectral **proxy**, not neural diarization.
* Auto reviews judge from metrics, not pictures. Visual judgement (and visual safety) comes from the agent review; the zero-touch gate refuses metrics-only clips unless you configure a reviewer.
* Face-track needs one 9:16 window to hold the faces; otherwise blur-fit is used. No facecam split-screen layout yet.
* Separate audio files are muxed assuming they start with the video (a duration mismatch is refused; no cross-correlation offset search).
* Whisper `small.en` first pass on a multi-hour stream is CPU-bound on this M2; see benchmarks. whisper.cpp (Metal) is the documented upgrade path.
* Selective word highlighting, sound effects and music beds are not implemented (the brief lists them as optional).
