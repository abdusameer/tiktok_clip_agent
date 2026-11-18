"""Credential storage. No password, client secret or token is ever written to source, config or logs: they live in the macOS Keychain (via the `security`
tool, fed through `security -i` on stdin so values never appear in a process list) or, in tests, an in-memory backend."""
import json
import subprocess
import time


class KeychainBackend:
    ACCOUNT = "clipagent"

    def get(self, service: str) -> str | None:
        r = subprocess.run(["security", "find-generic-password", "-s", service, "-a", self.ACCOUNT, "-w"], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None

    def set(self, service: str, value: str) -> None:
        cmd = f"add-generic-password -U -s {service} -a {self.ACCOUNT} -w {json.dumps(value)}\n"
        r = subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError("could not write to the macOS Keychain")

    def delete(self, service: str) -> None:
        subprocess.run(["security", "delete-generic-password", "-s", service, "-a", self.ACCOUNT], capture_output=True)


class MemoryBackend:
    def __init__(self):
        self.d: dict[str, str] = {}

    def get(self, service):
        return self.d.get(service)

    def set(self, service, value):
        self.d[service] = value

    def delete(self, service):
        self.d.pop(service, None)


class TokenStore:
    def __init__(self, cfg: dict, backend=None, clock=time.time):
        self.t, self.b, self.clock = cfg["tiktok"], backend or KeychainBackend(), clock

    def client(self) -> tuple[str | None, str | None]:
        return self.b.get(self.t["client_key_keychain"]), self.b.get(self.t["client_secret_keychain"])

    def set_client(self, key: str, secret: str) -> None:
        self.b.set(self.t["client_key_keychain"], key)
        self.b.set(self.t["client_secret_keychain"], secret)

    def load(self) -> dict | None:
        raw = self.b.get(self.t["token_keychain"])
        return json.loads(raw) if raw else None

    def save(self, tok: dict) -> dict:
        """tok: raw token response. Stores absolute expiry times; the rotated refresh token replaces the old one at once (a reused old one would invalidate the grant)."""
        now = self.clock()
        rec = {"access_token": tok["access_token"], "refresh_token": tok["refresh_token"], "open_id": tok.get("open_id"), "scope": tok.get("scope"),
               "access_expires_at": now + float(tok["expires_in"]), "refresh_expires_at": now + float(tok.get("refresh_expires_in", 180 * 86400))}
        self.b.set(self.t["token_keychain"], json.dumps(rec))
        return rec

    def clear(self) -> None:
        self.b.delete(self.t["token_keychain"])
