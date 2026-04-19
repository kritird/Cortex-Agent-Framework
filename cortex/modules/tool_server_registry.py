"""ToolServerRegistry — manages all tool server connections."""
import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp

from cortex.config.schema import ToolServerConfig, UserConfig
from cortex.exceptions import CortexToolUnavailableError

logger = logging.getLogger(__name__)

# ── Write-semantic keyword filter for external (auto-discovered) MCPs ─────────
# Tools whose name or description contains any of these words — as a whole word,
# case-insensitive — are stripped from external servers before registration.
# Schema-level check (presence of both a "content" param and a "path"/"filename"
# param in the tool's inputSchema) is also applied as a secondary signal.
_WRITE_KEYWORDS: frozenset = frozenset({
    "write", "create", "delete", "remove", "update", "modify", "edit",
    "upload", "insert", "append", "overwrite", "execute", "run", "exec",
    "deploy", "publish", "send", "submit", "destroy", "drop", "truncate",
    "reset", "format", "install", "save", "store", "persist", "commit",
    "push", "patch", "put", "post",
})
_WRITE_KEYWORD_RE = re.compile(
    r"(?:^|[^A-Za-z])(" + "|".join(re.escape(w) for w in _WRITE_KEYWORDS) + r")(?:[^A-Za-z]|$)",
    re.IGNORECASE,
)

# Capability classification patterns
CAPABILITY_PATTERNS = {
    "web_search": re.compile(r"search|fetch|lookup|query|browse|crawl|scrape", re.I),
    "document_generation": re.compile(r"generat|creat|produc|docx|pdf|doc|report|write|draft", re.I),
    "image_generation": re.compile(r"image|draw|render|png|jpeg|jpg|svg|picture|photo|visual|art", re.I),
    "bash": re.compile(r"bash|exec|run|process|transform|file|compute|convert|shell", re.I),
}


@dataclass
class ToolInfo:
    name: str
    description: str
    input_schema: dict = field(default_factory=dict)
    capability: str = "llm_synthesis"


@dataclass
class ToolServerInfo:
    name: str
    url: Optional[str]
    transport: str
    status: str  # READY | UNAVAILABLE | READY_WITH_WARNINGS
    tools: List[ToolInfo] = field(default_factory=list)
    capabilities: List[str] = field(default_factory=list)
    auth_status: str = "unknown"
    tls_status: str = "n/a"
    pool_status: str = "ok"
    health_check_result: str = "pending"
    scope: str = "permanent"  # permanent | session:{session_id}
    classification_notes: List[str] = field(default_factory=list)
    failure_count: int = 0
    recovery_count: int = 0
    session_id: Optional[str] = None
    # "internal" for user-configured servers; "external" for auto-discovered ones.
    # External servers have write-tools filtered out and all outputs pass through
    # MCPOutputGuard before being returned to the agent.
    trust_tier: str = "internal"


@dataclass
class ToolServerConnection:
    server_name: str
    server_info: ToolServerInfo
    session: Optional[aiohttp.ClientSession] = None


@dataclass
class StartupReport:
    servers: List[ToolServerInfo]
    all_ready: bool
    warnings: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        lines = ["=== Tool Server Startup Report ==="]
        for srv in self.servers:
            status_icon = "✓" if srv.status.startswith("READY") else "✗"
            lines.append(f"\n  {status_icon} {srv.name} [{srv.transport}] — {srv.status}")
            if srv.url:
                lines.append(f"    URL: {srv.url}")
            lines.append(f"    Auth: {srv.auth_status} | TLS: {srv.tls_status} | Pool: {srv.pool_status}")
            lines.append(f"    Tools: {', '.join(t.name for t in srv.tools) or 'none'}")
            lines.append(f"    Capabilities: {', '.join(srv.capabilities) or 'none'}")
            for note in srv.classification_notes:
                lines.append(f"    ⚠ {note}")
        if self.warnings:
            lines.append("\nWarnings:")
            for w in self.warnings:
                lines.append(f"  - {w}")
        return "\n".join(lines)


