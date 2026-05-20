"""Click entry point: cortex command."""
import click
from cortex.cli.dev_mode import dev_command
from cortex.cli.dry_run import dry_run_command
from cortex.cli.replay import replay_command
from cortex.cli.delta import delta_group
from cortex.cli.migrate import migrate_command
from cortex.cli.publish import publish_group
from cortex.cli.spec import spec_command
from cortex.cli.setup_wizard import setup_command
from cortex.cli.ants import ants_group
from cortex.cli.config_ui import config_ui_command
from cortex.cli.run import run_command, chat_command
from cortex.cli.sessions import sessions_group
from cortex.cli.blueprints import blueprints_group
from cortex.cli.mcps import mcps_group
from cortex.cli.stats import stats_command
from cortex.cli.config import config_group
from cortex.cli.providers import providers_group
from cortex.cli.storage import storage_group


@click.group()
@click.version_option(version="1.0.0", prog_name="cortex")
def cli():
    """Cortex Agent Framework CLI."""
    pass


# ── existing commands ─────────────────────────────────────────────────────────
cli.add_command(setup_command, name="setup")
cli.add_command(dev_command, name="dev")
cli.add_command(dry_run_command, name="dry-run")
cli.add_command(replay_command, name="replay")
cli.add_command(delta_group, name="delta")
cli.add_command(migrate_command, name="migrate")
cli.add_command(publish_group, name="publish")
cli.add_command(spec_command, name="spec")
cli.add_command(ants_group, name="ants")
cli.add_command(config_ui_command, name="config-ui")

# ── new commands ──────────────────────────────────────────────────────────────
cli.add_command(run_command, name="run")
cli.add_command(chat_command, name="chat")
cli.add_command(sessions_group, name="sessions")
cli.add_command(blueprints_group, name="blueprints")
cli.add_command(mcps_group, name="mcps")
cli.add_command(stats_command, name="stats")
cli.add_command(config_group, name="config")
cli.add_command(providers_group, name="providers")
cli.add_command(storage_group, name="storage")


if __name__ == "__main__":
    cli()
