"""Service configuration (config/service.json). Defaults are conservative; no secrets live here (tokens are in the macOS Keychain)."""
import copy
import json
from pathlib import Path

from .. import config as project_config

DEFAULTS = {
    "mode": "PRIVATE_OR_DRAFT_QUEUE",                    # or COMPLIANT_DIRECT_POST (only honoured with the attestations below)
    "dry_run": True,                                     # dry run: everything except real uploads and real deletions
    "timezone": "America/Los_Angeles",
    "inbox": "sourcevideo/inbox",
    "stable_seconds": 30,                                # a file must stop changing this long before it is ingested
    "emergency_stop_file": "STOP",
    "tick_seconds": 30,
    "posting": {"daily_ceiling": 3, "min_spacing_minutes": 180, "lead_slot": "14:00", "candidate_slots": ["11:00", "14:00", "16:00", "19:00", "21:00"],
                "slot_window_minutes": 30, "explore_fraction": 0.2, "min_observations_per_arm": 8, "inbox_pending_cap_per_24h": 5},
    "post_defaults": {"privacy_level": "SELF_ONLY", "allow_comment": True, "allow_duet": False, "allow_stitch": False},
    "metadata": {"streamer": None, "game_or_topic": None, "event": None},          # optional defaults; per-source sidecar `<file>.meta.json` overrides
    "storage": {"max_fraction_of_disk": 0.20, "min_free_fraction": 0.15, "high_water_fraction_of_budget": 0.90,
                "recovery_days": 7, "rejected_retention_days": 14, "temp_ttl_hours": 6},
    "retry": {"max_attempts": 5, "base_seconds": 60, "cap_seconds": 3600},
    "metrics_poll_hours": [1, 6, 24, 72, 168],
    "gate": {"min_editorial_score": 0.6, "near_duplicate_threshold": 0.6, "visual_safety": {"mode": "block_until_reviewed", "command": None}},
    "direct_post": {"audited_client": False, "audit_reference": None, "consent_recorded_at": None, "ux_flow_attested": False},
    "tiktok": {"client_key_keychain": "clipagent-tiktok-client-key", "client_secret_keychain": "clipagent-tiktok-client-secret",
               "token_keychain": "clipagent-tiktok-tokens", "api_base": "https://open.tiktokapis.com", "redirect_uri": "http://localhost:8765/callback"},
    "daily_summary_hour": 21,
}


def deep_merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in b.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def path(root=None) -> Path:
    return (Path(root) if root else project_config.project_root()) / "config" / "service.json"


def load(root=None, overrides: dict | None = None) -> dict:
    p = path(root)
    cfg = deep_merge(DEFAULTS, json.loads(p.read_text()) if p.is_file() else {})
    return deep_merge(cfg, overrides or {})


def save_default(root=None) -> Path:
    p = path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(json.dumps(DEFAULTS, indent=2) + "\n")
    return p


def effective_mode(cfg: dict) -> tuple[str, str | None]:
    """(mode actually used, reason it was downgraded or None). Direct Post requires an audited client, a recorded creator consent and an attested
    compliant UX flow; otherwise the draft/inbox queue is used. Never pretends approval exists."""
    if cfg["mode"] != "COMPLIANT_DIRECT_POST":
        return "PRIVATE_OR_DRAFT_QUEUE", None
    d = cfg["direct_post"]
    missing = [k for k, ok in (("audited_client", d.get("audited_client")), ("consent_recorded_at", d.get("consent_recorded_at")),
                               ("ux_flow_attested", d.get("ux_flow_attested"))) if not ok]
    if missing:
        return "PRIVATE_OR_DRAFT_QUEUE", ("COMPLIANT_DIRECT_POST requested but not legally supported by the recorded configuration (missing: " + ", ".join(missing) +
                                          "). TikTok restricts unaudited clients to private posts and requires per-upload creator consent; falling back to the draft/inbox queue.")
    return "COMPLIANT_DIRECT_POST", None
