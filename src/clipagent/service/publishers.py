"""Delivery back-ends behind one interface. Only OFFICIAL TikTok endpoints are ever used (no browser automation, no unofficial client).

  SimulatedPublisher     - in-memory, programmable failures (tests and dry runs)
  LocalQueuePublisher    - moves the finished video + post package into ready-to-post/ (the compliant fallback when no API delivery is possible)
  TikTokOfficialPublisher- Content Posting API: inbox upload (video.upload, creator finishes the post in TikTok) or, only when legally supported, Direct Post
"""
import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class PublisherError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind            # auth | daily_cap | rate_limit | transient | permanent | unaudited


class AuthExpired(PublisherError):
    def __init__(self, message="TikTok authorization expired or revoked: re-run `clipagent service auth`"):
        super().__init__("auth", message)


# status vocabulary used by the daemon
PROCESSING, DELIVERED, PUBLISHED, FAILED = "PROCESSING", "DELIVERED", "PUBLISHED", "FAILED"


class SimulatedPublisher:
    """Deterministic fake. `plan` is a list of behaviours consumed one per upload: 'ok' | PublisherError kinds | 'crash_after_send'."""
    name = "simulated"

    def __init__(self, plan=None, mode="PRIVATE_OR_DRAFT_QUEUE"):
        self.plan, self.mode = list(plan or []), mode
        self.sent: dict[str, dict] = {}                   # publish_id -> package (what TikTok "received")
        self.uploads_attempted = 0
        self.status_script: dict[str, list] = {}
        self.metrics_by_post: dict[str, dict] = {}
        self.ready = {"ok": True, "detail": "simulated"}
        self.posts: dict[str, str] = {}

    def check_ready(self) -> dict:
        return {**self.ready, "mode": self.mode}

    def upload(self, mp4: Path, package: dict, idem: str) -> str:
        self.uploads_attempted += 1
        step = self.plan.pop(0) if self.plan else "ok"
        if step in ("auth", "daily_cap", "rate_limit", "transient", "permanent", "unaudited"):
            raise PublisherError(step, f"simulated {step}")
        pid = f"pub_{len(self.sent) + 1}_{idem[:8]}"
        self.sent[pid] = {"idem": idem, "title": package["title"], "file": str(mp4)}
        if step == "crash_after_send":
            raise KeyboardInterrupt("simulated crash after TikTok received the video but before the service recorded the publish id")
        return pid

    def status(self, publish_id: str) -> dict:
        script = self.status_script.get(publish_id)
        if script:
            return script.pop(0) if len(script) > 1 else script[0]
        post = f"post_{publish_id}"
        self.posts[post] = publish_id
        return {"status": PUBLISHED, "post_id": post, "fail_reason": None, "retryable": False}

    def video_metrics(self, post_ids):
        return {p: self.metrics_by_post[p] for p in post_ids if p in self.metrics_by_post}

    def verify_post(self, post_id: str, package: dict) -> tuple[bool, str]:
        return (post_id in self.posts), "simulated account owns the post" if post_id in self.posts else "post not found"

    def find_existing(self, idem: str):
        for pid, p in self.sent.items():
            if p["idem"] == idem:
                return pid
        return None


class LocalQueuePublisher:
    """No API delivery: the finished video and its package go to ready-to-post/<date>/ for the creator to post. This can never be 'published' on its own."""
    name = "local-queue"
    mode = "PRIVATE_OR_DRAFT_QUEUE"

    def __init__(self, root: Path):
        self.root = Path(root) / "ready-to-post"

    def check_ready(self) -> dict:
        return {"ok": True, "detail": "local ready-to-post queue", "mode": self.mode}

    def upload(self, mp4: Path, package: dict, idem: str) -> str:
        dest = self.root / idem[:12]
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "post-package.json").write_text(json.dumps(package, indent=2))
        (dest / "caption.txt").write_text(package["caption"] + "\n")
        target = dest / Path(mp4).name
        if not target.exists():
            shutil.copy2(mp4, target)                 # copy: the ledger'd render stays where it is until the post is confirmed
        return f"local-{idem[:12]}"

    def status(self, publish_id: str) -> dict:
        return {"status": DELIVERED, "post_id": None, "fail_reason": None, "retryable": False}

    def video_metrics(self, post_ids):
        return {}

    def verify_post(self, post_id: str, package: dict):
        return False, "local queue cannot verify a post"

    def find_existing(self, idem: str):
        return f"local-{idem[:12]}" if (self.root / idem[:12] / "post-package.json").exists() else None


