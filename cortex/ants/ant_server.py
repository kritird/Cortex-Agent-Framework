"""AntServer — minimal aiohttp MCP server that each ant process runs.

Exposes the ant's CortexFramework as MCP-compatible HTTP endpoints:
  GET  /health           → {"status": "ok", "name": <name>}
  GET  /capabilities     → {"capabilities": [...]}
  GET  /tools            → {"tools": [...]}
  POST /tools/{name}/invoke → runs a session, returns result

The server is started by AntColony._spawn_ant() as a subprocess using
the bootstrap script generated in the ant's working directory.
"""
import asyncio
import logging

logger = logging.getLogger(__name__)


async def run_ant_server(cortex_yaml_path: str, name: str, port: int, host: str = "127.0.0.1") -> None:
    """Start the ant MCP server. Called from the generated bootstrap script."""
    from aiohttp import web
    from cortex.framework import CortexFramework

    framework = CortexFramework(cortex_yaml_path)
    await framework.initialize()

    cfg = framework._config

    async def handle_health(request):
        return web.json_response({"status": "ok", "name": name, "agent": cfg.agent.name})

    async def handle_capabilities(request):
        caps = list({
            t.capability_hint
            for t in cfg.task_types
            if t.capability_hint and t.capability_hint != "auto"
        })
        return web.json_response({"capabilities": caps})

    async def handle_tools(request):
        tools = [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "request": {"type": "string", "description": "The task request"},
                        "user_id": {"type": "string", "description": "Optional user identifier"},
                    },
                    "required": ["request"],
                },
            }
            for t in cfg.task_types
        ]
        # Always expose a generic run_session tool
        tools.append({
            "name": "run_session",
            "description": f"Run a full session against the {cfg.agent.name} agent: {cfg.agent.description}",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "request": {"type": "string", "description": "The user request"},
                    "user_id": {"type": "string", "description": "Optional user identifier"},
                },
                "required": ["request"],
            },
        })
        return web.json_response({"tools": tools})

    async def handle_invoke(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        params = body.get("params", body)
        user_request = params.get("request", "")
        user_id = params.get("user_id", "ant_caller")

        if not user_request:
            return web.json_response({"error": "request param is required"}, status=400)

        queue = asyncio.Queue()
        try:
            result = await framework.run_session(
                user_id=user_id,
                request=user_request,
                event_queue=queue,
            )
            return web.json_response({"content": result.response, "status": "ok"})
        except Exception as exc:
            logger.error("Ant %s invoke error: %s", name, exc)
            return web.json_response({"error": str(exc)}, status=500)

    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/capabilities", handle_capabilities)
    app.router.add_get("/tools", handle_tools)
    app.router.add_post("/tools/{tool_name}/invoke", handle_invoke)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    logger.info("Ant '%s' MCP server running at http://%s:%d", name, host, port)
    print(f"__ANT_READY__ http://{host}:{port}", flush=True)

    # Run until killed
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


_BOOTSTRAP_TEMPLATE = '''\
import asyncio
import sys
import os

# Ensure the parent framework is importable from this subprocess
sys.path.insert(0, {framework_path!r})

async def main():
    from cortex.ants.ant_server import run_ant_server
    await run_ant_server(
        cortex_yaml_path={cortex_yaml_path!r},
        name={name!r},
        port={port!r},
        host={host!r},
    )

asyncio.run(main())
'''


def generate_bootstrap(
    framework_path: str,
    cortex_yaml_path: str,
    name: str,
    port: int,
    host: str = "127.0.0.1",
) -> str:
    return _BOOTSTRAP_TEMPLATE.format(
        framework_path=framework_path,
        cortex_yaml_path=cortex_yaml_path,
        name=name,
        port=port,
        host=host,
    )
