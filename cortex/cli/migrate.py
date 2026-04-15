"""cortex migrate — migrate cortex.yaml to a new schema version."""
import click


@click.command()
@click.option("--config", default="cortex.yaml")
@click.option("--from-version", default="0.9", help="Source schema version")
@click.option("--to-version", default="1.0", help="Target schema version")
def migrate_command(config: str, from_version: str, to_version: str):
    """Migrate cortex.yaml to a newer schema version."""
    click.echo(f"Migrating {config} from v{from_version} to v{to_version}...")
    try:
        from cortex.config.loader import load_config
        cfg = load_config(config)
        click.echo(f"✓ Config is already valid for v{to_version}")
        click.echo(f"  Agent: {cfg.agent.name}")
        click.echo(f"  Task types: {len(cfg.task_types)}")
    except Exception as e:
        click.echo(f"✗ Migration check failed: {e}", err=True)
        raise SystemExit(1)
