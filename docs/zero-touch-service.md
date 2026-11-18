# Zero-touch service

`clipagent service ...` turns `/make-clips` into a continuous service: it watches `sourcevideo/inbox/`, makes and gates clips, schedules and delivers them through **official TikTok APIs only**, polls official metrics, learns, and protects the disk. Everything is a saved state in `service/state.db`; a crash or restart resumes instead of repeating.

## Read this first: what the official API does and does not allow
Verified against TikTok's developer documentation on 2026-11-17 (the pages are linked at the end).

* **Inbox/draft upload** (`video.upload`): the video arrives in the creator's TikTok inbox and **the creator must open the notification and post it in the app**. Up to **5 pending shares per 24 h**. This step cannot be automated.
* **Direct Post** (`video.publish`): posts straight to the profile. **Unaudited clients are restricted to private (`SELF_ONLY`) posts.** TikTok's UX rules require the creator to choose the title and privacy level (no default), turn on comments/duet/stitch manually, and expressly consent to each upload. A headless service cannot honestly perform that interaction.
* **Metrics** (`video.list`): views, likes, comments, shares only. Average watch time, completion rate, rewatches, saves, follower conversions and profile visits are **not available** and are recorded as unavailable.
* Rate limits: 6 init requests/min per token, 30 status requests/min; a daily post cap exists (number not published; `spam_risk_too_many_posts`).

So the service has two modes and never pretends otherwise:

| Mode | When | What happens |
|---|---|---|
| `PRIVATE_OR_DRAFT_QUEUE` (default) | always available | Uploads to the creator's inbox (needs one-time OAuth) or, with no authorization, places the video + post package in `ready-to-post/`. **The final public posting step is manual** and the daily summary says so. When you post it, run `clipagent service mark-posted <job> --post-id <id>`; or the service confirms it itself when TikTok reports `PUBLISH_COMPLETE` and the post is found under your account. |
| `COMPLIANT_DIRECT_POST` | only if `config/service.json` records `audited_client: true`, `audit_reference`, `consent_recorded_at` and `ux_flow_attested: true` | Direct Post with `privacy_level` from `creator_info`. If any attestation is missing the service **refuses and falls back to the draft queue**, and says why in the log/summary. A TikTok `unaudited_client...` error also drops it to the draft queue. |

There is no browser automation, no unofficial client, no cap evasion, and no password or secret in source or config (client key/secret/tokens live in the macOS Keychain). Access tokens live ~2 h and refresh tokens ~180 days and rotate; the service refreshes automatically and, if the grant dies, pauses posting only and asks for `clipagent service auth` while every other stage keeps running.

## One-time setup (you)
1. `PYTHONPATH=src .venv/bin/python -m clipagent.cli service init` : writes `config/service.json` (dry run, draft queue), the database, the budget report.
2. Edit `config/service.json` (streamer/game/topic metadata, posting ceiling, slots, `gate.visual_safety`).
3. For API delivery: create a TikTok developer app, then `service set-client` (Keychain) and `service auth` (browser consent).
4. Set `"dry_run": false` when you are ready; `service install-launchd --enable` starts it under launchd (RunAtLoad, KeepAlive, throttle, logs in `logs/`).

## The lifecycle
`INGESTED → ANALYZED → CANDIDATES_SELECTED → EDITED → QA_PASSED → QUEUED → UPLOADED → PUBLISH_CONFIRMED → METRICS_COLLECTED → LEARNED → QUARANTINED → PURGED` (side states: `REJECTED`, `FAILED`, `UPLOAD_UNCERTAIN`, `BLOCKED_RIGHTS`, `DUPLICATE_SOURCE`, `SOURCE_DONE`). Every transition is one SQLite compare-and-swap with a history row.

* **No duplicate posts**: an idempotency key (source hash + project + render hash) makes a render a single job; an upload records its *intent before* contacting TikTok. After a crash, an intent without a publish id is looked up; if TikTok cannot confirm it, the job becomes `UPLOAD_UNCERTAIN` and is **not re-sent**.
* **Failures are isolated**: one failing source/video gets exponential backoff and, after `retry.max_attempts`, `FAILED` (its render is kept: the only copy of a failed upload is never deleted). Rejections are logged with exact reasons and never interrupt the queue.
* **Emergency stop**: create `STOP` in the project root (`service stop`); the service keeps its heartbeat and does nothing else.
* **Health**: `service health` exits non-zero when the heartbeat is stale. Structured logs: `logs/service.log` (JSON lines). Daily summary: `logs/daily-summary-YYYY-MM-DD.{md,json}` (needs-attention list first).