class ToolServerRegistry:
    """
    Manages all tool server connections from startup through session lifecycle.
    When eager_discovery=True, all verification and discovery happens in initialize_all().
    When eager_discovery=False (default), servers are registered from config hints only
    and probed on first use.
    """

    def __init__(self, user_config: Optional[UserConfig] = None):
        self._servers: Dict[str, ToolServerInfo] = {}
        self._connections: Dict[str, ToolServerConnection] = {}
        self._capability_map: Dict[str, List[str]] = {}  # {capability: [server_names]}
        self._user_config = user_config or UserConfig()
        self._health_task: Optional[asyncio.Task] = None
        self._discovery_task: Optional[asyncio.Task] = None
        self._configs: Dict[str, ToolServerConfig] = {}
        self._discovery_timeout: int = 15
        self._verify_auth: bool = True
        self._registry_path: Optional[str] = None

    async def initialize_all(
        self,
        config: Dict[str, ToolServerConfig],
        require_all: bool = False,
        discovery_timeout: int = 15,
        verify_auth: bool = True,
        log_tools: bool = True,
        eager_discovery: bool = True,
        registry_path: Optional[str] = None,
    ) -> StartupReport:
        """Initialize all declared tool servers.

        When eager_discovery=False, servers are registered from capability_hints
        only (no network calls) and will be probed on first use.  If a
        capability registry file exists at registry_path, cached capabilities
        from previous runs are applied immediately so routing works before the
        background discovery completes.
        """
        self._configs = config
        self._discovery_timeout = discovery_timeout
        self._verify_auth = verify_auth
        if registry_path:
            self._registry_path = registry_path
        server_infos = []
        warnings = []

        if not eager_discovery:
            for name, srv_config in config.items():
                caps = list(srv_config.discovery.capability_hints) or ["llm_synthesis"]
                info = ToolServerInfo(
                    name=name,
                    url=srv_config.url,
                    transport=srv_config.transport,
                    status="UNVERIFIED",
                    capabilities=caps,
                )
                self._servers[name] = info
                server_infos.append(info)
                for cap in caps:
                    self._capability_map.setdefault(cap, [])
                    if name not in self._capability_map[cap]:
                        self._capability_map[cap].append(name)

            # Overlay richer capabilities from the persisted registry (no network)
            if registry_path:
                self._load_capability_registry(registry_path)

            if log_tools:
                for info in server_infos:
                    logger.info(
                        "Server '%s': UNVERIFIED (eager_discovery=false) | capabilities=%s",
                        info.name, info.capabilities,
                    )
            return StartupReport(
                servers=server_infos,
                all_ready=True,
                warnings=["eager_discovery=false: servers will be probed on first use"],
            )

        for name, srv_config in config.items():
            info = await self._init_server(
                name=name,
                srv_config=srv_config,
                verify_auth=verify_auth,
                discovery_timeout=discovery_timeout,
            )
            self._servers[name] = info
            server_infos.append(info)

            if info.status == "UNAVAILABLE":
                msg = f"Tool server '{name}' is UNAVAILABLE"
                if require_all:
                    raise CortexToolUnavailableError(
                        f"Required tool server '{name}' failed to initialize",
                        server_name=name,
                    )
                warnings.append(msg)
                logger.warning(msg)

            # Update capability map
            for cap in info.capabilities:
                if cap not in self._capability_map:
                    self._capability_map[cap] = []
                if name not in self._capability_map[cap]:
                    self._capability_map[cap].append(name)

            if log_tools:
                logger.info(
                    "Server '%s': %s | tools=%s | capabilities=%s",
                    name, info.status,
                    [t.name for t in info.tools],
                    info.capabilities,
                )

        all_ready = all(s.status != "UNAVAILABLE" for s in server_infos)
        report = StartupReport(servers=server_infos, all_ready=all_ready, warnings=warnings)
        if log_tools:
            logger.info("\n%s", str(report))
        return report

    async def ensure_server_ready(self, name: str) -> bool:
        """Probe and initialize a server that was registered lazily (UNVERIFIED).

        No-ops if the server is already READY or UNAVAILABLE.
        Returns True if the server is READY after this call.
        """
        info = self._servers.get(name)
        if not info:
            return False
        if info.status != "UNVERIFIED":
            return info.status.startswith("READY")
        config = self._configs.get(name)
        if not config:
            return False
        logger.info("Lazy-initializing server '%s' on first use", name)
        updated = await self._init_server(
            name=name,
            srv_config=config,
            verify_auth=self._verify_auth,
            discovery_timeout=self._discovery_timeout,
        )
        self._servers[name] = updated
        # Refresh capability map entry
        for cap, servers in self._capability_map.items():
            if name in servers and updated.status == "UNAVAILABLE":
                servers.remove(name)
        for cap in updated.capabilities:
            self._capability_map.setdefault(cap, [])
            if name not in self._capability_map[cap]:
                self._capability_map[cap].append(name)
        return updated.status.startswith("READY")

    async def _init_server(
        self,
        name: str,
        srv_config: ToolServerConfig,
        verify_auth: bool = True,
        discovery_timeout: int = 15,
    ) -> ToolServerInfo:
        """Initialize a single tool server: connect, verify, discover."""
        info = ToolServerInfo(
            name=name,
            url=srv_config.url,
            transport=srv_config.transport,
            status="UNAVAILABLE",
        )

        if srv_config.transport == "stdio":
            info.status = "READY"
            info.auth_status = "n/a"
            info.tls_status = "n/a"
            # Discover tools via stdio if command specified
            if srv_config.command:
                try:
                    tools = await asyncio.wait_for(
                        self._discover_stdio_tools(srv_config),
                        timeout=discovery_timeout,
                    )
                    info.tools = tools
                    info.capabilities = self._classify_tools(tools, srv_config, info)
                except asyncio.TimeoutError:
                    info.classification_notes.append("Tool discovery timed out")
                except Exception as e:
                    info.classification_notes.append(f"Tool discovery failed: {e}")
            return info

        if not srv_config.url:
            info.classification_notes.append("No URL configured")
            return info

        # Build aiohttp session
        try:
            timeout = aiohttp.ClientTimeout(
                total=srv_config.connection.timeout_seconds,
                sock_read=srv_config.connection.read_timeout_seconds,
            )
            connector_kwargs = {}
            if srv_config.tls.enabled:
                import ssl
                ssl_ctx = ssl.create_default_context()
                if not srv_config.tls.verify_cert:
                    ssl_ctx.check_hostname = False
                    ssl_ctx.verify_mode = ssl.CERT_NONE
                if srv_config.tls.ca_cert_file:
                    ssl_ctx.load_verify_locations(srv_config.tls.ca_cert_file)
                if srv_config.tls.client_cert_file and srv_config.tls.client_key_file:
                    ssl_ctx.load_cert_chain(srv_config.tls.client_cert_file, srv_config.tls.client_key_file)
                connector_kwargs["ssl"] = ssl_ctx
                info.tls_status = "ok"
            else:
                info.tls_status = "disabled"

            headers = dict(srv_config.headers)
            # Apply auth
            auth_token = self._resolve_auth(srv_config, headers)
            if auth_token:
                info.auth_status = "ok"
            elif srv_config.auth.type == "none":
                info.auth_status = "none"
            else:
                info.auth_status = "missing"
                if verify_auth:
                    info.classification_notes.append(
                        f"Auth type '{srv_config.auth.type}' configured but credentials not found"
                    )

            session = aiohttp.ClientSession(
                timeout=timeout,
                headers=headers,
                connector=aiohttp.TCPConnector(**connector_kwargs) if connector_kwargs else None,
            )
            self._connections[name] = ToolServerConnection(
                server_name=name,
                server_info=info,
                session=session,
            )

            # Health check
            health_ok = await self._health_check(name, srv_config, session)
            if health_ok:
                info.health_check_result = "ok"
                info.status = "READY"
            else:
                info.health_check_result = "failed"
                info.status = "UNAVAILABLE"
                return info

            # Tool discovery
            try:
                tools = await asyncio.wait_for(
                    self._discover_http_tools(name, srv_config, session),
                    timeout=discovery_timeout,
                )
                info.tools = tools
                info.capabilities = self._classify_tools(tools, srv_config, info)
            except asyncio.TimeoutError:
                info.classification_notes.append("Tool discovery timed out")
            except Exception as e:
                info.classification_notes.append(f"Tool discovery failed: {e}")

        except Exception as e:
            info.classification_notes.append(f"Connection failed: {e}")
            info.status = "UNAVAILABLE"

        return info

    def _resolve_auth(self, config: ToolServerConfig, headers: dict) -> bool:
        """Apply auth to headers dict. Returns True if auth was applied."""
        import os
        auth = config.auth
        if auth.type == "bearer":
            token = os.environ.get(auth.token_env_var or "", "")
            if token:
                header = auth.header or "Authorization"
                headers[header] = f"Bearer {token}"
                return True
        elif auth.type == "api_key":
            key = os.environ.get(auth.key_env_var or "", "")
            if key:
                header = auth.header or "X-Api-Key"
                headers[header] = key
                return True
        elif auth.type == "basic":
            import base64
            username = os.environ.get(auth.username_env_var or "", "")
            password = os.environ.get(auth.password_env_var or "", "")
            if username and password:
                creds = base64.b64encode(f"{username}:{password}".encode()).decode()
                headers["Authorization"] = f"Basic {creds}"
                return True
        return False

    async def _health_check(
        self,
        name: str,
        config: ToolServerConfig,
        session: aiohttp.ClientSession,
    ) -> bool:
        """Run health check against configured endpoint or MCP ping."""
        hc = config.health_check
        if not hc.enabled:
            return True
        endpoint = hc.endpoint or (f"{config.url}/ping" if config.url else None)
        if not endpoint:
            return True
        try:
            async with session.get(endpoint) as resp:
                return resp.status < 500
        except Exception as e:
            logger.debug("Health check failed for '%s': %s", name, e)
            return False

    async def _discover_http_tools(
        self,
        name: str,
        config: ToolServerConfig,
        session: aiohttp.ClientSession,
    ) -> List[ToolInfo]:
        """Discover tools via MCP introspection endpoint."""
        if not config.url:
            return []
        try:
            # Try MCP /tools endpoint
            async with session.get(f"{config.url}/tools") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    tools_raw = data if isinstance(data, list) else data.get("tools", [])
                    return [
                        ToolInfo(
                            name=t.get("name", ""),
                            description=t.get("description", ""),
                            input_schema=t.get("inputSchema", {}),
                        )
                        for t in tools_raw if t.get("name")
                    ]
        except Exception:
            pass
        return []

    async def _discover_stdio_tools(self, config: ToolServerConfig) -> List[ToolInfo]:
        """Discover tools from a stdio MCP server (simplified)."""
        return []

    def _classify_tools(
        self,
        tools: List[ToolInfo],
        config: ToolServerConfig,
        info: ToolServerInfo,
    ) -> List[str]:
        """Classify capabilities from tool names + docstrings + discovery hints."""
        capabilities = set()

        # Use explicit hints first
        for hint in config.discovery.capability_hints:
            capabilities.add(hint)

        for tool in tools:
            text = f"{tool.name} {tool.description}".lower()
            matched = False
            for cap, pattern in CAPABILITY_PATTERNS.items():
                if pattern.search(text):
                    capabilities.add(cap)
                    tool.capability = cap
                    matched = True
            if not matched:
                tool.capability = "llm_synthesis"
                if not config.discovery.capability_hints:
                    info.classification_notes.append(
                        f"AMBIGUOUS — '{tool.name}' did not match any known capability pattern. "
                        f"Defaulting to: llm_synthesis. "
                        f"To override: add capability_hints to this server in cortex.yaml."
                    )

        return list(capabilities) if capabilities else ["llm_synthesis"]

    async def start_health_check_loop(self) -> None:
        """Start background health check task."""
        self._health_task = asyncio.create_task(self._health_check_loop())

    async def _health_check_loop(self) -> None:
        """Background health monitoring loop."""
        while True:
            try:
                for name, info in list(self._servers.items()):
                    config = self._configs.get(name)
                    if not config or not config.health_check.enabled:
                        continue
                    await asyncio.sleep(config.health_check.interval_seconds)
                    conn = self._connections.get(name)
                    if not conn or not conn.session:
                        continue
                    ok = await self._health_check(name, config, conn.session)
                    if ok:
                        info.failure_count = 0
                        if info.status == "UNAVAILABLE":
                            info.recovery_count += 1
                            if info.recovery_count >= config.health_check.recovery_threshold:
                                logger.info("Server '%s' recovered — re-running discovery", name)
                                tools = await self._discover_http_tools(name, config, conn.session)
                                info.tools = tools
                                info.capabilities = self._classify_tools(tools, config, info)
                                info.status = "READY"
                                info.recovery_count = 0
                                # Update capability map
                                for cap in info.capabilities:
                                    self._capability_map.setdefault(cap, [])
                                    if name not in self._capability_map[cap]:
                                        self._capability_map[cap].append(name)
                    else:
                        info.failure_count += 1
                        info.recovery_count = 0
                        if info.failure_count >= config.health_check.failure_threshold:
                            if info.status != "UNAVAILABLE":
                                logger.warning("Server '%s' marked UNAVAILABLE after %d failures", name, info.failure_count)
                                info.status = "UNAVAILABLE"
                                for cap, servers in self._capability_map.items():
                                    if name in servers:
                                        servers.remove(name)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Health check loop error: %s", e)
                await asyncio.sleep(5)

    async def stop_health_check_loop(self) -> None:
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

    def emit_session_start_event(self) -> str:
        available = [cap for cap, servers in self._capability_map.items() if servers]
        unavailable = []
        for name, info in self._servers.items():
            if info.status == "UNAVAILABLE":
                unavailable.extend(info.capabilities)
        unavailable = list(set(unavailable))
        cap_str = ", ".join(available) if available else "none"
        msg = f"Starting your session. Available capabilities: {cap_str}."
        if unavailable:
            unavail_str = ", ".join(unavailable)
            msg += f" Note: {unavail_str} is currently unavailable."
        return msg

    async def register_tool_server(
        self,
        name: str,
        url: str,
        auth: Dict,
        tls: Dict = None,
        pool: Dict = None,
        session_id: Optional[str] = None,
    ) -> ToolServerInfo:
        """Runtime registration of a tool server."""
        if not self._user_config.allow_user_tool_servers and session_id:
            raise CortexToolUnavailableError(
                "User-attached tool servers are not allowed. Set user_config.allow_user_tool_servers: true",
                server_name=name,
            )
        from cortex.config.schema import (
            ToolServerConfig, ToolServerAuthConfig, ToolServerTLSConfig,
            ToolServerPoolConfig
        )
        srv_config = ToolServerConfig(
            url=url,
            name=name,
            transport="sse",
            auth=ToolServerAuthConfig(**(auth or {})),
            tls=ToolServerTLSConfig(**(tls or {})),
            pool=ToolServerPoolConfig(**(pool or {})),
        )
        info = await self._init_server(name, srv_config)
        if session_id:
            info.scope = f"session:{session_id}"
            info.session_id = session_id
        self._servers[name] = info
        self._configs[name] = srv_config
        for cap in info.capabilities:
            self._capability_map.setdefault(cap, [])
            if name not in self._capability_map[cap]:
                self._capability_map[cap].append(name)
        # Persist updated capabilities so next startup is pre-warmed
        await self._save_capability_registry()
        return info

    async def register_external_server(
        self,
        name: str,
        url: str,
        capabilities: List[str],
    ) -> ToolServerInfo:
        """Register an auto-discovered external MCP server with lower privilege.

        Differences from :meth:`register_tool_server`:
        - ``trust_tier`` is set to ``"external"``
        - No auth is applied (external MCPs that need auth are rejected upstream)
        - Write-semantic tools are stripped before the server goes live
        - All tool outputs pass through :meth:`apply_output_guard` at call time
          (enforced in :class:`GenericMCPAgent.call_tool_server`)
        """
        from cortex.config.schema import (
            ToolServerConfig, ToolServerAuthConfig, ToolServerDiscoveryConfig,
        )
        srv_config = ToolServerConfig(
            url=url,
            name=name,
            transport="sse",
            auth=ToolServerAuthConfig(type="none"),
            discovery=ToolServerDiscoveryConfig(
                auto=True,
                capability_hints=capabilities,
            ),
        )
        info = await self._init_server(name, srv_config)
        info.trust_tier = "external"
        info.scope = "permanent"

        # Strip write-semantic tools so the agent can never mutate state
        # via an auto-discovered server.
        original_count = len(info.tools)
        info.tools = self._filter_write_tools(info.tools)
        removed = original_count - len(info.tools)
        if removed:
            logger.info(
                "ExternalMCP '%s': stripped %d write-semantic tool(s) — %d read-only tool(s) remain",
                name, removed, len(info.tools),
            )
        if not info.tools and original_count:
            logger.warning(
                "ExternalMCP '%s': all tools were write-semantic — server not registered",
                name,
            )
            info.status = "UNAVAILABLE"
            info.classification_notes.append("All tools filtered as write-semantic")
            return info

        # Override capability classification with the verified hints so the scout
        # can find this server without re-probing.
        if capabilities:
            info.capabilities = capabilities

        self._servers[name] = info
        self._configs[name] = srv_config
        for cap in info.capabilities:
            self._capability_map.setdefault(cap, [])
            if name not in self._capability_map[cap]:
                self._capability_map[cap].append(name)
        await self._save_capability_registry()
        logger.info(
            "ExternalMCP '%s' registered (trust_tier=external, tools=%s, caps=%s)",
            name, [t.name for t in info.tools], info.capabilities,
        )
        return info

    async def register_ant_server(
        self,
        name: str,
        url: str,
        capability: str,
    ) -> ToolServerInfo:
        """Register a self-spawned ant agent MCP server.

        Differences from :meth:`register_tool_server` (internal):
        - ``trust_tier`` is set to ``"ant"``
        - No auth required (same process namespace, localhost only)
        - Write tools are NOT stripped (ant is a trusted Cortex agent)
        - Output guard is NOT applied (same as internal)

        Differences from :meth:`register_external_server`:
        - Write tools are allowed
        - No output guard
        """
        from cortex.config.schema import (
            ToolServerConfig, ToolServerAuthConfig, ToolServerDiscoveryConfig,
        )
        srv_config = ToolServerConfig(
            url=url,
            name=name,
            transport="sse",
            auth=ToolServerAuthConfig(type="none"),
            discovery=ToolServerDiscoveryConfig(
                auto=True,
                capability_hints=[capability],
            ),
        )
        info = await self._init_server(name, srv_config)
        info.trust_tier = "ant"
        info.scope = "permanent"

        if capability:
            info.capabilities = list({capability, *info.capabilities})

        self._servers[name] = info
        self._configs[name] = srv_config
        for cap in info.capabilities:
            self._capability_map.setdefault(cap, [])
            if name not in self._capability_map[cap]:
                self._capability_map[cap].append(name)
        await self._save_capability_registry()
        logger.info(
            "AntMCP '%s' registered (trust_tier=ant, url=%s, caps=%s)",
            name, url, info.capabilities,
        )
        return info

    @staticmethod
    def _filter_write_tools(tools: List[ToolInfo]) -> List[ToolInfo]:
        """Return only tools that have no write-semantic indicators.

        Two signals are checked:
        1. Keyword match — tool name or description contains a write-semantic word.
        2. Schema signal — inputSchema contains both a "content"-like param and a
           "path"/"filename"-like param simultaneously (suggests file-write operation).
        """
        safe = []
        for tool in tools:
            # Normalize camelCase → camel Case so the word-boundary regex catches it
            normalized_name = re.sub(r"([a-z])([A-Z])", r"\1 \2", tool.name)
            text = f"{normalized_name} {tool.description}"
            if _WRITE_KEYWORD_RE.search(text):
                logger.debug(
                    "ExternalMCP: filtering write-semantic tool '%s' (keyword match)",
                    tool.name,
                )
                continue
            # Schema-level check
            if ToolServerRegistry._schema_suggests_write(tool.input_schema):
                logger.debug(
                    "ExternalMCP: filtering write-semantic tool '%s' (schema signal)",
                    tool.name,
                )
                continue
            safe.append(tool)
        return safe

    @staticmethod
    def _schema_suggests_write(schema: dict) -> bool:
        """Heuristic: True if inputSchema has both content-like and path-like params."""
        if not schema:
            return False
        props = schema.get("properties", {})
        if not props:
            return False
        param_names = {k.lower() for k in props}
        has_content = any(
            w in param_names for w in ("content", "body", "data", "text", "payload")
        )
        has_path = any(
            w in param_names for w in ("path", "filename", "file", "filepath", "destination")
        )
        return has_content and has_path

    def apply_output_guard(
        self,
        server_name: str,
        content: str,
        content_type: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> str:
        """Apply MCPOutputGuard to *content* if *server_name* is an external server.

        Returns the (possibly sanitised) content unchanged for internal servers.
        Raises :exc:`MCPOutputSecurityError` if the content fails the safety check.
        This method is called by :class:`GenericMCPAgent` after every external
        tool-server response.
        """
        info = self._servers.get(server_name)
        if not info or info.trust_tier != "external":
            return content
        from cortex.security.mcp_output_guard import MCPOutputGuard
        guard = MCPOutputGuard()
        return guard.check(content, content_type=content_type, filename=filename)

    async def deregister_tool_server(self, name: str) -> None:
        """Remove server, close connections."""
        conn = self._connections.pop(name, None)
        if conn and conn.session:
            await conn.session.close()
        info = self._servers.pop(name, None)
        if info:
            for cap, servers in self._capability_map.items():
                if name in servers:
                    servers.remove(name)
        self._configs.pop(name, None)
        logger.info("Deregistered tool server: %s", name)

    # ── Capability Registry (persistence) ─────────────────────────────────────

    def _load_capability_registry(self, path: str) -> None:
        """Overlay cached capabilities from a prior run's registry file.

        Only updates servers that are currently registered and UNVERIFIED.
        Replaces default/hint-based capabilities with the richer discovered set.
        """
        registry_file = Path(path)
        if not registry_file.exists():
            return
        try:
            data = json.loads(registry_file.read_text())
        except Exception as e:
            logger.warning("Failed to load capability registry from %s: %s", path, e)
            return

        updated = 0
        for name, entry in data.items():
            info = self._servers.get(name)
            if not info or info.status != "UNVERIFIED":
                continue
            cached_caps = entry.get("capabilities", [])
            if not cached_caps:
                continue
            # Remove old capability_map entries for this server
            for servers in self._capability_map.values():
                if name in servers:
                    servers.remove(name)
            # Apply cached capabilities
            info.capabilities = cached_caps
            for cap in cached_caps:
                self._capability_map.setdefault(cap, [])
                if name not in self._capability_map[cap]:
                    self._capability_map[cap].append(name)
            updated += 1

        logger.info(
            "Capability registry loaded from %s — updated %d/%d servers",
            path, updated, len(data),
        )

    async def _save_capability_registry(self) -> None:
        """Persist capabilities for all currently READY servers to the registry file."""
        if not self._registry_path:
            return
        data = {}
        for name, info in self._servers.items():
            if info.status.startswith("READY") and info.capabilities:
                data[name] = {
                    "capabilities": info.capabilities,
                    "tools": [
                        {"name": t.name, "description": t.description}
                        for t in info.tools
                    ],
                    "last_seen": datetime.utcnow().isoformat(),
                }
        try:
            Path(self._registry_path).parent.mkdir(parents=True, exist_ok=True)
            Path(self._registry_path).write_text(json.dumps(data, indent=2))
            logger.info("Capability registry saved to %s (%d servers)", self._registry_path, len(data))
        except Exception as e:
            logger.warning("Failed to save capability registry: %s", e)

    # ── Background Discovery ───────────────────────────────────────────────────

    async def start_background_discovery(
        self,
        registry_path: Optional[str] = None,
        concurrency: int = 10,
    ) -> None:
        """Probe all UNVERIFIED servers concurrently in the background.

        Called after initialize_all(eager_discovery=False) so the main startup
        path returns instantly while discovery happens behind the scenes.
        Saves results to the registry file so the next startup is pre-warmed.
        """
        if registry_path:
            self._registry_path = registry_path
        self._discovery_task = asyncio.create_task(
            self._background_discover_all(concurrency)
        )

    async def _background_discover_all(self, concurrency: int) -> None:
        unverified = [n for n, i in self._servers.items() if i.status == "UNVERIFIED"]
        if not unverified:
            return
        logger.info(
            "Background discovery: probing %d servers (concurrency=%d)",
            len(unverified), concurrency,
        )
        sem = asyncio.Semaphore(concurrency)

        async def _probe(name: str) -> None:
            async with sem:
                try:
                    await self.ensure_server_ready(name)
                except Exception as e:
                    logger.debug("Background probe failed for '%s': %s", name, e)

        await asyncio.gather(*[_probe(n) for n in unverified], return_exceptions=True)
        await self._save_capability_registry()
        ready = sum(1 for i in self._servers.values() if i.status.startswith("READY"))
        logger.info("Background discovery complete: %d/%d servers READY", ready, len(unverified))

    async def stop_background_discovery(self) -> None:
        if self._discovery_task and not self._discovery_task.done():
            self._discovery_task.cancel()
            try:
                await self._discovery_task
            except asyncio.CancelledError:
                pass

    # ── Capability Lookup ──────────────────────────────────────────────────────

    async def get_capability_servers(self, capability: str) -> List[ToolServerConnection]:
        """Return available connections matching a capability hint.

        First returns any already-READY servers without touching UNVERIFIED ones.
        Only probes UNVERIFIED servers (one at a time) when no READY server exists
        for the capability — preventing a thundering-herd probe of all 100 servers
        on the first request.
        """
        server_names = list(self._capability_map.get(capability, []))

        # Fast path: return already-READY connections without any probing
        ready = []
        for name in server_names:
            info = self._servers.get(name)
            if info and info.status.startswith("READY"):
                conn = self._connections.get(name)
                if conn:
                    ready.append(conn)
        if ready:
            return ready

        # Slow path: probe UNVERIFIED servers one at a time until one succeeds
        for name in server_names:
            info = self._servers.get(name)
            if info and info.status == "UNVERIFIED":
                await self.ensure_server_ready(name)
                info = self._servers.get(name)
                if info and info.status.startswith("READY"):
                    conn = self._connections.get(name)
                    if conn:
                        return [conn]

        return []

    def classify_capability(self, tool_name: str, docstring: str) -> str:
        """Auto-classify from tool name and docstring."""
        text = f"{tool_name} {docstring}".lower()
        for cap, pattern in CAPABILITY_PATTERNS.items():
            if pattern.search(text):
                return cap
        return "llm_synthesis"

    async def pre_task_health_check(self, server_name: str) -> bool:
        """Lightweight ping before task dispatch."""
        info = self._servers.get(server_name)
        if not info:
            return False
        if info.status == "UNAVAILABLE":
            return False
        config = self._configs.get(server_name)
        conn = self._connections.get(server_name)
        if not config or not conn or not conn.session:
            return info.status.startswith("READY")
        return await self._health_check(server_name, config, conn.session)

    def list_servers(self) -> List[ToolServerInfo]:
        return list(self._servers.values())

    async def close_all(self) -> None:
        """Close all aiohttp sessions and cancel background tasks."""
        await self.stop_background_discovery()
        for conn in self._connections.values():
            if conn.session:
                await conn.session.close()
        self._connections.clear()
