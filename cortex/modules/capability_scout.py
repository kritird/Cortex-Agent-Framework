"""CapabilityScout — pre-decomposition step that identifies relevant MCP tools,
persisted sandbox code utilities, and — when internal tools fall short — auto-
discovers external MCPs from the internet.

Flow per session:
  1. Cheap LLM call: "which of these capabilities apply to this request?" → list of names
  2. Lazy-probe one server per matched capability → fetch actual tool names + descriptions
  3. Enumerate persisted sandbox scripts from AgentCodeStore (if provided) so the
     decomposition LLM sees both MCP tools and code utilities as a unified surface.
  4. If unmatched capability gaps remain, check ExternalMCPRegistry for already-known
     external servers that cover the gap.
  5. If gaps still remain, run internet search against the configured registry pool,
     validate candidates (no auth, safe output), register new external MCPs.
  6. Return ScoutResult so build_system_prompt can surface real tool vocabulary to the
     decomposition LLM instead of abstract capability names.

The same scout can be called mid-session via :meth:`discover_for_task` when a
running task finds no suitable tool.  The scout is the single entry point for all
capability discovery — both at session start and mid-run.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import aiohttp

from cortex.llm.client import LLMClient
from cortex.modules.tool_server_registry import ToolServerRegistry

if TYPE_CHECKING:
    from cortex.modules.external_mcp_registry import ExternalMCPRegistry
    from cortex.config.schema import ExternalMCPDiscoveryConfig

logger = logging.getLogger(__name__)

_SCOUT_SYSTEM = (
    "You are a capability router for an AI agent framework. "
    "Given a user request and a list of available capability names, "
    "identify which capabilities are needed to fulfill the request. "
    "Respond ONLY with a valid JSON array of matching capability names, "
    "chosen from the provided list. No explanations, no other text. "
    "Example: [\"web_search\", \"document_generation\"]"
)

_MCP_MATCH_SYSTEM = (
    "You are a tool-server selector for an AI agent framework. "
    "Given a capability gap description and a list of MCP server candidates "
    "(each with a name, description, and URL), select the single best candidate "
    "that can fill the gap without requiring authentication. "
    "Respond ONLY with a valid JSON object: "
    "{\"index\": <0-based index into the candidates list>, \"reason\": \"<one sentence>\"}. "
    "If no candidate is suitable, respond with {\"index\": -1, \"reason\": \"...\"}."
)

# Hard cap: never surface more than this many tools per capability in the prompt.
_MAX_TOOLS_PER_CAPABILITY = 10
# Hard cap on total tool descriptions passed to decompose to keep context tight.
_MAX_TOTAL_TOOLS = 50

# Registry source query endpoints (path templates; {q} is replaced with the search term).
_REGISTRY_QUERY_TEMPLATES: Dict[str, str] = {
    "https://registry.smithery.ai": "/servers?q={q}&pageSize=10",
    "https://www.pulsemcp.com":     "/api/servers?search={q}&count_per_page=10",
    "https://glama.ai":             "/api/mcp/servers?q={q}&limit=10",
    "https://mcp.so":               "/api/servers?search={q}&limit=10",
}


@dataclass
class ScoutedTool:
    name: str
    description: str
    capability: str
    server_name: str


@dataclass
class ScoutedCodeUtil:
    """A persisted sandbox script that is available for the decomposition LLM
    to route tasks into."""
    task_name: str
    description: str
    script_path: str
    use_count: int = 0
    added_to_yaml: bool = False


@dataclass
class ScoutResult:
    """Outcome of the capability scouting step."""
    matched_capabilities: List[str] = field(default_factory=list)
    tools: List[ScoutedTool] = field(default_factory=list)
    code_utils: List[ScoutedCodeUtil] = field(default_factory=list)
    # Capabilities that could not be matched by any internal or external server
    unresolved_gaps: List[str] = field(default_factory=list)

    @property
    def has_tools(self) -> bool:
        return bool(self.tools)

    @property
    def has_code_utils(self) -> bool:
        return bool(self.code_utils)

    def tools_by_capability(self):
        """Group tools by capability for prompt rendering."""
        grouped: Dict[str, List[ScoutedTool]] = {}
        for tool in self.tools:
            grouped.setdefault(tool.capability, []).append(tool)
        return grouped


class CapabilityScout:
    """
    Runs before LLM Call #1 (decompose) to identify which MCP servers are
    relevant to the current request and surface their actual tool descriptions.

    The scout itself makes one non-streaming LLM call (cheap: just capability
    names as input/output) then lazily probes only the matched servers.

    When internal servers cannot cover all matched capabilities, the scout
    queries the configured registry pool, validates candidates, and registers
    safe external MCPs via :class:`ToolServerRegistry.register_external_server`.

    The same instance can be reused for mid-run on-demand discovery via
    :meth:`discover_for_task`.
    """

    async def run(
        self,
        request: str,
        available_capabilities: List[str],
        registry: ToolServerRegistry,
        llm_client: LLMClient,
        max_capabilities: int = 5,
        code_store=None,
        no_probe_capabilities: Optional[set] = None,
        external_registry: Optional["ExternalMCPRegistry"] = None,
        discovery_config: Optional["ExternalMCPDiscoveryConfig"] = None,
    ) -> ScoutResult:
        """
        Returns a ScoutResult with matched capabilities, their tool descriptions,
        and any persisted sandbox code utilities.

        Never raises — on any failure returns an empty ScoutResult so the session
        continues without scout enrichment.

        Parameters
        ----------
        external_registry:
            ExternalMCPRegistry instance.  When provided, the scout will consult
            it for already-known external servers and may register new ones.
        discovery_config:
            ExternalMCPDiscoveryConfig from cortex.yaml.  Controls whether
            internet discovery is attempted and which registry sources are used.
        """
        code_utils = self._collect_code_utils(code_store)

        if not available_capabilities:
            return ScoutResult(code_utils=code_utils)

        matched = await self._match_capabilities(
            request, available_capabilities, llm_client, max_capabilities
        )
        if not matched:
            logger.info("Scout found no matching capabilities for request")
            return ScoutResult(matched_capabilities=[], code_utils=code_utils)

        logger.info("Scout matched capabilities: %s", matched)

        # Skip MCP probing for capabilities that belong exclusively to
        # scripted tasks — they run a Python handler, no LLM, no MCP needed.
        probe_caps = matched
        if no_probe_capabilities:
            probe_caps = [c for c in matched if c not in no_probe_capabilities]
            skipped = [c for c in matched if c in no_probe_capabilities]
            if skipped:
                logger.debug(
                    "Scout: skipping MCP probe for scripted caps: %s", skipped
                )

        tools = await self._collect_tools(probe_caps, registry)

        # Identify capabilities that internal servers couldn't cover
        covered = {t.capability for t in tools}
        gaps = [c for c in probe_caps if c not in covered]

        if gaps and external_registry is not None and discovery_config is not None:
            ext_tools = await self._resolve_gaps_externally(
                gaps=gaps,
                registry=registry,
                external_registry=external_registry,
                discovery_config=discovery_config,
                llm_client=llm_client,
            )
            tools.extend(ext_tools)
            covered = {t.capability for t in tools}

        unresolved = [c for c in probe_caps if c not in covered]
        if unresolved:
            logger.info("Scout: unresolved capability gap(s): %s", unresolved)

        return ScoutResult(
            matched_capabilities=matched,
            tools=tools,
            code_utils=code_utils,
            unresolved_gaps=unresolved,
        )

    async def discover_for_task(
        self,
        capability: str,
        registry: ToolServerRegistry,
        llm_client: LLMClient,
        external_registry: "ExternalMCPRegistry",
        discovery_config: "ExternalMCPDiscoveryConfig",
    ) -> List[ScoutedTool]:
        """On-demand mid-run discovery for a single capability gap.

        Called by :class:`GenericMCPAgent` when it finds no tool server for the
        task's capability and wants the scout to search for an external one
        before falling back to LLM synthesis.

        Returns newly registered tools for *capability*, or an empty list if
        nothing could be found.  Never raises.
        """
        if not discovery_config.enabled:
            return []
        try:
            tools = await asyncio.wait_for(
                self._resolve_gaps_externally(
                    gaps=[capability],
                    registry=registry,
                    external_registry=external_registry,
                    discovery_config=discovery_config,
                    llm_client=llm_client,
                ),
                timeout=discovery_config.search_timeout_s,
            )
            return tools
        except asyncio.TimeoutError:
            logger.warning(
                "Scout.discover_for_task: timed out searching for '%s'", capability
            )
            return []
        except Exception as exc:
            logger.warning(
                "Scout.discover_for_task: unexpected error for '%s': %s", capability, exc
            )
            return []

    # ── Internal tools collection ──────────────────────────────────────────────

    def _collect_code_utils(self, code_store) -> List[ScoutedCodeUtil]:
        """Enumerate persisted sandbox scripts from the code store."""
        if code_store is None:
            return []
        try:
            records = code_store.list_scripts()
        except Exception as exc:
            logger.debug("Scout: failed to enumerate code store: %s", exc)
            return []

        utils: List[ScoutedCodeUtil] = []
        for rec in records[:_MAX_TOTAL_TOOLS]:
            utils.append(ScoutedCodeUtil(
                task_name=rec.task_name,
                description=rec.description or "",
                script_path=rec.script_path,
                use_count=getattr(rec, "use_count", 0),
                added_to_yaml=getattr(rec, "added_to_yaml", False),
            ))
        if utils:
            logger.info("Scout: discovered %d persisted code util(s)", len(utils))
        return utils

    async def _match_capabilities(
        self,
        request: str,
        available_capabilities: List[str],
        llm_client: LLMClient,
        max_capabilities: int,
    ) -> List[str]:
        """Ask LLM which capabilities are relevant. Returns subset of available_capabilities."""
        user_msg = (
            f"User request: {request}\n\n"
            f"Available capabilities: {json.dumps(available_capabilities)}"
        )
        try:
            response = await llm_client.complete(
                messages=[{"role": "user", "content": user_msg}],
                system=_SCOUT_SYSTEM,
                max_tokens=256,
            )
            raw = response.content.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                return []
            valid = [c for c in parsed if c in available_capabilities]
            return valid[:max_capabilities]
        except Exception as exc:
            logger.warning("Scout LLM call failed (%s) — skipping enrichment", exc)
            return []

    async def _collect_tools(
        self,
        matched_capabilities: List[str],
        registry: ToolServerRegistry,
    ) -> List[ScoutedTool]:
        """For each matched capability, lazy-probe one server and read its tool list."""
        tools: List[ScoutedTool] = []
        for cap in matched_capabilities:
            if len(tools) >= _MAX_TOTAL_TOOLS:
                break
            try:
                conns = await registry.get_capability_servers(cap)
                if not conns:
                    logger.debug("Scout: no ready server for capability '%s'", cap)
                    continue
                conn = conns[0]
                server_info = registry._servers.get(conn.server_name)
                if not server_info:
                    continue
                server_tools = server_info.tools[:_MAX_TOOLS_PER_CAPABILITY]
                for t in server_tools:
                    desc = t.description[:200] if t.description else ""
                    tools.append(ScoutedTool(
                        name=t.name,
                        description=desc,
                        capability=cap,
                        server_name=conn.server_name,
                    ))
                    if len(tools) >= _MAX_TOTAL_TOOLS:
                        break
            except Exception as exc:
                logger.debug("Scout: failed to collect tools for '%s': %s", cap, exc)
        return tools

    # ── External discovery ─────────────────────────────────────────────────────

    async def _resolve_gaps_externally(
        self,
        gaps: List[str],
        registry: ToolServerRegistry,
        external_registry: "ExternalMCPRegistry",
        discovery_config: "ExternalMCPDiscoveryConfig",
        llm_client: LLMClient,
    ) -> List[ScoutedTool]:
        """Try to fill capability *gaps* using the external MCP registry and
        internet search.  Returns newly discovered ScoutedTool entries."""
        if not discovery_config.enabled:
            return []

        all_tools: List[ScoutedTool] = []
        new_registrations = 0

        for gap in gaps:
            if new_registrations >= discovery_config.max_new_per_session:
                logger.info(
                    "Scout: reached max_new_per_session (%d) — skipping remaining gaps",
                    discovery_config.max_new_per_session,
                )
                break

            # Step 1: check already-known external registry entries first
            known = external_registry.lookup_by_capability(gap)
            for rec in known:
                # Re-verify if stale
                if external_registry.needs_reverification(rec.url, discovery_config.max_stale_days):
                    ok = await self._verify_existing_external(
                        rec, registry, external_registry, discovery_config
                    )
                    if not ok:
                        continue
                # Surface tools from the already-registered server
                server_info = registry._servers.get(rec.name)
                if server_info and server_info.status.startswith("READY"):
                    for t in server_info.tools[:_MAX_TOOLS_PER_CAPABILITY]:
                        all_tools.append(ScoutedTool(
                            name=t.name,
                            description=t.description[:200] if t.description else "",
                            capability=gap,
                            server_name=rec.name,
                        ))
                    logger.info(
                        "Scout: gap '%s' filled by already-known external server '%s'",
                        gap, rec.name,
                    )
                    break  # gap covered; move to next

            # Check if the gap is already covered after checking known entries
            covered_names = {t.server_name for t in all_tools if t.capability == gap}
            if covered_names:
                continue

            # Step 2: internet search across registry sources
            logger.info("Scout: searching internet for capability '%s'", gap)
            candidates = await self._search_registry_sources(
                capability=gap,
                sources=discovery_config.registry_sources,
                timeout=discovery_config.search_timeout_s,
            )
            if not candidates:
                logger.info("Scout: no internet candidates found for '%s'", gap)
                continue

            # Step 3: LLM picks the best candidate
            best = await self._llm_select_candidate(gap, candidates, llm_client)
            if best is None:
                logger.info("Scout: LLM found no suitable candidate for '%s'", gap)
                continue

            url = best.get("url", "")
            name = best.get("name", url)
            source_registry = best.get("source_registry", "")

            if not url:
                continue

            # Already known (possibly with failed status) — skip re-registration
            if external_registry.has_url(url) and not external_registry.needs_reverification(
                url, discovery_config.max_stale_days
            ):
                logger.debug("Scout: candidate '%s' already in registry — skipping", url)
                continue

            # Step 4: validate and register
            new_tools = await self._validate_and_register(
                url=url,
                name=name,
                capability=gap,
                source_registry=source_registry,
                registry=registry,
                external_registry=external_registry,
            )
            if new_tools:
                all_tools.extend(new_tools)
                new_registrations += 1

        return all_tools

    async def _search_registry_sources(
        self,
        capability: str,
        sources: List[str],
        timeout: float,
    ) -> List[Dict[str, Any]]:
        """Query each configured registry source for *capability*.

        Returns a flat deduplicated list of candidate dicts:
          {url, name, description, source_registry}
        """
        q = urllib.parse.quote(capability)
        all_candidates: List[Dict[str, Any]] = []
        seen_urls: set = set()

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"User-Agent": "CortexAgentFramework/1.0 MCPDiscovery"},
        ) as session:
            tasks = [
                self._query_one_source(session, base, q)
                for base in sources
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.debug("Scout: registry source error — %s", result)
                continue
            for candidate in result:
                url = candidate.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    all_candidates.append(candidate)

        return all_candidates

    @staticmethod
    async def _query_one_source(
        session: aiohttp.ClientSession,
        base_url: str,
        q: str,
    ) -> List[Dict[str, Any]]:
        """Query a single registry source.  Returns a list of raw candidate dicts."""
        template = _REGISTRY_QUERY_TEMPLATES.get(base_url, "/search?q={q}")
        path = template.replace("{q}", q)
        url = base_url.rstrip("/") + path
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.debug("Scout: %s returned HTTP %d", url, resp.status)
                    return []
                data = await resp.json(content_type=None)
                return CapabilityScout._parse_registry_response(data, base_url)
        except Exception as exc:
            logger.debug("Scout: failed to query %s: %s", url, exc)
            return []

    @staticmethod
    def _parse_registry_response(
        data: Any,
        source_registry: str,
    ) -> List[Dict[str, Any]]:
        """Normalise a raw registry API response into a list of candidate dicts."""
        candidates: List[Dict[str, Any]] = []

        # Handle list at top level
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            # Common response shapes across registries
            items = (
                data.get("servers")
                or data.get("results")
                or data.get("data")
                or data.get("items")
                or []
            )
        else:
            return []

        for item in items:
            if not isinstance(item, dict):
                continue
            # Extract URL — different registries use different field names
            url = (
                item.get("url")
                or item.get("endpoint")
                or item.get("server_url")
                or item.get("mcp_url")
                or ""
            )
            if not url:
                continue
            # Must look like an HTTP(S) URL
            if not url.startswith(("http://", "https://")):
                continue
            name = (
                item.get("name")
                or item.get("displayName")
                or item.get("title")
                or url
            )
            description = (
                item.get("description")
                or item.get("summary")
                or ""
            )
            candidates.append({
                "url": url,
                "name": name,
                "description": description[:300],
                "source_registry": source_registry,
            })

        return candidates

    async def _llm_select_candidate(
        self,
        capability: str,
        candidates: List[Dict[str, Any]],
        llm_client: LLMClient,
    ) -> Optional[Dict[str, Any]]:
        """Ask the LLM to pick the best candidate for *capability*."""
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        candidates_text = json.dumps(
            [{"index": i, **c} for i, c in enumerate(candidates)],
            indent=2,
        )
        user_msg = (
            f"Capability gap: {capability}\n\n"
            f"Candidates:\n{candidates_text}"
        )
        try:
            response = await llm_client.complete(
                messages=[{"role": "user", "content": user_msg}],
                system=_MCP_MATCH_SYSTEM,
                max_tokens=128,
            )
            raw = response.content.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            parsed = json.loads(raw)
            idx = parsed.get("index", -1)
            if idx == -1 or not isinstance(idx, int):
                return None
            if 0 <= idx < len(candidates):
                logger.debug(
                    "Scout: LLM selected candidate[%d] '%s' for capability '%s' — %s",
                    idx, candidates[idx].get("name"), capability, parsed.get("reason"),
                )
                return candidates[idx]
        except Exception as exc:
            logger.debug("Scout: LLM candidate selection failed: %s", exc)
            # Fall back to first candidate
            return candidates[0]
        return None

    async def _validate_and_register(
        self,
        url: str,
        name: str,
        capability: str,
        source_registry: str,
        registry: ToolServerRegistry,
        external_registry: "ExternalMCPRegistry",
    ) -> List[ScoutedTool]:
        """Probe the candidate MCP server, check for auth requirement, validate
        a sample output through MCPOutputGuard, and register if safe.

        Returns a list of :class:`ScoutedTool` entries on success, or an empty
        list if the server was rejected (auth required, unsafe output, etc.).
        """
        from datetime import datetime, timezone
        from cortex.config.auto_discovery_schema import AutoDiscoveredMCPRecord
        from cortex.security.mcp_output_guard import MCPOutputGuard, MCPOutputSecurityError

        guard = MCPOutputGuard()
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": "CortexAgentFramework/1.0 MCPDiscovery"},
            ) as session:
                # ── Probe 1: tool list endpoint ────────────────────────────────
                tools_url = url.rstrip("/") + "/tools"
                async with session.get(tools_url) as resp:
                    # Authentication gate — 401/403 → mark and reject
                    if resp.status in (401, 403):
                        reason = f"HTTP {resp.status} on /tools endpoint"
                        external_registry.mark_auth_required(url, name=name, reason=reason)
                        logger.info(
                            "Scout: external MCP '%s' requires auth (%s) — queued for user notification",
                            url, reason,
                        )
                        return []

                    if resp.status != 200:
                        external_registry.mark_verification_failed(url, f"HTTP {resp.status}")
                        return []

                    # ── Output safety check on the tools response ──────────────
                    content_type = resp.headers.get("Content-Type", "")
                    raw_bytes = await resp.read()
                    try:
                        guard.check(raw_bytes, content_type=content_type)
                    except MCPOutputSecurityError as sec_err:
                        logger.warning(
                            "Scout: external MCP '%s' failed output guard on /tools: %s",
                            url, sec_err.reason,
                        )
                        external_registry.mark_verification_failed(url, str(sec_err.reason))
                        return []

                    # Parse tool list
                    try:
                        data = json.loads(raw_bytes.decode("utf-8", errors="replace"))
                    except Exception:
                        external_registry.mark_verification_failed(url, "non-JSON /tools response")
                        return []

                    tools_raw = data if isinstance(data, list) else data.get("tools", [])
                    if not tools_raw:
                        external_registry.mark_verification_failed(url, "empty tool list")
                        return []

        except aiohttp.ClientError as conn_err:
            logger.debug("Scout: could not connect to '%s': %s", url, conn_err)
            external_registry.mark_verification_failed(url, str(conn_err))
            return []
        except Exception as exc:
            logger.debug("Scout: unexpected error probing '%s': %s", url, exc)
            external_registry.mark_verification_failed(url, str(exc))
            return []

        # ── Register in ToolServerRegistry (write filter applied inside) ───────
        server_name = re.sub(r"[^\w-]", "_", name)[:40]
        server_info = await registry.register_external_server(
            name=server_name,
            url=url,
            capabilities=[capability],
        )
        if not server_info.status.startswith("READY"):
            external_registry.mark_verification_failed(url, "server not READY after registration")
            return []

        # ── Persist to ExternalMCPRegistry ─────────────────────────────────────
        record = AutoDiscoveredMCPRecord(
            url=url,
            name=server_name,
            description="",
            capabilities=[capability],
            source_registry=source_registry,
            discovered_at=now_iso,
            last_verified=now_iso,
        )
        external_registry.register(record)

        # ── Return ScoutedTool entries for the registered server ───────────────
        new_tools: List[ScoutedTool] = []
        for t in server_info.tools[:_MAX_TOOLS_PER_CAPABILITY]:
            new_tools.append(ScoutedTool(
                name=t.name,
                description=t.description[:200] if t.description else "",
                capability=capability,
                server_name=server_name,
            ))

        logger.info(
            "Scout: registered external MCP '%s' for capability '%s' (%d tool(s))",
            server_name, capability, len(new_tools),
        )
        return new_tools

    async def _verify_existing_external(
        self,
        rec: Any,
        registry: ToolServerRegistry,
        external_registry: "ExternalMCPRegistry",
        discovery_config: "ExternalMCPDiscoveryConfig",
    ) -> bool:
        """Re-verify a stale existing external MCP record.

        Returns True if the server is still reachable and safe, False otherwise.
        """
        from cortex.security.mcp_output_guard import MCPOutputGuard, MCPOutputSecurityError

        guard = MCPOutputGuard()
        url = rec.url
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=discovery_config.search_timeout_s),
            ) as session:
                async with session.get(url.rstrip("/") + "/tools") as resp:
                    if resp.status in (401, 403):
                        external_registry.mark_auth_required(
                            url, name=rec.name,
                            reason=f"re-verification: HTTP {resp.status}",
                        )
                        return False
                    if resp.status != 200:
                        external_registry.mark_verification_failed(
                            url, f"re-verification: HTTP {resp.status}"
                        )
                        return False
                    content_type = resp.headers.get("Content-Type", "")
                    raw_bytes = await resp.read()
                    try:
                        guard.check(raw_bytes, content_type=content_type)
                    except MCPOutputSecurityError as err:
                        external_registry.mark_verification_failed(url, str(err.reason))
                        return False
            external_registry.mark_verified(url)
            return True
        except Exception as exc:
            logger.debug("Scout: re-verification of '%s' failed: %s", url, exc)
            external_registry.mark_verification_failed(url, str(exc))
            return False
