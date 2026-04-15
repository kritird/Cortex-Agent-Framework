"""Tests for Part 2 changes:
  - Real validate_task_output LLM judge (pass/fail/malformed/error).
  - Unified sandbox discovery (persisted scripts via scout_result.code_utils).
  - Sub-agent HITL decision logic inside _call_llm (<ask_human> parsing,
    ask cap, budget reset on wave retry).

Uses mocks for LLM / registry / storage so tests run without any external
services.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cortex.config.schema import (
    AgentConfig, CortexConfig, LLMAccessConfig, LLMProviderConfig,
    StorageConfig, TaskTypeConfig, ValidationConfig,
)
from cortex.llm.context import TokenUsage
from cortex.modules.capability_scout import ScoutedCodeUtil, ScoutResult
from cortex.modules.generic_mcp_agent import GenericMCPAgent
from cortex.modules.primary_agent import PrimaryAgent, build_system_prompt
from cortex.modules.task_graph_compiler import RuntimeTask


# ─────────────────────────── helpers ──────────────────────────────────────

def _make_config(wave_gate_provider: str = "default") -> CortexConfig:
    return CortexConfig(
        agent=AgentConfig(
            name="TestAgent",
            description="test agent",
        ),
        llm_access=LLMAccessConfig(
            default=LLMProviderConfig(provider="anthropic", model="x"),
        ),
        storage=StorageConfig(backend="local", base_path="/tmp"),
        validation=ValidationConfig(wave_gate_llm_provider=wave_gate_provider),
    )


def _mk_runtime_task(human_in_loop: bool = False) -> RuntimeTask:
    cfg = TaskTypeConfig(
        name="analyze",
        description="do analysis",
        capability_hint="llm_synthesis",
        human_in_loop=human_in_loop,
    )
    return RuntimeTask(
        task_id="s1/001_analyze",
        task_name="analyze",
        instruction="analyze this",
        depends_on=[],
        depends_on_ids=[],
        input_refs=[],
        config=cfg,
    )


# ────────────────── 1. ValidationConfig schema extension ────────────────────

def test_validation_config_default_wave_gate_provider():
    v = ValidationConfig()
    assert v.wave_gate_llm_provider == "default"


def test_validation_config_accepts_custom_wave_gate_provider():
    v = ValidationConfig(wave_gate_llm_provider="cheap_judge")
    assert v.wave_gate_llm_provider == "cheap_judge"


# ─────────────────── 2. validate_task_output — LLM judge ─────────────────────

@pytest.mark.asyncio
async def test_validate_task_output_pass_verdict_returns_none():
    cfg = _make_config()
    llm = MagicMock()
    llm.complete = AsyncMock(return_value=SimpleNamespace(content='{"verdict": "pass"}'))
    agent = PrimaryAgent(config=cfg, llm_client=llm)

    task = SimpleNamespace(task_name="t", instruction="do it")
    env = SimpleNamespace(content_summary="output OK")

    result = await agent.validate_task_output(task, env, "rule: must not be empty")
    assert result is None
    llm.complete.assert_awaited_once()
    _, kwargs = llm.complete.call_args
    assert kwargs["provider_name"] == "default"


@pytest.mark.asyncio
async def test_validate_task_output_fail_verdict_returns_feedback():
    cfg = _make_config()
    llm = MagicMock()
    llm.complete = AsyncMock(return_value=SimpleNamespace(
        content='{"verdict": "fail", "feedback": "missing title field"}'
    ))
    agent = PrimaryAgent(config=cfg, llm_client=llm)
    task = SimpleNamespace(task_name="t", instruction="do it")
    env = SimpleNamespace(content_summary="bad output")
    fb = await agent.validate_task_output(task, env, "needs title")
    assert fb == "missing title field"


@pytest.mark.asyncio
async def test_validate_task_output_uses_configured_provider():
    cfg = _make_config(wave_gate_provider="cheap_judge")
    llm = MagicMock()
    llm.complete = AsyncMock(return_value=SimpleNamespace(content='{"verdict": "pass"}'))
    agent = PrimaryAgent(config=cfg, llm_client=llm)
    task = SimpleNamespace(task_name="t", instruction="i")
    env = SimpleNamespace(content_summary="c")
    await agent.validate_task_output(task, env, "rules")
    _, kwargs = llm.complete.call_args
    assert kwargs["provider_name"] == "cheap_judge"


@pytest.mark.asyncio
async def test_validate_task_output_fail_without_feedback_gets_default_msg():
    cfg = _make_config()
    llm = MagicMock()
    llm.complete = AsyncMock(return_value=SimpleNamespace(content='{"verdict": "fail"}'))
    agent = PrimaryAgent(config=cfg, llm_client=llm)
    fb = await agent.validate_task_output(
        SimpleNamespace(task_name="t", instruction="i"),
        SimpleNamespace(content_summary="c"),
        "rules",
    )
    assert fb and "validation" in fb.lower()


@pytest.mark.asyncio
async def test_validate_task_output_strips_markdown_fences():
    cfg = _make_config()
    llm = MagicMock()
    llm.complete = AsyncMock(return_value=SimpleNamespace(
        content='```json\n{"verdict": "pass"}\n```'
    ))
    agent = PrimaryAgent(config=cfg, llm_client=llm)
    result = await agent.validate_task_output(
        SimpleNamespace(task_name="t", instruction="i"),
        SimpleNamespace(content_summary="c"),
        "rules",
    )
    assert result is None


@pytest.mark.asyncio
async def test_validate_task_output_llm_exception_passes_gracefully():
    """LLM infra errors must not block the session."""
    cfg = _make_config()
    llm = MagicMock()
    llm.complete = AsyncMock(side_effect=RuntimeError("connection refused"))
    agent = PrimaryAgent(config=cfg, llm_client=llm)
    result = await agent.validate_task_output(
        SimpleNamespace(task_name="t", instruction="i"),
        SimpleNamespace(content_summary="c"),
        "rules",
    )
    assert result is None


@pytest.mark.asyncio
async def test_validate_task_output_malformed_json_passes():
    cfg = _make_config()
    llm = MagicMock()
    llm.complete = AsyncMock(return_value=SimpleNamespace(content="not json at all"))
    agent = PrimaryAgent(config=cfg, llm_client=llm)
    result = await agent.validate_task_output(
        SimpleNamespace(task_name="t", instruction="i"),
        SimpleNamespace(content_summary="c"),
        "rules",
    )
    assert result is None


# ────────────────── 3. Unified sandbox discovery via scout_result ───────────

def test_build_system_prompt_surfaces_code_utils_from_scout():
    cfg = _make_config()
    scout = ScoutResult(
        matched_capabilities=[],
        tools=[],
        code_utils=[
            ScoutedCodeUtil(
                task_name="scrape_site",
                description="Scrape a URL",
                script_path="/tmp/scrape.py",
                use_count=5,
            ),
        ],
    )
    prompt = build_system_prompt(cfg, capabilities=[], scout_result=scout)
    assert "Pre-built Agent Scripts" in prompt
    assert "scrape_site" in prompt
    assert "used 5×" in prompt
    assert "Scrape a URL" in prompt


def test_build_system_prompt_no_scripts_when_scout_empty():
    cfg = _make_config()
    prompt = build_system_prompt(cfg, capabilities=["web_search"], scout_result=None)
    assert "Pre-built Agent Scripts" not in prompt


def test_decompose_signature_has_no_persisted_scripts_param():
    """Regression guard: persisted_scripts must be fully removed."""
    import inspect
    sig = inspect.signature(PrimaryAgent.decompose)
    assert "persisted_scripts" not in sig.parameters


def test_build_system_prompt_signature_has_no_persisted_scripts_param():
    import inspect
    sig = inspect.signature(build_system_prompt)
    assert "persisted_scripts" not in sig.parameters


# ────────────────── 4. Sub-agent HITL decision logic in _call_llm ───────────

class _FakeLLMStream:
    """LLM mock whose stream() returns canned responses one call at a time."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.call_count = 0

    async def _gen(self, text):
        for ch in text:
            yield ch

    def stream(self, messages, system, provider_name):
        idx = self.call_count
        self.call_count += 1
        text = self._responses[idx] if idx < len(self._responses) else ""
        return self._gen(text)


