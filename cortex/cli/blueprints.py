"""cortex blueprints — manage per-task learning blueprints."""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import click


@click.group("blueprints")
def blueprints_group():
    """List, view, edit, and delete task learning blueprints."""
    pass


def _blueprint_dir(config_path: str) -> Path:
    from cortex.config.loader import load_config
    cfg = load_config(config_path)
    bp_cfg = getattr(cfg, "blueprints", None)
    if bp_cfg and getattr(bp_cfg, "dir", None):
        return Path(bp_cfg.dir)
    return Path(cfg.storage.base_path) / "blueprints"


# ── list ──────────────────────────────────────────────────────────────────────

@blueprints_group.command("list")
@click.option("--config", default="cortex.yaml")
@click.option("--format", "fmt", default="table", type=click.Choice(["table", "names"]))
def blueprints_list(config: str, fmt: str):
    """List all stored blueprints."""
    bp_dir = _blueprint_dir(config)
    if not bp_dir.exists():
        click.echo("No blueprints directory found (no blueprints have been created yet).")
        return

    files = sorted(bp_dir.glob("*.md"))
    if not files:
        click.echo("No blueprints found.")
        return

    if fmt == "names":
        for f in files:
            click.echo(f.stem)
        return

    click.echo(f"\n{'NAME':<50} {'SIZE':>8} {'MODIFIED':<20}")
    click.echo("-" * 82)
    for f in files:
        stat = f.stat()
        size = f"{stat.st_size:,} B"
        from datetime import datetime
        mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        click.echo(f"{f.stem:<50} {size:>8} {mtime:<20}")
    click.echo(f"\n{len(files)} blueprint(s) in {bp_dir}\n")


# ── show ──────────────────────────────────────────────────────────────────────

@blueprints_group.command("show")
@click.argument("name")
@click.option("--config", default="cortex.yaml")
def blueprints_show(name: str, config: str):
    """Print a blueprint's content."""
    asyncio.run(_blueprints_show(name, config))


async def _blueprints_show(name: str, config_path: str) -> None:
    from cortex.config.loader import load_config
    from cortex.modules.blueprint_store import BlueprintStore

    cfg = load_config(config_path)
    bp_cfg = getattr(cfg, "blueprints", None)
    bp_dir = (Path(bp_cfg.dir) if bp_cfg and getattr(bp_cfg, "dir", None)
               else Path(cfg.storage.base_path) / "blueprints")

    store = BlueprintStore(dir_path=str(bp_dir))
    bp = await store.load(name)
    if bp is None:
        click.echo(f"Blueprint '{name}' not found.", err=True)
        raise SystemExit(1)

    click.echo(click.style(f"Blueprint: {bp.name}", bold=True))
    click.echo(f"Task     : {bp.task_name}")
    click.echo(f"Version  : {bp.version}")
    click.echo(f"Updated  : {bp.updated_at}")
    click.echo()
    click.echo(bp.to_markdown())


# ── edit ──────────────────────────────────────────────────────────────────────

@blueprints_group.command("edit")
@click.argument("name")
@click.option("--config", default="cortex.yaml")
def blueprints_edit(name: str, config: str):
    """Open a blueprint in $EDITOR for manual editing."""
    bp_dir = _blueprint_dir(config)
    candidates = [bp_dir / f"{name}.md", bp_dir / name]
    path = None
    for c in candidates:
        if c.exists():
            path = c
            break

    if path is None:
        click.echo(f"Blueprint '{name}' not found in {bp_dir}.", err=True)
        raise SystemExit(1)

    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))
    subprocess.run([editor, str(path)], check=False)
    click.echo(f"✓ Saved {path}")


# ── delete ────────────────────────────────────────────────────────────────────

@blueprints_group.command("delete")
@click.argument("name")
@click.option("--config", default="cortex.yaml")
@click.option("--yes", is_flag=True, help="Skip confirmation")
def blueprints_delete(name: str, config: str, yes: bool):
    """Delete a blueprint permanently."""
    bp_dir = _blueprint_dir(config)
    candidates = [bp_dir / f"{name}.md", bp_dir / name]
    path = None
    for c in candidates:
        if c.exists():
            path = c
            break

    if path is None:
        click.echo(f"Blueprint '{name}' not found.", err=True)
        raise SystemExit(1)

    if not yes:
        click.confirm(f"Delete blueprint '{path.name}'?", abort=True)

    path.unlink()
    click.echo(click.style(f"✓ Deleted {path.name}", fg="green"))
