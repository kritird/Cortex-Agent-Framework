"""Tests for ToolServerRegistry."""
import asyncio
import pytest
from cortex.modules.tool_server_registry import ToolServerRegistry


@pytest.mark.asyncio
async def test_empty_initialization():
    registry = ToolServerRegistry()
    report = await registry.initialize_all({})
    assert report.all_ready
    assert len(report.servers) == 0


def test_classify_capability():
    registry = ToolServerRegistry()
    assert registry.classify_capability("web_search_tool", "Search the web for results") == "web_search"
    assert registry.classify_capability("generate_pdf", "Create a PDF document") == "document_generation"
    assert registry.classify_capability("draw_image", "Render an image") == "image_generation"
    assert registry.classify_capability("run_script", "Execute a bash script") == "bash"
    assert registry.classify_capability("unknown_tool", "Does something unclear") == "llm_synthesis"


def test_session_start_event_no_servers():
    registry = ToolServerRegistry()
    msg = registry.emit_session_start_event()
    assert "Starting your session" in msg