class HTTP:
    def request(self, method: str, url: str, headers: dict, body: bytes | None = None, timeout: float = 60) -> tuple[int, dict | bytes]:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                status = r.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        except urllib.error.URLError as e:
            raise PublisherError("transient", f"network error: {e.reason}") from e
        try:
            return status, json.loads(raw or b"{}")
        except ValueError:
            return status, raw


ERROR_KINDS = {"access_token_invalid": "auth", "scope_not_authorized": "auth", "spam_risk_too_many_posts": "daily_cap", "spam_risk_too_many_pending_share": "daily_cap",
               "rate_limit_exceeded": "rate_limit", "unaudited_client_can_only_post_to_private_accounts": "unaudited", "spam_risk_user_banned_from_posting": "permanent",
               "invalid_param": "permanent", "privacy_level_option_mismatch": "permanent", "url_ownership_unverified": "permanent", "reached_active_user_cap": "daily_cap"}


class TikTokOfficialPublisher:
    name = "tiktok-official"

    def __init__(self, cfg: dict, tokens, mode: str, http=None, clock=None):
        import time
        self.cfg, self.tokens, self.mode, self.http, self.clock = cfg, tokens, mode, http or HTTP(), clock or time.time
        self.base = cfg["tiktok"]["api_base"].rstrip("/")

    # ---- auth
    def access_token(self) -> str:
        tok = self.tokens.load()
        if not tok:
            raise AuthExpired("no TikTok authorization stored: run `clipagent service auth`")
        now = self.clock()
        if now >= tok["refresh_expires_at"]:
            raise AuthExpired("the TikTok refresh token expired: run `clipagent service auth` again")
        if now >= tok["access_expires_at"] - 600:
            tok = self._refresh(tok)
        return tok["access_token"]

    def _refresh(self, tok: dict) -> dict:
        key, secret = self.tokens.client()
        if not key or not secret:
            raise AuthExpired("TikTok client key/secret missing from the Keychain")
        body = urllib.parse.urlencode({"client_key": key, "client_secret": secret, "grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}).encode()
        st, res = self.http.request("POST", f"{self.base}/v2/oauth/token/", {"Content-Type": "application/x-www-form-urlencoded"}, body)
        if st != 200 or not isinstance(res, dict) or "access_token" not in res:
            err = (res or {}).get("error") if isinstance(res, dict) else None
            if err in ("invalid_grant", "invalid_request") or st in (400, 401):
                raise AuthExpired(f"token refresh rejected ({err or st}): re-authorize with `clipagent service auth`")
            raise PublisherError("transient", f"token refresh failed ({st})")
        return self.tokens.save(res)                       # rotated refresh token stored immediately

    def _api(self, path: str, payload: dict | None, method="POST", params: str = "") -> dict:
        h = {"Authorization": f"Bearer {self.access_token()}", "Content-Type": "application/json; charset=UTF-8"}
        st, res = self.http.request(method, f"{self.base}{path}{params}", h, json.dumps(payload).encode() if payload is not None else None)
        if not isinstance(res, dict):
            raise PublisherError("transient", f"non-JSON response ({st})")
        code = (res.get("error") or {}).get("code", "ok")
        if st >= 500:
            raise PublisherError("transient", f"TikTok server error {st}")
        if code != "ok":
            raise PublisherError(ERROR_KINDS.get(code, "permanent" if st < 500 else "transient"), f"{code}: {(res.get('error') or {}).get('message', '')}")
        return res.get("data") or {}

    # ---- interface
    def creator_info(self) -> dict:
        return self._api("/v2/post/publish/creator_info/query/", {})

    def check_ready(self) -> dict:
        try:
            info = self.creator_info() if self.mode == "COMPLIANT_DIRECT_POST" else {}
            self.access_token()
            return {"ok": True, "detail": "token valid", "mode": self.mode, "creator": info.get("creator_username")}
        except PublisherError as e:
            return {"ok": False, "detail": str(e), "mode": self.mode, "kind": e.kind}

    def upload(self, mp4: Path, package: dict, idem: str) -> str:
        size = os.path.getsize(mp4)
        chunk = size if size <= 64 * 2 ** 20 else 32 * 2 ** 20
        total = max(1, size // chunk)                      # the last chunk absorbs the remainder
        src = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": total}
        if self.mode == "COMPLIANT_DIRECT_POST":
            info = self.creator_info()
            want = package["settings"]["privacy_level"]
            if want not in info.get("privacy_level_options", []):
                raise PublisherError("permanent", f"privacy_level_option_mismatch: {want} not in {info.get('privacy_level_options')}")
            s = package["settings"]
            post_info = {"title": package["caption"][:2200], "privacy_level": want, "disable_comment": not s["allow_comment"] or info.get("comment_disabled", False),
                         "disable_duet": not s["allow_duet"] or info.get("duet_disabled", False), "disable_stitch": not s["allow_stitch"] or info.get("stitch_disabled", False),
                         "video_cover_timestamp_ms": package["cover_timestamp_ms"]}
            data = self._api("/v2/post/publish/video/init/", {"post_info": post_info, "source_info": src})
        else:
            data = self._api("/v2/post/publish/inbox/video/init/", {"source_info": src})
        up = data["upload_url"]
        with open(mp4, "rb") as f:
            for i in range(total):
                n = chunk if i < total - 1 else size - chunk * (total - 1)
                start = chunk * i
                body = f.read(n)
                st, _ = self.http.request("PUT", up, {"Content-Type": "video/mp4", "Content-Length": str(n), "Content-Range": f"bytes {start}-{start + n - 1}/{size}"}, body, timeout=600)
                if st >= 500:
                    raise PublisherError("transient", f"chunk upload failed ({st})")
                if st >= 400:
                    raise PublisherError("permanent", f"chunk upload rejected ({st})")
        return data["publish_id"]

    def status(self, publish_id: str) -> dict:
        d = self._api("/v2/post/publish/status/fetch/", {"publish_id": publish_id})
        s, why = d.get("status"), d.get("fail_reason")
        ids = d.get("publicaly_available_post_id") or []                     # (sic) TikTok's spelling
        if s == "PUBLISH_COMPLETE":
            return {"status": PUBLISHED, "post_id": str(ids[0]) if ids else None, "fail_reason": None, "retryable": False}
        if s == "SEND_TO_USER_INBOX":
            return {"status": DELIVERED, "post_id": None, "fail_reason": None, "retryable": False}
        if s == "FAILED":
            return {"status": FAILED, "post_id": None, "fail_reason": why, "retryable": why in ("internal", None)}
        return {"status": PROCESSING, "post_id": None, "fail_reason": None, "retryable": False}

    def video_metrics(self, post_ids):
        out = {}
        for i in range(0, len(post_ids), 20):
            d = self._api("/v2/video/query/", {"filters": {"video_ids": post_ids[i:i + 20]}}, params="?fields=id,view_count,like_count,comment_count,share_count")
            for v in d.get("videos", []):
                out[str(v["id"])] = {"views": v.get("view_count"), "likes": v.get("like_count"), "comments": v.get("comment_count"), "shares": v.get("share_count")}
        return out

    def verify_post(self, post_id: str, package: dict) -> tuple[bool, str]:
        d = self._api("/v2/video/query/", {"filters": {"video_ids": [post_id]}}, params="?fields=id,create_time")
        found = any(str(v.get("id")) == str(post_id) for v in d.get("videos", []))
        return found, ("post found under the authorized account" if found else "post not returned for the authorized account")

    def find_existing(self, idem: str):
        return None                                                          # TikTok offers no lookup by our key: ambiguity is handled conservatively by the daemon
