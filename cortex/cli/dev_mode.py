"""cortex dev — hot-reload development mode."""
import asyncio
import os
from pathlib import Path
import click


@click.command()
@click.option("--config", default="cortex.yaml", help="Path to cortex.yaml")
@click.option("--watch", is_flag=True, default=True, help="Watch for file changes")
def dev_command(config: str, watch: bool):
    """Start Cortex in development mode with hot-reload."""
    click.echo(f"Starting Cortex dev mode (config: {config})")
    asyncio.run(_run_dev(config, watch))


async def _run_dev(config_path: str, watch: bool):
    from cortex.config.loader import load_config
    try:
        cfg = load_config(config_path)
        click.echo(f"✓ Config loaded: agent '{cfg.agent.name}'")
        click.echo(f"  Task types: {len(cfg.task_types)}")
        click.echo(f"  Tool servers: {len(cfg.tool_servers)}")
    except Exception as e:
        click.echo(f"✗ Config error: {e}", err=True)
        return

    if watch:
        click.echo(f"Watching {config_path} for changes... (Ctrl+C to stop)")
        last_mtime = Path(config_path).stat().st_mtime if Path(config_path).exists() else 0
        try:
            while True:
                await asyncio.sleep(1)
                mtime = Path(config_path).stat().st_mtime if Path(config_path).exists() else 0
                if mtime != last_mtime:
                    last_mtime = mtime
                    try:
                        cfg = load_config(config_path)
                        click.echo(f"↺ Config reloaded: {len(cfg.task_types)} task types")
                    except Exception as e:
                        click.echo(f"✗ Reload error: {e}", err=True)
        except KeyboardInterrupt:
            click.echo("\nDev mode stopped.")
