"""Tests for Part 1 changes: wave validation gate, retry-with-feedback,
sub-agent HITL primitive, sandbox discovery in scout, and schema extensions.

Uses mocks for LLM and storage so tests run without any external services.
"""
import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cortex.config.schema import TaskTypeConfig
from cortex.modules.capability_scout import (
    CapabilityScout,
    ScoutedCodeUtil,
    ScoutResult,
)
from cortex.modules.generic_mcp_agent import GenericMCPAgent
from cortex.modules.primary_agent import PrimaryAgent
from cortex.modules.result_envelope_store import ResultEnvelope
from cortex.modules.task_graph_compiler import (
    DecomposedTask,
    RuntimeTask,
    TaskGraphCompiler,
)
from cortex.streaming.status_events import (
    ClarificationRequestEvent,
    EventType,
)


# ────────────────────────── 1. TaskTypeConfig schema ──────────────────────────

def test_task_type_config_defaults_new_fields():
    """New fields must default to safe values (disabled/empty)."""
    t = TaskTypeConfig(name="x", description="y")
    assert t.output_schema is None
    assert t.validation_notes is None
    assert t.human_in_loop is False


def test_task_type_config_accepts_new_fields():
    t = TaskTypeConfig(
        name="x",
        description="y",
        output_schema={"type": "object", "required": ["title"]},
        validation_notes="title must not be empty",
        human_in_loop=True,
    )
    assert t.output_schema == {"type": "object", "required": ["title"]}
    assert t.validation_notes == "title must not be empty"
    assert t.human_in_loop is True


# ────────────────────────── 2. RuntimeTask state ──────────────────────────────

def test_runtime_task_defaults_retry_state():
    t = RuntimeTask(
        task_id="s/001_x",
        task_name="x",
        instruction="i",
        depends_on=[],
        depends_on_ids=[],
        input_refs=[],
    )
    assert t.attempt_count == 0
    assert t.validation_feedback is None


def test_snapshot_restore_preserves_retry_state():
    """attempt_count and validation_feedback must survive snapshot/restore."""
    compiler = TaskGraphCompiler()
    tt = TaskTypeConfig(name="a", description="A")
    compiled = compiler.compile([tt])
    decomposed = [DecomposedTask(task_name="a", instruction="do a")]
    graph = compiler.instantiate(compiled, "sess_test01", decomposed)

    task_id = next(iter(graph.tasks))
    graph.tasks[task_id].attempt_count = 2
    graph.tasks[task_id].validation_feedback = "missing field foo"

    snapshot = compiler.snapshot_graph(graph)
    restored = compiler.restore_graph(snapshot)

    restored_task = restored.tasks[task_id]
    assert restored_task.attempt_count == 2
    assert restored_task.validation_feedback == "missing field foo"


# ────────────────────── 3. ClarificationRequestEvent SSE ─────────────────────

def test_clarification_request_event_sse_shape():
    e = ClarificationRequestEvent(
        question="which region?",
        session_id="s1",
        clarification_id="hitl_s1_001_x_abc",
        task_id="s1/001_x",
        task_name="x",
        context="two viable paths",
    )
    assert e.event_type == EventType.CLARIFICATION_REQUEST
    sse = e.to_sse()
    assert sse.startswith("event: clarification_request\n")
    payload = json.loads(sse.split("data: ", 1)[1].strip())
    assert payload["type"] == "clarification_request"
    assert payload["task_id"] == "s1/001_x"
    assert payload["task_name"] == "x"
    assert payload["question"] == "which region?"
    assert payload["context"] == "two viable paths"
    assert payload["clarification_id"] == "hitl_s1_001_x_abc"


# ────────────────────── 4. CapabilityScout sandbox discovery ─────────────────

def test_scout_collect_code_utils_from_mock_store():
    """Scout must enumerate persisted scripts from AgentCodeStore."""
    mock_record = SimpleNamespace(
        task_name="scrape_site",
        description="Scrape a URL with BS4",
        script_path="/tmp/scrape_site_abc.py",
        use_count=7,
        added_to_yaml=True,
    )
    mock_store = MagicMock()
    mock_store.list_scripts.return_value = [mock_record]

    scout = CapabilityScout()
    utils = scout._collect_code_utils(mock_store)

    assert len(utils) == 1
    assert isinstance(utils[0], ScoutedCodeUtil)
    assert utils[0].task_name == "scrape_site"
    assert utils[0].description == "Scrape a URL with BS4"
    assert utils[0].use_count == 7
    assert utils[0].added_to_yaml is True


