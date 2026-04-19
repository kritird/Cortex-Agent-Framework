"""cortex ui — launch the monitoring dashboard."""
import webbrowser
import click


@click.command()
@click.option("--port", default=7800, help="Dashboard port (default: 7800)")
@click.option("--config", default="cortex.yaml")
def ui_command(port: int, config: str):
    """Launch the Cortex monitoring dashboard."""
    click.echo(f"Starting Cortex dashboard at http://localhost:{port} ...")
    webbrowser.open(f"http://localhost:{port}")
    click.echo("Dashboard UI requires the wizard server. Run: cortex setup")
