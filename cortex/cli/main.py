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


@click.group()
@click.version_option(version="1.0.0", prog_name="cortex")
def cli():
    """Cortex Agent Framework CLI."""
    pass


cli.add_command(setup_command, name="setup")
cli.add_command(dev_command, name="dev")
cli.add_command(dry_run_command, name="dry-run")
cli.add_command(replay_command, name="replay")
cli.add_command(delta_group, name="delta")
cli.add_command(migrate_command, name="migrate")
cli.add_command(publish_group, name="publish")
cli.add_command(spec_command, name="spec")


if __name__ == "__main__":
    cli()