def test_scout_collect_code_utils_handles_none_store():
    scout = CapabilityScout()
    assert scout._collect_code_utils(None) == []


def test_scout_collect_code_utils_swallows_exceptions():
    """A broken code store must not crash the scout."""
    broken = MagicMock()
    broken.list_scripts.side_effect = RuntimeError("disk gone")
    scout = CapabilityScout()
    assert scout._collect_code_utils(broken) == []


@pytest.mark.asyncio
async def test_scout_run_returns_code_utils_even_without_capabilities():
    """Even with no MCP capabilities, scout should still surface code utils."""
    mock_record = SimpleNamespace(
        task_name="summarize_pdf",
        description="Read + summarize a PDF",
        script_path="/tmp/summarize_pdf_x.py",
        use_count=0,
        added_to_yaml=False,
    )
    mock_store = MagicMock()
    mock_store.list_scripts.return_value = [mock_record]

    scout = CapabilityScout()
    result = await scout.run(
        request="summarize this document",
        available_capabilities=[],
        registry=MagicMock(),
        llm_client=MagicMock(),
        max_capabilities=5,
        code_store=mock_store,
    )
    assert result.has_code_utils
    assert result.code_utils[0].task_name == "summarize_pdf"
    assert result.tools == []


# ───────────────── 5. PrimaryAgent stubs (validate_task_output, replan) ──────

@pytest.mark.asyncio
async def test_primary_validate_task_output_stub_returns_none():
    """Stub must return None so the wave gate treats all tasks as valid."""
    agent = PrimaryAgent(config=MagicMock(), llm_client=MagicMock())
    result = await agent.validate_task_output(
        task=MagicMock(),
        envelope=MagicMock(),
        validation_notes="anything",
    )
    assert result is None


@pytest.mark.asyncio
async def test_primary_replan_stub_is_noop():
    """Stub must return None and not raise."""
    agent = PrimaryAgent(config=MagicMock(), llm_client=MagicMock())
    result = await agent.replan(
        runtime_graph=MagicMock(),
        completed_envelopes=[],
        task_compiler=MagicMock(),
        event_queue=MagicMock(),
    )
    assert result is None


# ───────────────── 6. GenericMCPAgent.ask_human primitive ────────────────────

def _make_runtime_task(human_in_loop: bool) -> RuntimeTask:
    cfg = TaskTypeConfig(
        name="analyze",
        description="analyze stuff",
        human_in_loop=human_in_loop,
    )
    return RuntimeTask(
        task_id="sess123/001_analyze",
        task_name="analyze",
        instruction="do it",
        depends_on=[],
        depends_on_ids=[],
        input_refs=[],
        config=cfg,
    )


@pytest.mark.asyncio
async def test_ask_human_returns_none_when_hitl_disabled():
    """If human_in_loop=False, ask_human must not emit any event."""
    agent = GenericMCPAgent(session_storage_path="/tmp")
    task = _make_runtime_task(human_in_loop=False)
    queue = asyncio.Queue()
    result = await agent.ask_human(task, "?", event_queue=queue)
    assert result is None
    assert queue.empty()


@pytest.mark.asyncio
async def test_ask_human_returns_none_when_no_event_queue():
    agent = GenericMCPAgent(session_storage_path="/tmp")
    task = _make_runtime_task(human_in_loop=True)
    result = await agent.ask_human(task, "?", event_queue=None)
    assert result is None


@pytest.mark.asyncio
async def test_ask_human_resolves_via_registry():
    """Full round-trip: sub-agent asks, framework resolves, sub-agent wakes."""
    from cortex.framework import _PENDING_TASK_CLARIFICATIONS

    agent = GenericMCPAgent(session_storage_path="/tmp")
    task = _make_runtime_task(human_in_loop=True)
    queue = asyncio.Queue()

    # Start ask_human in the background; it will register an entry and wait.
    ask_task = asyncio.create_task(
        agent.ask_human(task, "pick A or B?", event_queue=queue, timeout_seconds=5)
    )

    # Wait for the clarification event to be emitted.
    event = await asyncio.wait_for(queue.get(), timeout=2)
    assert isinstance(event, ClarificationRequestEvent)
    assert event.task_id == "sess123/001_analyze"
    assert event.question == "pick A or B?"
    clar_id = event.clarification_id
    assert clar_id in _PENDING_TASK_CLARIFICATIONS

    # Simulate the framework resolver being called by the application.
    entry = _PENDING_TASK_CLARIFICATIONS[clar_id]
    entry["answer"] = "A"
    entry["event"].set()

    answer = await asyncio.wait_for(ask_task, timeout=2)
    assert answer == "A"
    assert clar_id not in _PENDING_TASK_CLARIFICATIONS


