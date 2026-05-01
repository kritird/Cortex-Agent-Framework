"""cortex config-ui — launch the Cortex Config Studio."""
import asyncio
import webbrowser
from pathlib import Path

import click
from aiohttp import web


@click.command()
@click.option("--config", default="cortex.yaml", show_default=True,
              help="Path to cortex.yaml to load and edit.")
@click.option("--port", default=7801, show_default=True,
              help="Port for the Config Studio server.")
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Host to bind the server to.")
@click.option("--no-browser", is_flag=True, default=False,
              help="Start server without opening the browser.")
@click.option("--storage-base", default=None,
              help="Override storage.base_path for locating runtime files.")
def config_ui_command(config: str, port: int, host: str, no_browser: bool, storage_base: str):
    """Launch the Cortex Config Studio — browse and edit all framework configs."""
    config_path = Path(config).resolve()
    if not config_path.exists():
        click.echo(f"  ✗ Config file not found: {config}", err=True)
        raise click.Abort()

    resolved_storage_base = storage_base
    if not resolved_storage_base:
        try:
            import yaml
            with open(config_path, "r") as f:
                raw = yaml.safe_load(f) or {}
            resolved_storage_base = raw.get("storage", {}).get("base_path", "./cortex_data")
        except Exception:
            resolved_storage_base = "./cortex_data"

    url = f"http://{host}:{port}"

    click.echo("")
    click.echo("  ⬡  Cortex Config Studio")
    click.echo(f"     Config  : {config_path}")
    click.echo(f"     Storage : {resolved_storage_base}")
    click.echo(f"     URL     : {url}")
    click.echo("     Press Ctrl+C to stop.")
    click.echo("")

    asyncio.run(_serve(str(config_path), resolved_storage_base, host, port, url, no_browser))


async def _serve(config_path: str, storage_base: str, host: str, port: int, url: str, no_browser: bool):
    from cortex.config_ui.server import create_app

    app = create_app(config_path, storage_base)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    if not no_browser:
        webbrowser.open(url)

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runner.cleanup()
        click.echo("\n  Config Studio stopped.")