@pytest.mark.asyncio
async def test_call_llm_no_hitl_when_disabled():
    """With human_in_loop=False, ask_human tags are ignored / not parsed."""
    agent = GenericMCPAgent(session_storage_path="/tmp")
    task = _mk_runtime_task(human_in_loop=False)
    fake = _FakeLLMStream(["plain output with no tags"])
    content, usage = await agent._call_llm(
        task_id=task.task_id,
        instruction="do it",
        config=task.config,
        llm_client=fake,
        tool_trace=[],
        task=task,
        event_queue=asyncio.Queue(),
    )
    assert content == "plain output with no tags"
    assert fake.call_count == 1
    assert task.hitl_ask_count == 0


@pytest.mark.asyncio
async def test_call_llm_hitl_single_question_gets_answer_and_resumes():
    agent = GenericMCPAgent(session_storage_path="/tmp")
    task = _mk_runtime_task(human_in_loop=True)

    # First stream emits a question. Second stream emits the final answer.
    fake = _FakeLLMStream([
        "<ask_human>what region?</ask_human>",
        "final answer using us-east-1",
    ])
    queue = asyncio.Queue()

    async def resolver():
        # Wait for the ClarificationRequestEvent, then resolve it.
        from cortex.framework import _PENDING_TASK_CLARIFICATIONS
        for _ in range(100):
            if not queue.empty():
                evt = await queue.get()
                entry = _PENDING_TASK_CLARIFICATIONS.get(evt.clarification_id)
                if entry:
                    entry["answer"] = "us-east-1"
                    entry["event"].set()
                    return
            await asyncio.sleep(0.01)

    resolver_task = asyncio.create_task(resolver())
    content, _ = await agent._call_llm(
        task_id=task.task_id,
        instruction="deploy",
        config=task.config,
        llm_client=fake,
        tool_trace=[],
        task=task,
        event_queue=queue,
    )
    await resolver_task

    assert "final answer using us-east-1" in content
    assert "<ask_human>" not in content
    assert task.hitl_ask_count == 1
    assert fake.call_count == 2