## The auto-posting gate (all must pass, else `REJECTED` with reasons)
Clip QA (resolution/9:16/H.264/AAC/faststart, A/V sync, black/frozen frames, clipping, silent audio, cut-in-word, caption accuracy from two decodes, caption safe area, subject visibility for tracked crops, ranking numbers, byte duplicates) **plus**: rights resolved, no severe language in captions/on-screen text/title, a full playback test, an editorial-score floor, near-duplicate protection against everything already posted, and **visual safety**.

**Visual safety needs a reviewer.** Audio/text metrics cannot tell whether the picture contains sexual, violent or hateful imagery. By default (`gate.visual_safety.mode = "block_until_reviewed"`) a clip is posted only if every entry was visually reviewed by the Claude agent (`/make-clips` writes `visual_review: claude-agent`), or if `gate.visual_safety.command` (your own classifier, prints `{"safe": true|false, "reason": "..."}`) approves it. Metrics-only clips are rejected with `blocked: visual safety review`. You can choose `"metrics_only_allowed"` explicitly if you accept that risk; the service will not choose it for you.

## Post package
Title (`headline | game/topic | streamer`, ≤ 90 chars), a description that quotes what was actually said, 3-5 hashtags from the real topic/streamer/content type (never `#fyp`, `#viral`, `#foryou`, trends), cover timestamp at the peak, and comment/duet/stitch from config. Per-source overrides in `<file>.meta.json` (`streamer`, `game_or_topic`, `event`, `hashtags`, `content_type`).

## Scheduler (Pacific Time)
Daily ceiling (also ≤ 5 in inbox mode), minimum spacing, error backoff (exponential; to the next Pacific day after a daily-cap error), a persistent queue. The 14:00 slot leads **only as a starting hypothesis** (one earlier observation). A single controlled experiment tests one variable at a time (slot, then caption style) on a fraction of posts. A challenger replaces the leader only with ≥ 8 observations per arm, ≥ 25 % better median age-adjusted performance and a permutation-test p < 0.1; one post can never change the strategy. "Viral" is account-relative: age-adjusted views/hour vs the account's rolling median at the same post age (breakout ≥ 3× with ≥ 10 baseline posts). Metrics are polled at 1 h, 6 h, 24 h, 72 h and 7 d.

## Storage protection
Budget = ≤ 20 % of the disk, ≥ 15 % always free (ingestion pauses 2 % before the reserve), emergency cleanup at 90 % of the budget (temp artifacts, then quarantined media oldest first). Every managed file is in a ledger (path, type, source, size, sha256, created, state, retention class, delete-after). Deletion goes through one guard: exact ledger path, inside the project's quarantine/temp locations, no symlinks, retention class not protected, never a directory, never recursive; dry run deletes nothing. Retention: after a **confirmed** post (publish id → complete → post found under the account → visibility consistent) the render and the processed source move to `quarantine/` for 7 days, then are purged; manifest, recipe, source hash, captions, title, post id, metrics and QA record stay forever. A source with unfinished candidates, an unconfirmed render, a failed upload's only copy, the database and anything outside the ledger are never deleted. A source that produced no confirmed post is kept (never deleted on a guess) and reported.

## Sources
Only authorized media: yours, permissioned, public-domain, licensed, or authorized streamer feeds. A rights record exists for every source (`runs/<id>/source.json`); movies/shows without explicit rights are blocked (`BLOCKED_RIGHTS`) and only that source stops. The service never fetches or reposts other accounts' clips.

## Sources (official docs read)
* https://developers.tiktok.com/doc/content-posting-api-get-started
* https://developers.tiktok.com/doc/content-posting-api-reference-direct-post
* https://developers.tiktok.com/doc/content-posting-api-reference-upload-video
* https://developers.tiktok.com/doc/content-posting-api-reference-get-video-status
* https://developers.tiktok.com/doc/content-posting-api-reference-query-creator-info
* https://developers.tiktok.com/doc/content-sharing-guidelines
* https://developers.tiktok.com/doc/tiktok-api-v2-video-query
* https://developers.tiktok.com/doc/oauth-user-access-token-management
