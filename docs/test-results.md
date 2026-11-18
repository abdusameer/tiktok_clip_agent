# Test results and benchmarks

Machine: Apple M2, 8 GB RAM, macOS 27 (arm64), Python 3.12.15, ffmpeg 9.0.2. Date: 2026-11-17/18. Branch `feature/make-clips`.

## Final full run (`pytest tests -q`)
**368 passed, 1 skipped, 1 failed** in 14 min 14 s.

| New tests | Count | Result |
|---|---|---|
| `test_clips_units.py` (content type, events, audio events, reframing, layouts, rights, review, ranking/standalone passes, source discovery) | 24 | all pass |
| `test_clips_e2e.py` (real pipeline on the legal fixture, ~3 min) | 4 | all pass: streamer detected, planted events found, 4 final videos approved (1 ranking + 3 standalone), 1080x1920 H.264, full manifests, idempotent re-run |
| `test_service_lifecycle.py` (simulations 1-9) | 21 | all pass |
| `test_service_storage.py` (simulations 10-13 + full retention lifecycle to PURGED) | 7 | all pass |
| `test_service_learning.py` (simulations 14-15 + scheduler) | 9 | all pass |
| `test_service_policy.py` (mode honesty, gate, post package, kill switch, dry run, health, launchd, no browser automation/secrets) | 12 | all pass |

Required simulations: 1 ingestion, 2 candidate selection, 3 rendering, 4 QA rejection, 5 upload simulation, 6 failed-upload recovery, 7 duplicate-post prevention, 8 crash recovery (4 variants), 9 expired token (refresh + rotation, dead refresh token, refresh expiry), 10 storage high-water cleanup, 11 quarantine recovery, 12 safe purge, 13 untracked-path protection (incl. symlink tricks), 14 metrics collection, 15 strategy update without overfitting (outlier, consistent evidence, noise).

Bugs the tests found and I fixed: the service quarantined the user's own source as a "duplicate" of itself; cut points landing inside a word; silent-audio baseline collapse when a source has little silence; non-unique auto headlines; a frozen test card in the fixture (QA correctly rejected it); launchd plist resolving the venv interpreter to the system Python.

## The one failure is a pre-existing intermittent failure in the original pipeline
Original tests in `tests/test_content.py` fail intermittently (`test_one_failed_render_does_not_stop_the_other_outputs`, `test_dashboard_links_every_finished_video`, and in the last run `test_used_moments_are_never_planned_again...`). Cause captured from failing runs: `FAIL av_sync_stream_durations audio/video duration gap 0.135s` on the ranking render (tolerance 0.12 s). **It reproduces on a clean extract of `main`** (1 failure in about 10 runs there, identical message; roughly 5 in 20 on this branch, so a higher rate here cannot be ruled out, but the failing check is the same) and also on this branch, in code this branch does not modify (the only existing source file this branch changes is `cli.py`, additively); the test feeds the renderer unseeded `anoisesrc` noise. The third test's failure is consistent with the same cause (a ranking sent to needs-review is not registered as used) but I did not repeat it to prove that. I did not change the original renderer or its tolerance.

## Benchmarks (10-minute 720p h264 slice, `scripts/benchmark.py`)
| Stage | Time | Real-time factor |
|---|---|---|
| Visual: PySceneDetect + 4 Hz motion/faces/letterbox | 40.5 s | 0.068 |
| Transcript: `small.en`, word timestamps, VAD (CPU int8) | 95.0 s | 0.158 |
| Audio events | 0.8 s | 0.0014 |

Serial projection: 1 h source ≈ 14 min, 2 h ≈ 27 min, 4 h ≈ 55 min (visual and transcript run in parallel in production). Per-video render + two-decode captions + QA on the fixture: roughly 30-45 s each. Peak memory was not measured. A cold first start can stall for minutes on first file reads in the venv (observed with `faster_whisper` and `typer` imports; not code).
Fixture end-to-end (96 s synthetic stream, warm caches): analysis 14 s.
