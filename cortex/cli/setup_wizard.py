"""cortex setup — launches browser Setup Studio at localhost:7799."""
import asyncio
import webbrowser
import click


@click.command()
@click.option("--port", default=7799, help="Port for Setup Studio (default: 7799)")
@click.option("--no-browser", is_flag=True, help="Don't auto-open browser")
def setup_command(port: int, no_browser: bool):
    """Launch the interactive Setup Studio in your browser."""
    click.echo(f"Starting Cortex Setup Studio at http://localhost:{port} ...")
    asyncio.run(_run_wizard(port, no_browser))


async def _run_wizard(port: int, no_browser: bool):
    from cortex.wizard.server import WizardServer
    server = WizardServer(port=port)
    url = await server.start()
    click.echo(f"Setup Studio running at {url}")
    if not no_browser:
        webbrowser.open(url)
    click.echo("Press Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        await server.stop()
        click.echo("\nSetup Studio stopped.")
