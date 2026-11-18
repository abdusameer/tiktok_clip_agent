# Handoff: everything learned so far (2026-11-17 to 2026-11-18)

Written so a fresh session on another machine can continue without this laptop. Branch: `feature/make-clips` (base `main`). **Nothing is enabled, uploaded or deleted.** The service ships in dry-run.

## 1. Where things stand
| Area | State |
|---|---|
| GitHub research + audit | Done: `research/github_clipping_agent_audit.md` (35 repos listed, 11 audited from source; raw search TSVs kept beside it). |
| Architecture | Chosen: hybrid. `research/architecture_decision.md`. |
| `/make-clips` agent | Built and passing on the legal fixture: `src/clipagent/clips/`, skill `.claude/skills/make-clips/SKILL.md`, CLI `make-clips`. |
| Zero-touch service | Built and passing 15 required simulations: `src/clipagent/service/`, CLI `clipagent service ...`. **Not enabled.** |
| Tests | 368 passed, 1 skipped, 1 failed (pre-existing flake, section 6). `docs/test-results.md`. |
| Real sources processed | **0**: `sourcevideo/` was empty. The only real media on the laptop were two compilations under `source_library/` (not touched by `/make-clips`). |
| Not committed before this push | Everything above was uncommitted working-tree work until this push. |

## 2. Resume on a new machine (read the portability warnings first)
```
git clone https://github.com/abdusameer/tiktok_clip_agent.git && cd tiktok_clip_agent
git checkout feature/make-clips
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.lock && .venv/bin/pip install --no-deps -e .
# YuNet face model (not in git): scripts/install.sh downloads it and verifies SHA-256 8f2383e4...2fa4
PYTHONPATH=src .venv/bin/python -m pytest tests -q --ignore=tests/test_clips_e2e.py
```
**The project was developed on macOS (Apple M2). The owner's PC is Windows + RTX 2070 Super; see section 2b for the exact port plan. These parts are macOS-specific:**
* Text overlays use `/usr/bin/python3` + Pillow and Arial fonts at `/System/Library/Fonts/...` (`config/ranking_style.json` -> `fonts`), because the local ffmpeg has no text filters. On a PC: point `fonts` at real font files and `renderer_python` at an interpreter with Pillow.
* On-screen-text OCR is a Swift/Vision helper (`src/clipagent/_ocr.swift`, compiled to `source_library/_tools/ocr2`). Without it OCR reports "unavailable" and clips carry `onscreen_text_unverified` (allowed by the QA gate, but nothing is verified). Needs a replacement (e.g. a local OCR engine).
* Test fixture generation uses macOS `say` (`tests/fixtures/make_fixture.py`); other OSes need another TTS (the fixture, the e2e test and 3 unit tests skip/need it).
* Keychain token storage (`service/tokens.py`, `security` CLI) and `launchd` (`service/launchd.py`) are macOS-only. Equivalents: Windows Credential Manager / `keyring`, and Task Scheduler / a Windows service / systemd.
* `doctor`, some ffmpeg paths (`/opt/homebrew/bin`) assume Homebrew. `h264_videotoolbox` is not used; encoding is `libx264` (works anywhere).
* faster-whisper runs CPU int8 here. On an NVIDIA PC use CUDA (`device="cuda"`, `float16`) in `models.py` for a large speed-up; `whisper.cpp`/MLX were deferred as upgrades only for Apple Silicon.
* Storage defaults assume POSIX `shutil.disk_usage`; works on Windows too, but the "no symlink" guard uses `os.path.realpath` (Windows junctions are not covered; review before trusting purge on Windows).

## 2b. Windows + RTX 2070 Super port plan (owner's PC; written 2026-11-18, nothing below is done yet)
Verified facts are marked (verified); everything else is my inference and must be tested on the PC.

