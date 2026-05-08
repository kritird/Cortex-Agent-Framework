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
    import sys
    click.echo("Building Python package...")
    try:
        subprocess.run([sys.executable, "-m", "build", "-o", output_dir], check=True)
        click.echo(f"✓ Package built in {output_dir}/")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        click.echo(f"✗ Build failed: {e}", err=True)


@publish_group.command("mcp")
@click.option("--config", default="cortex.yaml")
@click.option("--port", default=8080, type=int)
def publish_mcp(config: str, port: int):
    """Export this agent as an MCP server.

    Serves the agent as an MCP-over-SSE endpoint at /mcp so other Cortex
    agents (or any MCP client) can call its capabilities as tools.

    Automatically forces ``agent.interaction_mode=rpc`` so the agent never
    emits interactive clarifications — MCP clients cannot answer them.
    """
    import asyncio
    import os
    os.environ["CORTEX_INTERACTION_MODE"] = "rpc"

    async def _serve():
        from cortex.framework import CortexFramework
        from aiohttp import web

        framework = CortexFramework(config)
        await framework.initialize()

        async def handle_mcp(request: web.Request) -> web.Response:
            """Minimal MCP-over-HTTP handler: accepts {input} and returns {output}."""
            try:
                body = await request.json()
            except Exception:
                return web.json_response({"error": "invalid json"}, status=400)

            user_input = body.get("input") or body.get("request") or ""
            if not user_input:
                return web.json_response({"error": "missing 'input' field"}, status=400)

            import asyncio as _asyncio
            queue: _asyncio.Queue = _asyncio.Queue()
            result_text = ""
            try:
                session_result = await framework.run_session(
                    user_id="mcp_caller",
                    request=user_input,
                    event_queue=queue,
                )
                result_text = session_result.response or ""
            except Exception as exc:
                return web.json_response({"error": str(exc)}, status=500)

            return web.json_response({"output": result_text})

        app = web.Application()
        app.router.add_post("/mcp", handle_mcp)
        app.router.add_post("/run", handle_mcp)  # convenience alias

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        click.echo(f"MCP server running at http://localhost:{port}/mcp")
        click.echo("  interaction_mode: rpc")
        click.echo("  Ctrl-C to stop.")
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await runner.cleanup()

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        click.echo("\nStopped.")


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
        browser_host = "localhost" if ui_cfg.host in ("0.0.0.0", "") else ui_cfg.host
        click.echo(f"Cortex chat UI: http://{browser_host}:{ui_cfg.port}")
        click.echo(f"  auth mode: {ui_cfg.auth.mode}")
        click.echo("  Ctrl-C to stop.")
        await run_ui_server(framework)

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        click.echo("\nStopped.")
