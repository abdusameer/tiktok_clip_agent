"""`clipagent service ...` commands."""
import getpass
import json
import os
import time
from pathlib import Path

import typer

from .. import config as project_config

app = typer.Typer(help="Zero-touch service: continuous, compliant, storage-safe clipping and delivery.", no_args_is_help=True)


def _root() -> Path:
    return project_config.project_root()


def _svc(**kw):
    from .daemon import Service
    return Service(_root(), **kw)


@app.command("init")
def init_cmd() -> None:
    """One-time setup: write config/service.json (dry-run, draft queue), create the database, detect disk capacity and the storage budget."""
    from . import config as c
    p = c.save_default(_root())
    (_root() / "sourcevideo" / "inbox").mkdir(parents=True, exist_ok=True)
    svc = _svc()
    b = svc.storage.budget()
    typer.echo(f"config: {p}")
    typer.echo(f"disk: {b['total_bytes'] / 1e9:.0f} GB total, {b['free_bytes'] / 1e9:.1f} GB free ({b['free_fraction']:.1%})")
    typer.echo(f"budget: max {b['budget_bytes'] / 1e9:.1f} GB for this system (20% cap), {svc.cfg['storage']['min_free_fraction']:.0%} always kept free")
    typer.echo("ingestion: " + ("PAUSED - " + "; ".join(b["pause_reasons"]) if b["ingestion_paused"] else "allowed"))
    typer.echo(f"mode: {svc.mode}" + (f"  ({svc.mode_note})" if svc.mode_note else ""))
    typer.echo("dry_run: true (nothing is uploaded or deleted until you set \"dry_run\": false in config/service.json)")


@app.command("run")
def run_cmd(once: bool = typer.Option(False, "--once", help="One tick then exit"), dry_run: bool = typer.Option(None, "--dry-run/--live")) -> None:
    """Run the service loop (what launchd starts)."""
    from . import config as c
    from .daemon import Service, run_forever
    cfg = c.load(_root(), {"dry_run": dry_run} if dry_run is not None else None)
    run_forever(Service(_root(), cfg), once=once)


@app.command("status")
def status_cmd() -> None:
    """Job counts, storage, mode, strategy and anything needing attention."""
    svc = _svc()
    path = svc.write_summary()
    typer.echo(path.with_suffix(".md").read_text())


@app.command("health")
def health_cmd(max_age: float = typer.Option(300.0)) -> None:
    """Exit 0 when the heartbeat is fresh (use as a health check)."""
    from .daemon import health
    ok, msg = health(_root(), max_age)
    typer.echo(("OK " if ok else "UNHEALTHY ") + msg)
    raise typer.Exit(code=0 if ok else 1)


@app.command("stop")
def stop_cmd() -> None:
    """Create the emergency stop file: the service keeps its heartbeat but does nothing else."""
    from . import config as c
    (_root() / c.load(_root())["emergency_stop_file"]).write_text(time.strftime("stopped %Y-%m-%d %H:%M:%S\n"))
    typer.echo("emergency stop engaged")


@app.command("resume")
def resume_cmd() -> None:
    from . import config as c
    f = _root() / c.load(_root())["emergency_stop_file"]
    if f.exists():
        os.unlink(f)
    typer.echo("resumed")


@app.command("set-client")
def set_client_cmd() -> None:
    """Store your TikTok developer app's client key and secret in the macOS Keychain (never in files)."""
    from .tokens import TokenStore
    from . import config as c
    key = typer.prompt("TikTok client key")
    secret = getpass.getpass("TikTok client secret (hidden): ")
    TokenStore(c.load(_root())).set_client(key, secret)
    typer.echo("stored in the Keychain")


@app.command("auth")
def auth_cmd(direct: bool = typer.Option(False, "--direct", help="Also request video.publish (only meaningful for an audited app)")) -> None:
    """One-time creator authorization in your browser (OAuth + PKCE)."""
    from . import config as c, oauth
    from .tokens import TokenStore
    cfg = c.load(_root())
    oauth.run_flow(cfg, TokenStore(cfg), direct=direct)
    typer.echo("authorized; tokens stored in the Keychain")


@app.command("mark-posted")
def mark_posted_cmd(job_id: str = typer.Argument(...), post_id: str = typer.Option(..., "--post-id")) -> None:
    """Record that the creator posted a delivered draft (the one step TikTok's official API leaves to the creator)."""
    typer.echo(json.dumps(_svc().mark_posted(job_id, post_id)["state"]))


@app.command("install-launchd")
def install_launchd_cmd(enable: bool = typer.Option(False, "--enable", help="Also load it now with launchctl")) -> None:
    """Write (and lint) the LaunchAgent plist; --enable also starts it. Not enabled until you say so."""
    from . import launchd
    p = launchd.write(_root(), python=str(_root() / ".venv" / "bin" / "python"))
    typer.echo(f"wrote {p}")
    if enable:
        typer.echo(launchd.load(p))
    else:
        typer.echo(f"not loaded. To start it: launchctl bootstrap gui/$(id -u) {p}")
