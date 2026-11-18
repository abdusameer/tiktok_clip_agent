"""One-time TikTok authorization (OAuth authorization-code flow with PKCE S256; desktop/loopback redirect). The user approves in their own browser; the
resulting tokens go straight into the Keychain. Nothing here can authorize on the user's behalf or skip their consent screen.

Verified from TikTok's docs: token endpoint https://open.tiktokapis.com/v2/oauth/token/ (code exchange and refresh), PKCE S256 only, access token ~2 h,
refresh token ~180 days and rotating. The authorize URL below follows TikTok's Login Kit for desktop; confirm it against the current Login Kit docs for your
app type before first use."""
import base64
import hashlib
import http.server
import secrets
import threading
import urllib.parse
import webbrowser

from .publishers import HTTP

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
SCOPES_INBOX = ["user.info.basic", "video.upload", "video.list"]
SCOPES_DIRECT = SCOPES_INBOX + ["video.publish"]


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def authorize_url(client_key: str, redirect_uri: str, scopes: list[str], state: str, challenge: str) -> str:
    q = {"client_key": client_key, "scope": ",".join(scopes), "response_type": "code", "redirect_uri": redirect_uri, "state": state,
         "code_challenge": challenge, "code_challenge_method": "S256"}
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(q)


def exchange_code(cfg: dict, client_key: str, client_secret: str, code: str, verifier: str, http=None) -> dict:
    http = http or HTTP()
    body = urllib.parse.urlencode({"client_key": client_key, "client_secret": client_secret, "code": code, "grant_type": "authorization_code",
                                   "redirect_uri": cfg["tiktok"]["redirect_uri"], "code_verifier": verifier}).encode()
    st, res = http.request("POST", cfg["tiktok"]["api_base"].rstrip("/") + "/v2/oauth/token/", {"Content-Type": "application/x-www-form-urlencoded"}, body)
    if st != 200 or not isinstance(res, dict) or "access_token" not in res:
        raise RuntimeError(f"token exchange failed ({st}): {res if isinstance(res, dict) else ''}")
    return res


def run_flow(cfg: dict, tokens, direct: bool = False, open_browser: bool = True, timeout: float = 300.0, http=None) -> dict:
    key, secret = tokens.client()
    if not key or not secret:
        raise RuntimeError("store the client key and secret first: `clipagent service set-client`")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    redirect = urllib.parse.urlparse(cfg["tiktok"]["redirect_uri"])
    got: dict = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Authorization received. You can close this tab.")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer((redirect.hostname, redirect.port), H)
    srv.timeout = timeout
    url = authorize_url(key, cfg["tiktok"]["redirect_uri"], SCOPES_DIRECT if direct else SCOPES_INBOX, state, challenge)
    print("Open this URL, sign in and approve (your consent is required and cannot be automated):\n" + url)
    if open_browser:
        webbrowser.open(url)
    t = threading.Thread(target=srv.handle_request, daemon=True)
    t.start()
    t.join(timeout)
    srv.server_close()
    if got.get("state") != state or "code" not in got:
        raise RuntimeError(f"authorization not completed: {got.get('error_description') or got.get('error') or 'no code received'}")
    return tokens.save(exchange_code(cfg, key, secret, got["code"], verifier, http))
