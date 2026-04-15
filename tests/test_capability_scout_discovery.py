"""Tests for CapabilityScout internet discovery + end-to-end external MCP registration.

These tests monkeypatch the HTTP layer (aiohttp.ClientSession) and the
ToolServerRegistry.register_external_server coroutine so the scout's flow can
be exercised without any real network calls.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from cortex.config.schema import ExternalMCPDiscoveryConfig
from cortex.modules import capability_scout as cs_mod
from cortex.modules.capability_scout import CapabilityScout, ScoutedTool
from cortex.modules.external_mcp_registry import ExternalMCPRegistry
from cortex.modules.tool_server_registry import ToolInfo, ToolServerInfo


# ── Parse-response unit tests ────────────────────────────────────────────────

def test_parse_response_list_shape():
    data = [
        {"url": "https://a.example", "name": "A", "description": "alpha"},
        {"url": "https://b.example", "name": "B"},
    ]
    out = CapabilityScout._parse_registry_response(data, "https://src")
    assert len(out) == 2
    assert out[0]["url"] == "https://a.example"
    assert out[0]["source_registry"] == "https://src"


def test_parse_response_dict_with_servers_key():
    data = {"servers": [{"url": "https://z", "name": "Z"}]}
    out = CapabilityScout._parse_registry_response(data, "https://src")
    assert out[0]["url"] == "https://z"


def test_parse_response_alternate_url_fields():
    data = [{"endpoint": "https://end", "title": "T"}]
    out = CapabilityScout._parse_registry_response(data, "https://src")
    assert out[0]["url"] == "https://end"
    assert out[0]["name"] == "T"


def test_parse_response_filters_non_http_urls():
    data = [{"url": "ftp://nope", "name": "bad"}, {"url": "https://ok", "name": "ok"}]
    out = CapabilityScout._parse_registry_response(data, "src")
    assert len(out) == 1
    assert out[0]["url"] == "https://ok"


# ── _llm_select_candidate ────────────────────────────────────────────────────

class _FakeLLMResponse:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self, replies: List[str]):
        self._replies = list(replies)

    async def complete(self, **kwargs):
        return _FakeLLMResponse(self._replies.pop(0))


@pytest.mark.asyncio
async def test_llm_select_returns_chosen_candidate():
    scout = CapabilityScout()
    candidates = [
        {"url": "https://a", "name": "A"},
        {"url": "https://b", "name": "B"},
    ]
    llm = _FakeLLM(['{"index": 1, "reason": "better"}'])
    choice = await scout._llm_select_candidate("web_search", candidates, llm)
    assert choice["url"] == "https://b"


@pytest.mark.asyncio
async def test_llm_select_returns_none_when_index_is_negative():
    scout = CapabilityScout()
    candidates = [{"url": "https://a", "name": "A"}, {"url": "https://b", "name": "B"}]
    llm = _FakeLLM(['{"index": -1, "reason": "none fit"}'])
    assert await scout._llm_select_candidate("cap", candidates, llm) is None


@pytest.mark.asyncio
async def test_llm_select_single_candidate_no_call():
    scout = CapabilityScout()
    candidates = [{"url": "https://only", "name": "only"}]
    choice = await scout._llm_select_candidate("cap", candidates, llm_client=None)
    assert choice["url"] == "https://only"


# ── Fake aiohttp.ClientSession ───────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, *, status=200, body=b"", headers=None):
        self.status = status
        self._body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.headers = headers or {"Content-Type": "application/json"}

    async def read(self):
        return self._body

    async def json(self, content_type=None):
        return json.loads(self._body.decode("utf-8"))

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    """Minimal aiohttp.ClientSession replacement controlled by a URL → response map."""

    def __init__(self, url_map: Dict[str, _FakeResponse]):
        self._map = url_map

    def get(self, url, **kwargs):
        if url in self._map:
            return self._map[url]
        # Fallback: HTTP 404
        return _FakeResponse(status=404, body=b"{}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.fixture
def patched_client_session(monkeypatch):
    """Patch aiohttp.ClientSession in the capability_scout module with a function
    that the test populates via the closure's url_map dict."""
    url_map: Dict[str, _FakeResponse] = {}

    def _factory(*args, **kwargs):
        return _FakeSession(url_map)

    monkeypatch.setattr(cs_mod.aiohttp, "ClientSession", _factory)
    return url_map