**GPU transcription (the big win).**
* faster-whisper's GPU path needs **CUDA 12, cuBLAS for CUDA 12 and cuDNN 9** (verified from the faster-whisper README; `ctranslate2` 4.x supports only CUDA 12 / cuDNN 9). On Windows the README points to a libraries archive from `Purfview/whisper-standalone-win` (release tag `libs`) to unpack into a folder on `PATH`. Update the NVIDIA driver first so it supports CUDA 12.
* Then `WhisperModel(name, device="cuda", compute_type="float16")` (verified API). Today `src/clipagent/models.py` hard-codes `device="cpu", compute_type="int8"`: make device/compute type configurable (e.g. env `CLIPAGENT_DEVICE`, `CLIPAGENT_COMPUTE`) with a CPU fallback.
* 8 GB VRAM should hold `medium.en` and even `large-v3` in float16 (inference, my estimate: unmeasured). Run the transcript worker and the caption-decode step one at a time on the GPU. Expected speed-up vs the Mac's 0.158x real-time CPU factor is large but **unmeasured**; run `scripts/benchmark.py` on the PC and record it in `docs/benchmarks.json`.
* Optional: `h264_nvenc` for rendering. Turing has NVENC; keep `libx264` (config/ranking_style.json `video_encode`) as default for the approved look; render time is not the bottleneck.