@pytest.mark.asyncio
async def test_call_llm_hitl_ask_cap_enforced_at_3():
    """After 3 asks, the loop must instruct the agent to proceed without asking."""
    agent = GenericMCPAgent(session_storage_path="/tmp")
    task = _mk_runtime_task(human_in_loop=True)
    # 4 asks in a row, then a clean answer. Cap should kick in on the 4th.
    fake = _FakeLLMStream([
        "<ask_human>q1</ask_human>",
        "<ask_human>q2</ask_human>",
        "<ask_human>q3</ask_human>",
        "<ask_human>q4</ask_human>",  # 4th — should hit cap
        "done",
    ])
    queue = asyncio.Queue()

    async def resolver():
        from cortex.framework import _PENDING_TASK_CLARIFICATIONS
        resolved = 0
        while resolved < 3:
            if not queue.empty():
                evt = await queue.get()
                entry = _PENDING_TASK_CLARIFICATIONS.get(evt.clarification_id)
                if entry:
                    entry["answer"] = f"ans{resolved}"
                    entry["event"].set()
                    resolved += 1
            else:
                await asyncio.sleep(0.01)

    resolver_task = asyncio.create_task(resolver())
    content, _ = await agent._call_llm(
        task_id=task.task_id,
        instruction="x",
        config=task.config,
        llm_client=fake,
        tool_trace=[],
        task=task,
        event_queue=queue,
    )
    await resolver_task
    assert task.hitl_ask_count == 3
    assert content == "done"
    # 3 asks + cap-nudge + final = 5 stream calls
    assert fake.call_count == 5


@pytest.mark.asyncio
async def test_call_llm_hitl_no_answer_falls_through():
    """If ask_human times out / returns None, loop injects a fallback and continues."""
    agent = GenericMCPAgent(session_storage_path="/tmp")
    agent.ask_human = AsyncMock(return_value=None)  # simulate timeout
    task = _mk_runtime_task(human_in_loop=True)

    fake = _FakeLLMStream([
        "<ask_human>what now?</ask_human>",
        "proceeded with best guess",
    ])
    content, _ = await agent._call_llm(
        task_id=task.task_id,
        instruction="x",
        config=task.config,
        llm_client=fake,
        tool_trace=[],
        task=task,
        event_queue=asyncio.Queue(),
    )
    assert content == "proceeded with best guess"
    assert task.hitl_ask_count == 1


@pytest.mark.asyncio
async def test_call_llm_hitl_system_prompt_only_when_enabled():
    """The ask_human instructions should only appear in system prompt when HITL is on."""
    agent = GenericMCPAgent(session_storage_path="/tmp")

    captured = {"systems": []}

    class _Capturing:
        async def _gen(self, text):
            for ch in text:
                yield ch

        def stream(self, messages, system, provider_name):
            captured["systems"].append(system)
            return self._gen("ok")

    task_on = _mk_runtime_task(human_in_loop=True)
    task_off = _mk_runtime_task(human_in_loop=False)
    llm = _Capturing()

    await agent._call_llm(
        task_id=task_on.task_id, instruction="i", config=task_on.config,
        llm_client=llm, tool_trace=[], task=task_on, event_queue=asyncio.Queue(),
    )
    await agent._call_llm(
        task_id=task_off.task_id, instruction="i", config=task_off.config,
        llm_client=llm, tool_trace=[], task=task_off, event_queue=asyncio.Queue(),
    )

    assert "<ask_human>" in captured["systems"][0]
    assert "<ask_human>" not in captured["systems"][1]


# ────────────────── 5. RuntimeTask hitl_ask_count snapshot/restore ───────────

def test_runtime_task_default_hitl_ask_count_zero():
    t = RuntimeTask(
        task_id="s/001", task_name="x", instruction="i",
        depends_on=[], depends_on_ids=[], input_refs=[],
    )
    assert t.hitl_ask_count == 0


def test_snapshot_restore_preserves_hitl_ask_count():
    from cortex.modules.task_graph_compiler import (
        DecomposedTask, TaskGraphCompiler,
    )

    compiler = TaskGraphCompiler()
    tt = TaskTypeConfig(name="a", description="A")
    compiled = compiler.compile([tt])
    graph = compiler.instantiate(
        compiled, "sess_test01",
        [DecomposedTask(task_name="a", instruction="do a")],
    )
    tid = next(iter(graph.tasks))
    graph.tasks[tid].hitl_ask_count = 2

    snapshot = compiler.snapshot_graph(graph)
    restored = compiler.restore_graph(snapshot)
    assert restored.tasks[tid].hitl_ask_count == 2