# ── _search_registry_sources with fake HTTP ──────────────────────────────────

@pytest.mark.asyncio
async def test_search_registry_sources_deduplicates(patched_client_session):
    # Set up two sources each returning one overlapping URL
    smithery_url = "https://registry.smithery.ai/servers?q=web_search&pageSize=10"
    pulse_url = "https://www.pulsemcp.com/api/servers?search=web_search&count_per_page=10"
    patched_client_session[smithery_url] = _FakeResponse(
        body=json.dumps([{"url": "https://shared.example", "name": "Shared"}]).encode()
    )
    patched_client_session[pulse_url] = _FakeResponse(
        body=json.dumps({
            "servers": [
                {"url": "https://shared.example", "name": "Shared"},
                {"url": "https://pulse-only.example", "name": "P"},
            ]
        }).encode()
    )

    scout = CapabilityScout()
    candidates = await scout._search_registry_sources(
        capability="web_search",
        sources=["https://registry.smithery.ai", "https://www.pulsemcp.com"],
        timeout=5.0,
    )
    urls = {c["url"] for c in candidates}
    assert urls == {"https://shared.example", "https://pulse-only.example"}


# ── End-to-end: scout finds a gap, searches registries, registers external MCP ──

def _fake_tool_server_info(name, url, tool_names):
    info = ToolServerInfo(
        name=name,
        url=url,
        transport="sse",
        status="READY",
    )
    info.trust_tier = "external"
    info.tools = [ToolInfo(name=t, description=f"desc {t}") for t in tool_names]
    info.capabilities = ["web_search"]
    return info


@pytest.mark.asyncio
async def test_end_to_end_external_registration(tmp_path, patched_client_session):
    # Fake discovery config pointing at one source
    discovery_config = ExternalMCPDiscoveryConfig(
        enabled=True,
        auto_discovery_file=str(tmp_path / "auto.yaml"),
        registry_sources=["https://registry.smithery.ai"],
        max_new_per_session=5,
        max_stale_days=30,
        search_timeout_s=5.0,
    )

    # Fake external registry
    ext = ExternalMCPRegistry(str(tmp_path / "auto.yaml"))
    ext.load()

    # Populate HTTP responses:
    # 1. Registry search returns one candidate
    registry_search_url = (
        "https://registry.smithery.ai/servers?q=web_search&pageSize=10"
    )
    patched_client_session[registry_search_url] = _FakeResponse(
        body=json.dumps([{
            "url": "https://safe-mcp.example",
            "name": "SafeSearch",
            "description": "A web search MCP",
        }]).encode()
    )
    # 2. /tools probe returns a clean tool list
    tools_probe_url = "https://safe-mcp.example/tools"
    patched_client_session[tools_probe_url] = _FakeResponse(
        body=json.dumps({
            "tools": [
                {"name": "search", "description": "search the web"},
                {"name": "fetch", "description": "fetch a url"},
            ]
        }).encode(),
        headers={"Content-Type": "application/json"},
    )

    # Fake ToolServerRegistry — just enough surface for the scout to call into it
    class _FakeToolRegistry:
        def __init__(self):
            self._servers = {}
            self.register_calls = []

        async def get_capability_servers(self, cap):
            return []

        async def register_external_server(self, name, url, capabilities):
            self.register_calls.append((name, url, capabilities))
            info = _fake_tool_server_info(name, url, ["search", "fetch"])
            info.capabilities = list(capabilities)
            self._servers[name] = info
            return info

    tool_registry = _FakeToolRegistry()

    # Fake LLM: first call is capability matching, second is candidate selection
    llm = _FakeLLM([
        '["web_search"]',
        '{"index": 0, "reason": "fits"}',
    ])

    scout = CapabilityScout()
    result = await scout.run(
        request="search something online",
        available_capabilities=["web_search"],
        registry=tool_registry,
        llm_client=llm,
        external_registry=ext,
        discovery_config=discovery_config,
    )

    # Verify internal registration was called
    assert len(tool_registry.register_calls) == 1
    assert tool_registry.register_calls[0][1] == "https://safe-mcp.example"
    assert tool_registry.register_calls[0][2] == ["web_search"]

    # Scout result surfaces the external tools
    assert result.matched_capabilities == ["web_search"]
    assert any(t.name == "search" for t in result.tools)
    assert result.unresolved_gaps == []

    # External registry has persisted the new record
    verified = ext.get_all_verified()
    assert len(verified) == 1
    assert verified[0].url == "https://safe-mcp.example"
    assert ext.get_auth_pending() == []