**Code that will break on Windows (found by grep, verified present):**
| Where | Problem | Fix |
|---|---|---|
| `src/clipagent/service/daemon.py:7,592` | `import fcntl` is POSIX-only: importing the daemon fails on Windows | use `msvcrt.locking` or the `portalocker` package for the single-instance lock |
| `daemon.py:610` | `signal.SIGTERM` handling differs on Windows | handle `SIGINT`/`SIGBREAK`; run under a service wrapper |
| `src/clipagent/service/tokens.py` | macOS `security` Keychain CLI | use the `keyring` package (Windows Credential Manager) behind the same `get/set/delete` interface |
| `src/clipagent/service/launchd.py`, `service/cli.py:111-119` | launchd/plist | Windows Task Scheduler (`schtasks`, "At log on", restart on failure) or a service wrapper such as NSSM; keep the plist generator for the Mac |
| `config/ranking_style.json:13-15`, `src/clipagent/_pngtext.py`, `doctor.py:41-46` | `/System/Library/Fonts/...` fonts and `/usr/bin/python3` as the Pillow renderer | point `fonts` at `C:\Windows\Fonts\arialbd.ttf` and `ariblk.ttf`; set `renderer_python` to the venv's `python.exe` and `pip install pillow` (the venv has no Pillow today); make `doctor` check `sys.executable` |
| `src/clipagent/textsafety.py:140-147`, `_ocr.swift` | on-screen-text OCR is a Swift/Vision helper | replace with a Windows OCR (e.g. `winocr`/Windows.Media.Ocr, or Tesseract/RapidOCR). Until then OCR reports unavailable and every clip carries `onscreen_text_unverified` (allowed by QA, but on-screen text is NOT verified: do not rely on it for chat-slur safety) |
| `tests/fixtures/make_fixture.py:44` | macOS `say` TTS | use `pyttsx3` or PowerShell `System.Speech.Synthesis` to synthesize the fixture speech; `tests/test_clips_e2e.py:15` skip condition |
| `render.py` (original and `clips/render.py`) concat list files | the ffmpeg concat demuxer treats `\` as an escape: Windows paths in `file '...'` lines break | write `Path.as_posix()` paths (forward slashes) |
| `doctor.py:31` | Homebrew PATH | `winget install Gyan.FFmpeg` and ensure `ffmpeg`/`ffprobe` are on PATH |
| `service/storage.py` | symlink guard uses `os.path.realpath`; Windows junctions/reparse points are not detected | add a reparse-point check before trusting purge; keep `dry_run: true` on the PC until the storage tests pass there |
| file locking | Windows refuses to rename/delete files another process has open (ffmpeg, a player) | retry-with-backoff around `os.replace`/`os.unlink` in storage, and make QA close its handles |

**Probably fine:** PySceneDetect, OpenCV, YuNet (ONNX), NumPy, SQLite WAL, typer, onnxruntime (VAD). `requirements.lock` is a macOS freeze: install `faster-whisper`, `ctranslate2`, `scenedetect`, `opencv-python`, `numpy`, `typer`, `onnxruntime` fresh on Windows and re-freeze to `requirements-windows.lock`.

**Suggested order on the PC:** (1) clone, venv, install; (2) fix the `fcntl` import and fonts/Pillow so the CLI and renderer run; (3) run the unit tests; (4) enable CUDA and run `scripts/benchmark.py`; (5) port the fixture TTS and run the e2e test; (6) port tokens/scheduler/OCR; (7) only then the service. The full Windows port is a real chunk of work; ask me to do it in that order.

## 3. Decisions made and why
* **Hybrid architecture**: ffmpeg + faster-whisper + PySceneDetect + OpenCV/YuNet as foundations; editorial logic, passes, QA and renderer are original. Rejected: AGPL clippers (copyleft), unlicensed repos, whisperX/pyannote (torch stack, telemetry, gated tokens), MediaPipe (heavier than YuNet for one job), TransNetV2 (stale; PySceneDetect 0.7 embeds a TransNetV2 detector), PANNs (pinned to torch 1.0.1), ClipsAI (stale since 2024), AI-Youtube-Shorts-Generator (paid cloud API default).
* **Process isolation**: OpenCV and PyAV (from faster-whisper) each bundle their own `libavdevice`; loading both in one process triggers macOS ObjC duplicate-class warnings. So visual analysis, transcription and face QA run as separate worker processes. This also bounds memory on 8 GB.
* **Runtime is metadata, never a score** (`CONTENT_EDITING_RULES.md`). Events grow until the conversation/joke/scene ends; shots are inputs, never boundaries; cut edges snap to word edges.
* **Agent does the visual judging**: heuristics can't see pictures. The pipeline writes a review pack per candidate; the Claude agent reviews and writes `candidates/reviews.json`; an auto review exists only as a metrics fallback and is marked `auto-metrics-only`.
* **Default delivery = draft/inbox queue** (section 4). Direct Post only with recorded audit + consent + UX attestation.
* **Deletion safety**: one guard function (`service/storage.py::_guard_delete`); exact ledger path, inside quarantine/temp locations, no symlinks, protected retention classes never deleted, files only (never recursive), dry-run deletes nothing.
* **Duplicate-post protection**: idempotency key + intent recorded before contacting TikTok; ambiguous crash outcomes become `UPLOAD_UNCERTAIN` and are never re-sent.

## 4. TikTok official API facts (verified from developers.tiktok.com on 2026-11-17)
* Inbox/draft upload `POST /v2/post/publish/inbox/video/init/` (scope `video.upload`): video goes to the creator's inbox; **the creator must open the notification and post**. Max 5 pending shares per 24 h (`spam_risk_too_many_pending_share`). Status `SEND_TO_USER_INBOX` -> `PUBLISH_COMPLETE` once the creator posts.
* Direct Post `POST /v2/post/publish/video/init/` (scope `video.publish`): **unaudited clients are restricted to private (`SELF_ONLY`)**; error `unaudited_client_can_only_post_to_private_accounts`. Must call `creator_info/query` first; `privacy_level` must match `privacy_level_options`.
* Content-sharing guidelines: creator chooses title and privacy (**no default**), interactions (comment/duet/stitch) start unchecked, commercial-content toggle off by default, Music Usage Confirmation declaration before publish, no watermarks/promo text; "must only start sending content after the user has expressly consented". A headless service cannot honestly perform that UX; that is why Direct Post needs recorded attestations.
* Limits: 6 init requests/min and 30 status requests/min per user token; a daily post cap exists with no published number (`spam_risk_too_many_posts`).
* Status fields: `PROCESSING_UPLOAD`, `PROCESSING_DOWNLOAD`, `SEND_TO_USER_INBOX`, `PUBLISH_COMPLETE`, `FAILED` with `fail_reason` (`auth_removed`, `spam_risk*` = do not retry; `internal` = retry); `publicaly_available_post_id` (sic) only when public and moderated.
* Metrics: `POST /v2/video/query/` (scope `video.list`), <= 20 ids per call; fields `view_count`, `like_count`, `comment_count`, `share_count` (plus id/title/create_time/duration...). **Not available**: watch time, completion rate, rewatches, saves, follower conversion, profile visits.
* OAuth: token endpoint `https://open.tiktokapis.com/v2/oauth/token/` for code exchange and refresh; PKCE S256 required for desktop apps; access token ~2 h, refresh token ~180 days and **rotates on every refresh** (store the new one immediately). The *authorize URL* in `service/oauth.py` is from Login Kit memory and was **not** verified from a fetched page: confirm before first use. The Login Kit page URL I tried returned 404.
* Untested against the real API: everything TikTok-facing was tested only against a local fake (`FakeTikTok` in `tests/test_service_lifecycle.py`). Chunk-size rules were not documented on the pages read (single chunk <= 64 MB, else 32 MB chunks is my conservative guess).
* The Higgsfield MCP connector exposes `tiktok_*` tools (accounts, prepare_publish, publish_status, music). Whether Higgsfield's client is audited for public posting is unknown; the service does not use it. Higgsfield CLI 1.1.26, 275.49 credits on Plus at the time of the first inspection (nothing spent).

