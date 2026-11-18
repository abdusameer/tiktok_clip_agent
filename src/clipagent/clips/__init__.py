"""/make-clips: local, autonomous clipping of authorized long-form media (streams, gameplay, movies, shows, podcasts, interviews).

Isolated from the original ranking/standalone pipeline: nothing here writes to source_library/, approved_clips/ or registry/.
Editorial policy lives only in CONTENT_EDITING_RULES.md (runtime is metadata, never a quality score).
"""
CONTENT_TYPES = ("streamer", "gaming", "movie", "show", "podcast", "interview")
PLATFORMS = ("tiktok",)