@pytest.mark.asyncio
async def test_end_to_end_rejects_auth_required(tmp_path, patched_client_session):
    """401 from /tools → queued for user notification, not registered."""
    discovery_config = ExternalMCPDiscoveryConfig(
        enabled=True,
        auto_discovery_file=str(tmp_path / "auto.yaml"),
        registry_sources=["https://registry.smithery.ai"],
    )
    ext = ExternalMCPRegistry(str(tmp_path / "auto.yaml"))
    ext.load()

    patched_client_session[
        "https://registry.smithery.ai/servers?q=web_search&pageSize=10"
    ] = _FakeResponse(body=json.dumps([{
        "url": "https://paid-mcp.example",
        "name": "Paid",
    }]).encode())
    patched_client_session["https://paid-mcp.example/tools"] = _FakeResponse(
        status=401, body=b"{}",
    )

    class _FakeToolRegistry:
        _servers = {}

        async def get_capability_servers(self, cap):
            return []

        async def register_external_server(self, name, url, capabilities):
            raise AssertionError("must not register a server that requires auth")

    llm = _FakeLLM(['["web_search"]', '{"index": 0, "reason": "x"}'])

    scout = CapabilityScout()
    result = await scout.run(
        request="search",
        available_capabilities=["web_search"],
        registry=_FakeToolRegistry(),
        llm_client=llm,
        external_registry=ext,
        discovery_config=discovery_config,
    )

    # Nothing registered; gap remains unresolved
    assert result.tools == []
    assert result.unresolved_gaps == ["web_search"]

    # But auth-pending notification is queued
    pending = ext.get_auth_pending()
    assert len(pending) == 1
    assert pending[0].url == "https://paid-mcp.example"
    assert pending[0].auth_required is True


@pytest.mark.asyncio
async def test_end_to_end_rejects_unsafe_output(tmp_path, patched_client_session):
    """PDF MIME type on /tools → verification_failed, not registered."""
    discovery_config = ExternalMCPDiscoveryConfig(
        enabled=True,
        auto_discovery_file=str(tmp_path / "auto.yaml"),
        registry_sources=["https://registry.smithery.ai"],
    )
    ext = ExternalMCPRegistry(str(tmp_path / "auto.yaml"))
    ext.load()

    patched_client_session[
        "https://registry.smithery.ai/servers?q=web_search&pageSize=10"
    ] = _FakeResponse(body=json.dumps([{
        "url": "https://bad-mcp.example",
        "name": "Bad",
    }]).encode())
    patched_client_session["https://bad-mcp.example/tools"] = _FakeResponse(
        body=b"%PDF-1.4 dangerous",
        headers={"Content-Type": "application/pdf"},
    )

    class _FakeToolRegistry:
        _servers = {}

        async def get_capability_servers(self, cap):
            return []

        async def register_external_server(self, name, url, capabilities):
            raise AssertionError("must not register a server with unsafe output")

    llm = _FakeLLM(['["web_search"]', '{"index": 0, "reason": "x"}'])

    scout = CapabilityScout()
    result = await scout.run(
        request="search",
        available_capabilities=["web_search"],
        registry=_FakeToolRegistry(),
        llm_client=llm,
        external_registry=ext,
        discovery_config=discovery_config,
    )

    assert result.tools == []
    assert result.unresolved_gaps == ["web_search"]
    # Auth pending is empty (not an auth issue)
    assert ext.get_auth_pending() == []
