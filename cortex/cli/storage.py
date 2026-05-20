"""cortex storage — inspect and maintain framework storage."""
from __future__ import annotations

import asyncio
from pathlib import Path

import click


def _hr_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


@click.group("storage")
def storage_group():
    """Inspect and maintain framework storage."""
    pass


# ── status ────────────────────────────────────────────────────────────────────

@storage_group.command("status")
@click.option("--config", default="cortex.yaml")
def storage_status(config: str):
    """Show storage paths, sizes, and backend health."""
    import sys
    from cortex.config.loader import load_config

    try:
        cfg = load_config(config)
    except Exception as exc:
        click.echo(click.style(f"✗ {exc}", fg="red"), err=True)
        sys.exit(1)

    base = Path(cfg.storage.base_path)
    click.echo(click.style("\nStorage Status", bold=True))
    click.echo(f"  Base path  : {base}")
    click.echo(f"  Exists     : {click.style('yes', fg='green') if base.exists() else click.style('no', fg='red')}")

    if not base.exists():
        click.echo()
        return

    subdirs = {
        "history"   : base / "history",
        "blueprints": (Path(getattr(getattr(cfg, "blueprints", None), "dir", None) or base / "blueprints")),
        "ants"      : base / "ants",
        "delta"     : base / "cortex_delta",
        "snapshots" : base / "snapshots",
    }

    click.echo()
    click.echo(click.style("  Directory Sizes", bold=True))
    click.echo(f"  {'PATH':<20} {'SIZE':>10}  EXISTS")
    click.echo(f"  {'-'*44}")
    for label, path in subdirs.items():
        exists = path.exists()
        size = _hr_size(_dir_size(path)) if exists else "—"
        exists_str = click.style("yes", fg="green") if exists else click.style("no", fg="bright_black")
        click.echo(f"  {label:<20} {size:>10}  {exists_str}")

    # SQLite DB file
    db_file = base / "cortex_sessions.db"
    if db_file.exists():
        click.echo()
        click.echo(click.style("  Database", bold=True))
        click.echo(f"  cortex_sessions.db : {_hr_size(db_file.stat().st_size)}")

    # Session count
    history_root = base / "history"
    if history_root.exists():
        users = [d for d in history_root.iterdir() if d.is_dir()]
        session_count = sum(
            len(list((u / "sessions").glob("*.json")))
            for u in users
            if (u / "sessions").exists()
        )
        click.echo()
        click.echo(click.style("  Sessions", bold=True))
        click.echo(f"  Users    : {len(users)}")
        click.echo(f"  Sessions : {session_count}")

    # Ants file
    ants_file = base / "ants.yaml"
    if ants_file.exists():
        click.echo()
        click.echo(click.style("  Ant Colony", bold=True))
        click.echo(f"  ants.yaml : {_hr_size(ants_file.stat().st_size)}")

    click.echo()


# ── purge ─────────────────────────────────────────────────────────────────────

@storage_group.command("purge")
@click.option("--config", default="cortex.yaml")
@click.option("--older-than", "older_than_days", default=30, show_default=True,
              help="Delete sessions older than N days")
@click.option("--user-id", default=None, help="Purge only this user (omit for all users)")
@click.option("--yes", is_flag=True, help="Skip confirmation")
def storage_purge(config: str, older_than_days: int, user_id: str | None, yes: bool):
    """Delete session history older than N days."""
    if not yes:
        scope = f"user '{user_id}'" if user_id else "all users"
        click.confirm(
            f"Delete sessions older than {older_than_days} days for {scope}?",
            abort=True,
        )
    asyncio.run(_storage_purge(config, older_than_days, user_id))


async def _storage_purge(config_path: str, older_than_days: int, user_id: str | None) -> None:
    from cortex.config.loader import load_config
    from cortex.modules.history_store import HistoryStore

    cfg = load_config(config_path)
    store = HistoryStore(base_path=cfg.storage.base_path)

    base = Path(cfg.storage.base_path)
    history_root = base / "history"

    if user_id:
        users = [user_id]
    elif history_root.exists():
        users = [d.name for d in history_root.iterdir() if d.is_dir()]
    else:
        users = []

    total_deleted = 0
    for uid in users:
        try:
            deleted = await store.auto_cleanup(uid, retention_days=older_than_days)
            total_deleted += deleted
            if deleted:
                click.echo(f"  {uid}: {deleted} session(s) deleted")
        except Exception as exc:
            click.echo(click.style(f"  {uid}: error — {exc}", fg="yellow"), err=True)

    if total_deleted:
        click.echo(click.style(f"\n✓ Purged {total_deleted} session(s) total.", fg="green"))
    else:
        click.echo("No sessions matched the purge criteria.")