@pytest.mark.asyncio
async def test_ask_human_timeout_returns_none_and_cleans_up():
    from cortex.framework import _PENDING_TASK_CLARIFICATIONS

    agent = GenericMCPAgent(session_storage_path="/tmp")
    task = _make_runtime_task(human_in_loop=True)
    queue = asyncio.Queue()

    result = await agent.ask_human(
        task, "?", event_queue=queue, timeout_seconds=0.1
    )
    assert result is None
    # The clarification_id must be removed from the registry on timeout.
    for key in list(_PENDING_TASK_CLARIFICATIONS.keys()):
        assert not key.startswith("hitl_sess123") or (
            _PENDING_TASK_CLARIFICATIONS[key].get("answer") is not None
        )


# ───────────────── 7. Framework.resolve_task_clarification ───────────────────

def test_resolve_task_clarification_unknown_id_returns_false():
    """Resolver must return False gracefully when the id is unknown."""
    # Import here to avoid loading the whole framework at module import time.
    from cortex.framework import CortexFramework
    fw = CortexFramework.__new__(CortexFramework)  # bypass __init__
    assert fw.resolve_task_clarification("does_not_exist", "x") is False


# ───────────────── 8. validation_feedback injection in _execute_once ─────────

@pytest.mark.asyncio
async def test_validation_feedback_injected_into_llm_instruction():
    """When a task carries validation_feedback, the LLM must see a RETRY
    FEEDBACK block appended to its instruction."""
    seen_instructions = []

    async def fake_stream(*, messages, system, provider_name):
        seen_instructions.append(messages[0]["content"])
        for tok in ["ok"]:
            yield tok

    llm = MagicMock()
    llm.stream = fake_stream

    agent = GenericMCPAgent(session_storage_path="/tmp")
    cfg = TaskTypeConfig(
        name="t",
        description="d",
        capability_hint="llm_synthesis",
    )
    task = RuntimeTask(
        task_id="s/000_t",
        task_name="t",
        instruction="original instruction",
        depends_on=[],
        depends_on_ids=[],
        input_refs=[],
        config=cfg,
        validation_feedback="output was missing field 'summary'",
    )

    envelope_store = MagicMock()
    envelope_store.read_envelope = AsyncMock(return_value=None)

    envelope = await agent._execute_once(
        task=task,
        tool_registry=MagicMock(),
        llm_client=llm,
        envelope_store=envelope_store,
        config=cfg,
    )

    assert len(seen_instructions) == 1
    sent = seen_instructions[0]
    assert "original instruction" in sent
    assert "RETRY FEEDBACK" in sent
    assert "missing field 'summary'" in sent
    assert envelope.status == "complete"


@pytest.mark.asyncio
async def test_no_feedback_block_on_first_attempt():
    """Without validation_feedback, the RETRY FEEDBACK block must not appear."""
    seen = []

    async def fake_stream(*, messages, system, provider_name):
        seen.append(messages[0]["content"])
        yield "ok"

    llm = MagicMock()
    llm.stream = fake_stream

    agent = GenericMCPAgent(session_storage_path="/tmp")
    cfg = TaskTypeConfig(
        name="t", description="d", capability_hint="llm_synthesis",
    )
    task = RuntimeTask(
        task_id="s/000_t",
        task_name="t",
        instruction="fresh instruction",
        depends_on=[],
        depends_on_ids=[],
        input_refs=[],
        config=cfg,
    )
    store = MagicMock()
    store.read_envelope = AsyncMock(return_value=None)

    await agent._execute_once(
        task=task,
        tool_registry=MagicMock(),
        llm_client=llm,
        envelope_store=store,
        config=cfg,
    )
    assert "RETRY FEEDBACK" not in seen[0]


# ────────────── 9. Framework._run_wave_validation gate logic ────────────────

def _make_framework_for_gate():
    from cortex.framework import CortexFramework
    fw = CortexFramework.__new__(CortexFramework)  # bypass __init__
    return fw


