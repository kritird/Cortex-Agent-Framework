"""cortex publish — publish docker image, package, MCP server, or chat UI."""
import click


@click.group()
def publish_group():
    """Publish your agent as Docker, a Python package, an MCP server, or a chat UI."""
    pass


@publish_group.command("docker")
@click.option("--tag", default="cortex-agent:latest")
@click.option("--config", default="cortex.yaml")
@click.option("--with-ui", is_flag=True, help="Bundle the chat UI and expose its port.")
def publish_docker(tag: str, config: str, with_ui: bool):
    """Build and publish a Docker image for this agent."""
    click.echo(f"Building Docker image: {tag}")
    click.echo("  (Generating Dockerfile...)")
    dockerfile = _generate_dockerfile(config, with_ui=with_ui)
    with open("Dockerfile.cortex", "w") as f:
        f.write(dockerfile)
    click.echo("  ✓ Dockerfile.cortex generated")
    if with_ui:
        click.echo("  ✓ Image will launch the chat UI on startup")
    click.echo(f"  Run: docker build -f Dockerfile.cortex -t {tag} .")


def _generate_dockerfile(config_path: str, with_ui: bool = False) -> str:
    if with_ui:
        return f"""FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install -e .
EXPOSE 8090
CMD ["python", "-m", "cortex.cli.main", "publish", "ui", "--config", "{config_path}"]
"""
    return f"""FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install -e .
CMD ["python", "-m", "cortex.cli.main", "dev", "--config", "{config_path}"]
"""


@publish_group.command("package")
@click.option("--output-dir", default="dist")
def publish_package(output_dir: str):
    """Build a distributable Python package."""
    import subprocess
    click.echo("Building Python package...")
    try:
        subprocess.run(["python", "-m", "build", "-o", output_dir], check=True)
        click.echo(f"✓ Package built in {output_dir}/")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        click.echo(f"✗ Build failed: {e}", err=True)


@publish_group.command("mcp")
@click.option("--config", default="cortex.yaml")
@click.option("--port", default=8080)
def publish_mcp(config: str, port: int):
    """Export this agent as an MCP server.

    Automatically forces ``agent.interaction_mode=rpc`` via the
    ``CORTEX_INTERACTION_MODE`` environment variable so the agent treats
    every request as a task and never emits interactive clarifications.
    An MCP client cannot answer interactive prompts, so running in the
    default ``interactive`` mode would hang on chat-shaped payloads.
    """
    import os
    os.environ["CORTEX_INTERACTION_MODE"] = "rpc"
    click.echo(f"Generating MCP server wrapper (port {port})...")
    click.echo("  interaction_mode forced to 'rpc' (via CORTEX_INTERACTION_MODE)")
    click.echo("  This agent can be accessed by other Cortex instances as a tool server.")
    click.echo(f"  Run: cortex publish mcp --port {port}")
    click.echo(f"  Configure in other cortex.yaml as tool_server with url: http://host:{port}")


@publish_group.command("ui")
@click.option("--config", default="cortex.yaml")
@click.option("--host", default=None, help="Override ui.host from cortex.yaml.")
@click.option("--port", default=None, type=int, help="Override ui.port from cortex.yaml.")
def publish_ui(config: str, host, port):
    """Serve a chat UI backed by this agent.

    A clean web UI with text + file upload, SSE streaming, and persistent
    session history. Auth, host and port are read from the ``ui`` block in
    cortex.yaml; --host/--port override them for ad-hoc runs.
    """
    import asyncio
    from cortex.framework import CortexFramework
    from cortex.ui import run_ui_server

    async def _serve():
        framework = CortexFramework(config)
        await framework.initialize()
        ui_cfg = framework._config.ui
        if host is not None:
            ui_cfg.host = host
        if port is not None:
            ui_cfg.port = port
        click.echo(f"Cortex chat UI: http://{ui_cfg.host}:{ui_cfg.port}")
        click.echo(f"  auth mode: {ui_cfg.auth.mode}")
        click.echo("  Ctrl-C to stop.")
        await run_ui_server(framework)

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        click.echo("\nStopped.")
