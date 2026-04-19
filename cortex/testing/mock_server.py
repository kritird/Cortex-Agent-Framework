"""MockToolServer — lightweight HTTP server for testing tool server integrations."""
import asyncio
from typing import Callable, Dict, Optional
from aiohttp import web


class MockToolServer:
    """
    Lightweight HTTP mock of an MCP tool server for testing.
    Register tools with callbacks, run in tests.

    Usage:
        server = MockToolServer()
        server.register_tool("search", lambda params: {"results": ["item1"]})

        async with server.run() as url:
            # url is e.g. "http://localhost:8765"
            # run your test
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self._host = host
        self._port = port
        self._tools: Dict[str, Callable] = {}
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self.call_log: list = []

    def register_tool(self, name: str, handler: Callable) -> None:
        """Register a tool handler. handler(params: dict) -> Any"""
        self._tools[name] = handler

    def _build_app(self) -> web.Application:
        app = web.Application()

        async def handle_tools(request):
            return web.json_response([
                {"name": name, "description": f"Mock tool: {name}", "inputSchema": {}}
                for name in self._tools
            ])

        async def handle_invoke(request):
            tool_name = request.match_info["tool_name"]
            try:
                body = await request.json()
            except Exception:
                body = {}
            params = body.get("params", body)
            self.call_log.append({"tool": tool_name, "params": params})
            handler = self._tools.get(tool_name)
            if handler is None:
                return web.json_response({"error": f"Tool '{tool_name}' not found"}, status=404)
            try:
                if asyncio.iscoroutinefunction(handler):
                    result = await handler(params)
                else:
                    result = handler(params)
                return web.json_response({"content": result})
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)

        async def handle_ping(request):
            return web.json_response({"status": "ok"})

        app.router.add_get("/tools", handle_tools)
        app.router.add_get("/ping", handle_ping)
        app.router.add_post("/tools/{tool_name}/invoke", handle_invoke)
        return app

    async def start(self) -> str:
        """Start the mock server. Returns the base URL."""
        self._app = self._build_app()
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        # Get actual port
        port = self._site._server.sockets[0].getsockname()[1]
        self._port = port
        return f"http://{self._host}:{port}"

    async def stop(self) -> None:
        """Stop the mock server."""
        if self._runner:
            await self._runner.cleanup()

    class _RunContextManager:
        def __init__(self, server):
            self._server = server
            self._url = None

        async def __aenter__(self) -> str:
            self._url = await self._server.start()
            return self._url

        async def __aexit__(self, *args):
            await self._server.stop()

    def run(self):
        """Async context manager: async with server.run() as url."""
        return self._RunContextManager(self)

    def reset_call_log(self) -> None:
        self.call_log.clear()
