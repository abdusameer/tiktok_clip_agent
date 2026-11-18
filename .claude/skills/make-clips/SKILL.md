---
name: make-clips
description: Autonomous local clipping of authorized long-form media (streams, gaming, movies, shows, podcasts, interviews) into ranking and standalone TikTok videos with audio-aware candidate recovery, content-aware 9:16 reframing, two-decode captions and a strict QA gate. Manual use only; never uploads or publishes.
disable-model-invocation: true
argument-hint: "--source <path> --content-type auto|streamer|gaming|movie|show|podcast|interview --platform tiktok [--rights '<status: details>']"
allowed-tools: Bash, Read, Write, Edit
---

# /make-clips

Arguments: `$ARGUMENTS`. Editorial policy is **only** in `CONTENT_EDITING_RULES.md` (runtime is metadata, never a score; ranking minimum 3, never padded; both passes mandatory). Visual style is `config/ranking_style.json`. Nothing here uploads, publishes, buys, or deletes sources.

Setup per shell call: `export PATH=/opt/homebrew/bin:$PATH`; CLI: `PYTHONPATH=src .venv/bin/python -m clipagent.cli make-clips ...`.

## Rights gate (the only reason to stop)
`movie` and `show` sources need an explicit rights status (`--rights 'owned|licensed|permission|public-domain: details'` or `<file>.rights.txt`). Exit code 2 = stopped for missing rights: report it and move to the next source. Never assume fair use. Streamer/gaming/podcast/interview files the user placed in `sourcevideo/` are recorded as user-supplied.

## Workflow (no routine questions)
1. `make-clips --source <path> --content-type auto --until review-pack` : analyses the WHOLE source (PySceneDetect cuts, motion, YuNet faces, audio events vs noise floor, two-pass transcript, speaker-change proxy, content type), recovers candidate events, and writes `runs/<id>/candidates/review_pack/<Cxxx>/` (7 frames across the event, contact sheet, timed words, audio events, neighbours).
2. **Review as the editor** (this is where visual and audio judgement happens; the auto-review is only a metrics fallback). For every candidate: view `sheet.jpg` and the frames, read `evidence.json` (words against audio events). A contact sheet never proves an audio payoff: use the timed words and `audio_events`, and inspect neighbouring candidates before rejecting anything. Merge/trim only with a recorded `trim_reason`; recover setup/payoff/reaction by widening `start`/`end` while the same event continues. For risky on-screen text/material try truthful trimming, cropping, blur, or muting before rejecting. Reject weak filler with a reason.
3. Write `runs/<id>/candidates/reviews.json` (list of `{id, verdict: accept|reject, reason, theme, title (2-5 words, from what is actually said), headline (2-6 words, different from title), treatment, standalone_only?, start?, end?, trim_reason?, visual_review: "claude-agent"}`). Titles and headlines must be truthful; never invent drama. Candidates you leave out get the metrics-only auto review (`visual_review: auto-metrics-only`), which the zero-touch gate will NOT post unless a visual-safety reviewer is configured.
4. `make-clips --source <path> --content-type auto` (full run; resumes from cache): ranking pass + standalone pass, duplicate/reuse policy, render, QA, routing to `approved/{rankings,standalone}/<source-id>/` or `needs-review/<source-id>/` with `FAILURE.txt`. Failures are logged, never silently dropped.
5. Report the number of FINAL videos (not candidates): `runs/<id>/REPORT.md`.

## Guarantees
Every output has a manifest (source id/timecodes, content type, candidate ids, format, hook, setup, payoff, runtime, transcript confidence, visual and audio evidence, reframing, editing decisions, reuse, rights, QA). Captions use only words both Whisper decodes confirm; profanity is uncensored. Layouts: blur-fit (default, whole scene), fill, face-track (only when one 9:16 window holds the faces), optional punch-in at a real impact. Public publishing needs the user; the zero-touch service (`docs/zero-touch-service.md`) never bypasses that.