## 5. Technical learnings (pitfalls found by testing)
* Percentile-based thresholds quietly impose an event quota; use robust statistics (median + k*MAD) with a floor relative to the maximum.
* Never baseline loudness on a median/std of a bimodal speech+silence mix: silence inflates the spread and hides shouts. Use the active level (frames above a *low-percentile* noise floor); a 10th-percentile floor collapses when silence is < 10% of the audio (talkative streamers). Floor is now the 3rd percentile, active = floor + 10 dB.
* Whisper word timestamps can stretch over silence (a word reported 0.6 s before it is heard); refine edges against the loudness series and share one refined timing between planning and QA.
* `impact` from raw dB jumps fires on every speech onset out of silence; only count jumps that also exceed the active level.
* Laughter: 3-7 Hz envelope modulation works only where nobody is speaking (speech has the same syllable rate), so it needs the transcript.
* Comic pause (silence directly before a loud burst) belongs inside the event; scene-change boundaries should not block it if the cut lands with the payoff.
* Auto titles must quote what is said at the *peak*, not the aftermath; make auto headlines unique (try other subject words, then a numeric suffix).
* A video whose test pattern is static (`rgbtestsrc`) correctly fails the frozen-frame QA: use animated lavfi sources in fixtures.
* Service bug caught by tests: after ingest the source stays in the inbox, and the duplicate check matched the file against itself. Skip files already in the ledger and never treat a path as its own duplicate.
* `.resolve()` on `.venv/bin/python` follows the symlink to the system Python (breaks launchd); use the unresolved absolute path.
* `.gitignore` patterns must be root-anchored (`/service/`), or `src/clipagent/service/` gets ignored too.

## 6. Environment quirks (this laptop; may not apply elsewhere)
* No `timeout` command on macOS; BSD `sed -i ''`. The harness blocks `rm -rf "$VAR/..."`; avoid `rm -rf` with variables.
* First read of files in the venv (and the first `pip install`) stalled for minutes at ~0% CPU (`import faster_whisper` once took 6 min; `typer` imports once ~7 min). It was disk-level first-touch latency, not code; later runs were fast. Pre-warm by importing once.
* `pip list` hung; use `ls site-packages` or `pip freeze` in the background.
* The original ffmpeg has no `subtitles/drawtext/ass` filters; all text is PNG overlays.
* This laptop had **12.4% free disk (30 GB of 245 GB)**, below the 15% reserve: the service correctly pauses ingestion. On the PC, check capacity first (`clipagent service init`).
* Whisper models cached: `small.en` (full-source pass), `medium.en` (two-decode caption pass).

## 7. Known flake in the ORIGINAL pipeline (not caused by this branch)
`tests/test_content.py` intermittently fails with `FAIL av_sync_stream_durations audio/video duration gap 0.135s` (tolerance 0.12 s) on the ranking render. Reproduced on a clean `git archive main` extract (1 in ~10 runs) and on this branch (~5 in ~20). The three tests seen failing: `test_one_failed_render_does_not_stop_the_other_outputs`, `test_dashboard_links_every_finished_video`, `test_used_moments_are_never_planned_again_until_the_user_rejects_the_run` (the last is only *consistent with* the same cause; not proven). The test feeds unseeded `anoisesrc` noise. Candidate fixes (not done): seed the noise in the test; and/or investigate why concatenated AAC audio runs ~0.135 s longer than video in some runs (frame-exact video vs AAC priming/padding), then widen or fix the tolerance deliberately.

