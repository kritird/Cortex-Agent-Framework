"""cortex delta — manage learning deltas."""
import asyncio
import click


@click.group()
def delta_group():
    """Manage learning deltas (review, apply, reject, history, rollback)."""
    pass


@delta_group.command("review")
@click.option("--config", default="cortex.yaml")
def delta_review(config: str):
    """Review pending delta proposals."""
    asyncio.run(_delta_review(config))


async def _delta_review(config_path: str):
    from pathlib import Path
    from cortex.config.loader import load_config
    import yaml
    cfg = load_config(config_path)
    pending_path = Path(cfg.storage.base_path) / "cortex_delta" / "pending.yaml"
    if not pending_path.exists():
        click.echo("No pending deltas.")
        return
    with open(pending_path) as f:
        pending = yaml.safe_load(f) or {}
    tasks = pending.get("task_types", [])
    if not tasks:
        click.echo("No pending task type proposals.")
        return
    click.echo(f"Pending delta proposals ({len(tasks)}):")
    for t in tasks:
        click.echo(f"\n  Task: {t['name']}")
        click.echo(f"  Confidence: {t.get('confidence', 'low')} ({t.get('confirmations', 0)} distinct users)")
        click.echo(f"  Description: {t.get('description', '')[:100]}")


@delta_group.command("apply")
@click.option("--config", default="cortex.yaml")
@click.option("--min-confidence", default="high", type=click.Choice(["high", "medium", "low"]))
@click.option("--yes", is_flag=True, help="Skip confirmation")
def delta_apply(config: str, min_confidence: str, yes: bool):
    """Apply pending deltas to cortex.yaml."""
    if not yes:
        click.confirm(f"Apply deltas with confidence >= {min_confidence} to {config}?", abort=True)
    asyncio.run(_delta_apply(config, min_confidence))


async def _delta_apply(config_path: str, min_confidence: str):
    from pathlib import Path
    from cortex.config.loader import load_config
    from cortex.modules.learning_engine import LearningEngine
    from cortex.config.schema import LearningConfig
    cfg = load_config(config_path)
    delta_path = str(Path(cfg.storage.base_path) / "cortex_delta")
    engine = LearningEngine(delta_path=delta_path, config=cfg.learning)
    result = await engine.apply_delta(delta_path=delta_path, cortex_yaml_path=config_path, min_confidence=min_confidence)
    if result.applied:
        click.echo(f"✓ Applied {len(result.applied)} delta(s): {', '.join(result.applied)}")
        click.echo(f"  Backup saved: {result.backup_path}")
    else:
        click.echo("No deltas met the confidence threshold.")
    if result.skipped:
        click.echo(f"  Skipped (below threshold): {', '.join(result.skipped)}")


@delta_group.command("reject")
@click.argument("task_name")
@click.option("--config", default="cortex.yaml")
def delta_reject(task_name: str, config: str):
    """Reject a pending delta proposal."""
    asyncio.run(_delta_reject(task_name, config))


async def _delta_reject(task_name: str, config_path: str):
    from pathlib import Path
    from cortex.config.loader import load_config
    import yaml
    cfg = load_config(config_path)
    pending_path = Path(cfg.storage.base_path) / "cortex_delta" / "pending.yaml"
    if not pending_path.exists():
        click.echo("No pending deltas.")
        return
    with open(pending_path) as f:
        pending = yaml.safe_load(f) or {}
    tasks = pending.get("task_types", [])
    filtered = [t for t in tasks if t.get("name") != task_name]
    if len(filtered) == len(tasks):
        click.echo(f"Task '{task_name}' not found in pending deltas.")
        return
    pending["task_types"] = filtered
    with open(pending_path, "w") as f:
        yaml.dump(pending, f)
    click.echo(f"✓ Rejected delta for task: {task_name}")


@delta_group.command("history")
@click.option("--config", default="cortex.yaml")
def delta_history(config: str):
    """Show delta apply history."""
    asyncio.run(_delta_history(config))


async def _delta_history(config_path: str):
    from pathlib import Path
    from cortex.config.loader import load_config
    import yaml
    cfg = load_config(config_path)
    history_path = Path(cfg.storage.base_path) / "cortex_delta" / "history"
    if not history_path.exists() or not list(history_path.glob("*.yaml")):
        click.echo("No delta history.")
        return
    for entry_file in sorted(history_path.glob("*.yaml"), reverse=True)[:10]:
        with open(entry_file) as f:
            entry = yaml.safe_load(f) or {}
        ts = entry.get("timestamp", entry_file.stem)
        applied = [t.get("name", "") for t in entry.get("applied", [])]
        click.echo(f"  {ts}: applied {', '.join(applied)}")


@delta_group.command("rollback")
@click.option("--config", default="cortex.yaml")
@click.option("--yes", is_flag=True)
def delta_rollback(config: str, yes: bool):
    """Rollback to the most recent cortex.yaml backup."""
    import glob
    backups = sorted(glob.glob(f"{config}.bak.*"), reverse=True)
    if not backups:
        click.echo("No backups found.")
        return
    latest = backups[0]
    if not yes:
        click.confirm(f"Rollback {config} to {latest}?", abort=True)
    import shutil
    shutil.copy2(latest, config)
    click.echo(f"✓ Rolled back to {latest}")