@pytest.mark.asyncio
async def test_wave_gate_passes_when_no_contract():
    """A task with neither output_schema nor validation_notes must pass."""
    fw = _make_framework_for_gate()
    cfg = TaskTypeConfig(name="t", description="d")
    task = RuntimeTask(
        task_id="s/000_t", task_name="t", instruction="i",
        depends_on=[], depends_on_ids=[], input_refs=[], config=cfg,
    )
    envelope = ResultEnvelope(
        task_id=task.task_id, session_id="s", status="complete",
        output_value='{"ok": true}',
    )
    primary = MagicMock()
    result = await fw._run_wave_validation(task, envelope, primary)
    assert result is None


@pytest.mark.asyncio
async def test_wave_gate_schema_missing_required_field_fails():
    fw = _make_framework_for_gate()
    cfg = TaskTypeConfig(
        name="t", description="d",
        output_schema={"type": "object", "required": ["title", "sections"]},
    )
    task = RuntimeTask(
        task_id="s/000_t", task_name="t", instruction="i",
        depends_on=[], depends_on_ids=[], input_refs=[], config=cfg,
    )
    envelope = ResultEnvelope(
        task_id=task.task_id, session_id="s", status="complete",
        output_value='{"title": "hi"}',  # missing "sections"
    )
    primary = MagicMock()
    feedback = await fw._run_wave_validation(task, envelope, primary)
    assert feedback is not None
    assert "sections" in feedback


@pytest.mark.asyncio
async def test_wave_gate_schema_all_fields_present_passes():
    fw = _make_framework_for_gate()
    cfg = TaskTypeConfig(
        name="t", description="d",
        output_schema={"type": "object", "required": ["title"]},
    )
    task = RuntimeTask(
        task_id="s/000_t", task_name="t", instruction="i",
        depends_on=[], depends_on_ids=[], input_refs=[], config=cfg,
    )
    envelope = ResultEnvelope(
        task_id=task.task_id, session_id="s", status="complete",
        output_value='{"title": "hi"}',
    )
    # Primary will not be called because schema passed and no validation_notes.
    primary = MagicMock()
    primary.validate_task_output = AsyncMock()
    feedback = await fw._run_wave_validation(task, envelope, primary)
    assert feedback is None
    primary.validate_task_output.assert_not_called()


@pytest.mark.asyncio
async def test_wave_gate_invalid_json_when_schema_required_fails():
    fw = _make_framework_for_gate()
    cfg = TaskTypeConfig(
        name="t", description="d",
        output_schema={"type": "object", "required": ["x"]},
    )
    task = RuntimeTask(
        task_id="s/000_t", task_name="t", instruction="i",
        depends_on=[], depends_on_ids=[], input_refs=[], config=cfg,
    )
    envelope = ResultEnvelope(
        task_id=task.task_id, session_id="s", status="complete",
        output_value="this is not json",
    )
    primary = MagicMock()
    feedback = await fw._run_wave_validation(task, envelope, primary)
    assert feedback is not None
    assert "JSON" in feedback or "json" in feedback


@pytest.mark.asyncio
async def test_wave_gate_validation_notes_calls_primary_llm():
    """When validation_notes is set, the LLM-based validator must be invoked."""
    fw = _make_framework_for_gate()
    cfg = TaskTypeConfig(
        name="t", description="d",
        validation_notes="answer must mention Paris",
    )
    task = RuntimeTask(
        task_id="s/000_t", task_name="t", instruction="i",
        depends_on=[], depends_on_ids=[], input_refs=[], config=cfg,
    )
    envelope = ResultEnvelope(
        task_id=task.task_id, session_id="s", status="complete",
        output_value="The capital is London.",
    )
    primary = MagicMock()
    primary.validate_task_output = AsyncMock(return_value="missing 'Paris'")
    feedback = await fw._run_wave_validation(task, envelope, primary)
    assert feedback == "missing 'Paris'"
    primary.validate_task_output.assert_awaited_once()


@pytest.mark.asyncio
async def test_wave_gate_validation_notes_passes_when_primary_returns_none():
    fw = _make_framework_for_gate()
    cfg = TaskTypeConfig(
        name="t", description="d",
        validation_notes="any rules",
    )
    task = RuntimeTask(
        task_id="s/000_t", task_name="t", instruction="i",
        depends_on=[], depends_on_ids=[], input_refs=[], config=cfg,
    )
    envelope = ResultEnvelope(
        task_id=task.task_id, session_id="s", status="complete",
        output_value="anything",
    )
    primary = MagicMock()
    primary.validate_task_output = AsyncMock(return_value=None)
    feedback = await fw._run_wave_validation(task, envelope, primary)
    assert feedback is None