## 8. Open questions for the owner (answer when back)
1. ~~PC OS and GPU?~~ **ANSWERED 2026-11-18: Windows PC, NVIDIA RTX 2070 Super (8 GB VRAM).** Port plan in section 2b. **Also answered 2026-11-18: Windows 11, 1.5 TB disk, 16 GB RAM, always on.** With 1.5 TB the default storage rules (<= 20% = ~300 GB budget, >= 15% = ~225 GB always free) are generous and the disk-reserve blocker seen on the Mac (12.4% free) disappears; confirm actual FREE space on the PC with `clipagent service init`. 16 GB RAM removes the memory pressure of the 8 GB Mac. Still open: CPU model and how much of the 1.5 TB is currently free.
2. **TikTok developer app**: does one exist? Which scopes are approved? Is it **audited** for Direct Post? If not, stay on the draft queue (creator posts manually). Do you want me to prepare the audit application notes?
3. **Visual-safety policy**: the zero-touch gate blocks any clip that only has a metrics-only review. Options: (a) agent review per batch (not zero-touch), (b) plug in a classifier command (`gate.visual_safety.command`), (c) explicitly accept `metrics_only_allowed`. Which?
4. **Disk**: free space on the PC; keep the 20%/15% rules or adjust?
5. **Sources and rights**: which streamers/channels are authorized? Rights text for each (owned / licensed / permission). Is the compilation under `source_library/` yours to edit? Movies/shows need explicit rights or the run stops.
6. **Post metadata defaults**: streamer name, game/topic, event, any hashtags you want; privacy default (currently `SELF_ONLY`); comment/duet/stitch settings.
7. **Posting plan**: daily ceiling (default 3), spacing (180 min), slots (lead 14:00 PT, candidates 11/14/16/19/21). Is 5 per day via inbox mode acceptable given TikTok's cap?
8. **Retention**: confirm the 7-day quarantine then permanent purge of final renders AND the processed source after a confirmed post (the source purge deletes your original file); sources that yield no confirmed post are kept.
9. **Repo and branches**: this push went to `tiktok_clip_agent` (underscores) while your local `origin` is `tiktok-clip-agent` (hyphens). Which is canonical? Merge `feature/make-clips` to `main`?
10. **CLAUDE.md**: its top section is a leftover "inspect and report only" task text; and a standing rule says public publishing needs your explicit approval. The service treats `dry_run:false` + Direct Post attestations as that approval. OK, or remove the line / change the rule?
11. **The flaky test**: should I fix it (section 7)?
12. **Languages**: only English (`.en` models) is supported. Needed otherwise?
13. **Not built (optional in the brief)**: facecam split-screen layout, selective word highlighting, sound effects/music, progress bars, neural diarization, trained audio-event tagger, cross-correlation sync for separate audio, CLIP semantic search. Which matter?

## 9. Suggested next steps
1. Answer section 8. 2. Set up the PC (section 2) and free disk. 3. Run `clipagent service init`, edit `config/service.json`. 4. Drop one authorized source in `sourcevideo/inbox/` and run `make-clips` by hand; review the pack with the agent. 5. Create the TikTok app, `service set-client`, `service auth`; test the draft queue with one clip. 6. Only then set `dry_run:false` and load the service manager.

## 10. File map
`research/` audit, architecture, raw searches | `src/clipagent/clips/` pipeline (source, analysis, visual/transcript workers, audio, events, review, passes, reframe, render, qa, pipeline) | `src/clipagent/service/` (config, db, storage, gate, postpackage, scheduler, learning, publishers, tokens, oauth, daemon, launchd, cli) | `tests/` (original + `test_clips_*`, `test_service_*`, `fixtures/make_fixture.py`) | `docs/` (`make-clips.md`, `zero-touch-service.md`, `test-results.md`, `benchmarks.json`, `launchd/`) | `licenses/` | `scripts/install.sh`, `scripts/benchmark.py` | `requirements.lock`.
Existing files edited: `src/clipagent/cli.py` (additive), `CLAUDE.md`, `.gitignore`, `pyproject.toml`.
