"""Unit tests for ToolServerRegistry._filter_write_tools and trust_tier defaults."""
from cortex.modules.tool_server_registry import (
    ToolInfo,
    ToolServerInfo,
    ToolServerRegistry,
)


def test_trust_tier_defaults_to_internal():
    info = ToolServerInfo(name="x", url=None, transport="sse", status="READY")
    assert info.trust_tier == "internal"


def _mk(name, description="", schema=None):
    return ToolInfo(name=name, description=description, input_schema=schema or {})


def test_filter_removes_keyword_write_tools():
    tools = [
        _mk("search_web", "Search the web"),
        _mk("create_file", "Create a new file"),
        _mk("delete_record", "Remove a record"),
        _mk("read_document", "Read a document"),
    ]
    safe = ToolServerRegistry._filter_write_tools(tools)
    safe_names = {t.name for t in safe}
    assert "search_web" in safe_names
    assert "read_document" in safe_names
    assert "create_file" not in safe_names
    assert "delete_record" not in safe_names


def test_filter_catches_camel_case_writes():
    tools = [
        _mk("writeLog", "persist a log"),
        _mk("getWeather", "fetch forecast"),
    ]
    safe = {t.name for t in ToolServerRegistry._filter_write_tools(tools)}
    assert "getWeather" in safe
    assert "writeLog" not in safe


def test_filter_catches_description_write_words():
    tools = [
        _mk("action", "This tool will upload a file to s3"),
        _mk("stats", "Return statistics about the current state"),
    ]
    safe = {t.name for t in ToolServerRegistry._filter_write_tools(tools)}
    assert "stats" in safe
    assert "action" not in safe


def test_schema_signal_content_plus_path_is_write():
    schema = {
        "type": "object",
        "properties": {
            "content": {"type": "string"},
            "path": {"type": "string"},
        },
    }
    tools = [_mk("store_something", "Stores data", schema=schema)]
    assert ToolServerRegistry._filter_write_tools(tools) == []


def test_schema_signal_content_only_is_safe():
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
    }
    tools = [_mk("fetch_thing", "Reads data", schema=schema)]
    safe = ToolServerRegistry._filter_write_tools(tools)
    assert len(safe) == 1


def test_schema_suggests_write_helper():
    assert ToolServerRegistry._schema_suggests_write({
        "properties": {"body": {}, "filename": {}},
    })
    assert not ToolServerRegistry._schema_suggests_write({
        "properties": {"query": {}},
    })
    assert not ToolServerRegistry._schema_suggests_write({})
