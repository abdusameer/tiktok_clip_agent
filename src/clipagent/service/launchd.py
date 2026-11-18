"""launchd (macOS service manager) integration: a LaunchAgent with RunAtLoad + KeepAlive (automatic restart), throttle, structured stdout/stderr logs."""
import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.clipagent.service"


def plist(root: Path, python: str | None = None) -> dict:
    root = Path(root).resolve()
    return {"Label": LABEL, "ProgramArguments": [python or sys.executable, "-m", "clipagent.cli", "service", "run"], "WorkingDirectory": str(root),
            "EnvironmentVariables": {"CLIPAGENT_ROOT": str(root), "PYTHONPATH": str(root / "src"), "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30, "ProcessType": "Background", "Nice": 10,
            "StandardOutPath": str(root / "logs" / "service.stdout.log"), "StandardErrorPath": str(root / "logs" / "service.stderr.log")}


def write(root: Path, dest: Path | None = None, python: str | None = None) -> Path:
    dest = dest or Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        plistlib.dump(plist(root, python), f)
    lint = subprocess.run(["plutil", "-lint", str(dest)], capture_output=True, text=True)
    if lint.returncode:
        raise RuntimeError(lint.stdout + lint.stderr)
    return dest


def load(path: Path) -> str:
    uid = os.getuid()
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)], capture_output=True, text=True)
    return (r.stdout + r.stderr).strip() or "loaded"
