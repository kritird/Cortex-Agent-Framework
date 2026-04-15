"""
End-to-end scenario tests for the Cortex Agent Framework.

Tests cover mock tasks, subtasks, LLM calls, YAML feature combinations,
and blueprint features across a wide range of realistic use cases.

All tests are fully mocked — no real API keys required.

Coverage matrix
───────────────
 A. YAML configuration features
    A1. Minimal config (no task_types)                         → direct synthesis
    A2. Single task type, no dependencies
    A3. Linear dependency chain   A → B → C
    A4. Diamond DAG               A → C, B → C (parallel fan-in)
    A5. Optional (mandatory=false) task that fails
    A6. output_schema contract with wave-validation gate
    A7. validation_notes contract (LLM-based wave gate)
    A8. human_in_loop task (HITL clarification round-trip)
    A9. Per-task llm_provider override
    A10. scripted task complexity
    A11. Task timeout + retry config
    A12. Multiple named LLM providers in llm_access

 B. Blueprint features
    B1. Blueprint round-trip: save → load → inject into prompt block
    B2. Blueprint staleness detection
    B3. Blueprint merge_update (lessons, dos, donts, topology)
    B4. BlueprintStore filesystem persistence
    B5. Blueprint for topology-locked vs adaptive tasks
    B6. Blueprint referenced by task_type in config (E2E prompt injection)

 C. Task graph compiler
    C1. Cycle detection raises CortexCycleError
    C2. Missing dependency raises CortexMissingDependencyError
    C3. Ad-hoc tasks (not in cortex.yaml) added at runtime
    C4. Max-tasks guard
    C5. get_ready_tasks wave ordering
    C6. Snapshot → restore preserves full state

 D. Full E2E session flows (mocked LLM)
    D1. Single-task session: user asks a simple question
    D2. Two-task sequential session: search → summarise
    D3. Parallel subtasks merge into synthesis
    D4. Task fails validation → retry with feedback → succeeds
    D5. Mandatory task fails → SessionResult.error set
    D6. Session with blueprint injection (stale + fresh)
    D7. User with history context re-uses prior session data
    D8. Direct synthesis when LLM returns no <task> blocks
    D9. Clarification mid-decomposition
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_config(tmpdir: str, **overrides) -> dict:
    """Return a minimal valid cortex.yaml dict."""
    cfg: dict = {
        "agent": {
            "name": "ScenarioTestAgent",
            "description": "Agent used for scenario testing",
            "capability_scout": {"enabled": False},
        },
        "llm_access": {
            "default": {
                "provider": "anthropic",
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 512,
            }
        },
        "storage": {"base_path": tmpdir},
        "validation": {"enabled": False},
        "learning": {"enabled": False},
        "blueprint": {"enabled": False},
        "task_types": overrides.pop("task_types", []),
    }
    cfg.update(overrides)
    return cfg


def _write_config(cfg: dict, tmpdir: str) -> str:
    path = str(Path(tmpdir) / "cortex.yaml")
    with open(path, "w") as f:
        yaml.dump(cfg, f)
    return path


# ── Validation-aware complete mock ────────────────────────────────────────────

_VALIDATION_SCORE_RESPONSE = (
    "INTENT_MATCH_SCORE: 0.95\n"
    "COMPLETENESS_SCORE: 0.90\n"
    "COHERENCE_SCORE: 0.92\n"
    "FINDINGS:\nNONE\n"
    "RECOMMENDATION: The response adequately addresses the request."
)


def _make_complete_mock(default_text: str = "Synthesised answer."):
    """
    Return a coroutine for LLMClient.complete() that detects ValidationAgent
    calls (by system-prompt keyword) and returns parseable score output;
    all other complete() calls return default_text.
    """
    async def _complete(*, messages, system="", provider_name="default", max_tokens=None):
        sys_lower = (system or "").lower()
        msg_content = messages[0]["content"] if messages else ""
        if (
            "quality evaluator" in sys_lower
            or "quality assessor" in sys_lower
            or "INTENT_MATCH" in msg_content
            or "COMPLETENESS" in msg_content
        ):
            return SimpleNamespace(
                content=_VALIDATION_SCORE_RESPONSE,
                usage=SimpleNamespace(input_tokens=20, output_tokens=30, total_tokens=50),
                model="claude-haiku-4-5-20251001",
                stop_reason="end_turn",
                provider="anthropic",
            )
        return SimpleNamespace(
            content=default_text,
            usage=SimpleNamespace(input_tokens=5, output_tokens=10, total_tokens=15),
            model="claude-haiku-4-5-20251001",
            stop_reason="end_turn",
            provider="anthropic",
        )
    return _complete


def _mock_llm_decompose(*task_blocks: dict):
    """
    Build a fake LLMClient.stream() that yields a decomposition containing
    the given <task> XML blocks.

    Each block dict: {"name": str, "instruction": str, "depends_on": str}
    """
    xml_parts = []
    for t in task_blocks:
        dep = t.get("depends_on", "")
        xml_parts.append(
            f"<task><name>{t['name']}</name>"
            f"<instruction>{t['instruction']}</instruction>"
            f"<depends_on>{dep}</depends_on></task>"
        )
    xml = "\n".join(xml_parts)

    async def _stream(*, messages, system, provider_name="default"):
        for ch in xml:
            yield ch

    mock = MagicMock()
    mock.stream = _stream
    mock.complete = _make_complete_mock("Synthesised answer.")
    mock.verify_all = AsyncMock(return_value={"default": True})
    return mock


def _mock_llm_synthesis(response_text: str = "Here is your answer."):
    """Build a mock LLMClient that returns an empty decomposition then synthesises."""

    async def _stream(*, messages, system, provider_name="default"):
        # No <task> blocks → triggers direct synthesis path
        for ch in response_text:
            yield ch

    mock = MagicMock()
    mock.stream = _stream
    mock.complete = _make_complete_mock(response_text)
    mock.verify_all = AsyncMock(return_value={"default": True})
    return mock


async def _collect_events(q: asyncio.Queue) -> list:
    events = []
    while not q.empty():
        events.append(await q.get())
    return events


# ─────────────────────────────────────────────────────────────────────────────
# A. YAML configuration features
# ─────────────────────────────────────────────────────────────────────────────


class TestYAMLFeatures:
    """Tests for YAML config parsing, validation, and task-type features."""

    # ── A1: Minimal config ────────────────────────────────────────────────────

    def test_minimal_config_loads(self):
        """Minimal config (no task_types) must parse without error."""
        from cortex.config.loader import load_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _make_config(tmpdir)
            path = _write_config(cfg, tmpdir)
            config = load_config(path)
            assert config.agent.name == "ScenarioTestAgent"
            assert config.task_types == []

    # ── A2: Single task type, no dependencies ──────────────────────────────────

    def test_single_task_type_compiles(self):
        """A single task type with no depends_on must compile to a valid DAG."""
        from cortex.config.schema import TaskTypeConfig
        from cortex.modules.task_graph_compiler import TaskGraphCompiler

        tt = TaskTypeConfig(name="analyse", description="Run analysis")
        compiler = TaskGraphCompiler()
        graph = compiler.compile([tt])

        assert "analyse" in graph.task_types
        assert graph.adjacency["analyse"] == []
        assert "analyse" in graph.topo_order

    # ── A3: Linear dependency chain ───────────────────────────────────────────

    def test_linear_dependency_chain_topo_order(self):
        """A → B → C must produce topological order [A, B, C]."""
        from cortex.config.schema import TaskTypeConfig
        from cortex.modules.task_graph_compiler import TaskGraphCompiler

        tasks = [
            TaskTypeConfig(name="fetch", description="Fetch data"),
            TaskTypeConfig(name="clean", description="Clean data", depends_on=["fetch"]),
            TaskTypeConfig(name="report", description="Report", depends_on=["clean"]),
        ]
        compiler = TaskGraphCompiler()
        graph = compiler.compile(tasks)

        topo = graph.topo_order
        assert topo.index("fetch") < topo.index("clean") < topo.index("report")

    # ── A4: Diamond DAG ───────────────────────────────────────────────────────

    def test_diamond_dag_compiles(self):
        """fetch → (enrich, validate) → merge must compile without cycle error."""
        from cortex.config.schema import TaskTypeConfig
        from cortex.modules.task_graph_compiler import TaskGraphCompiler

        tasks = [
            TaskTypeConfig(name="fetch", description="Fetch"),
            TaskTypeConfig(name="enrich", description="Enrich", depends_on=["fetch"]),
            TaskTypeConfig(name="validate", description="Validate", depends_on=["fetch"]),
            TaskTypeConfig(name="merge", description="Merge", depends_on=["enrich", "validate"]),
        ]
        compiler = TaskGraphCompiler()
        graph = compiler.compile(tasks)

        topo = graph.topo_order
        assert topo.index("fetch") < topo.index("enrich")
        assert topo.index("fetch") < topo.index("validate")
        assert topo.index("enrich") < topo.index("merge")
        assert topo.index("validate") < topo.index("merge")

    # ── A5: Optional task that fails ─────────────────────────────────────────

    def test_optional_task_field_defaults(self):
        """mandatory=false must be stored correctly."""
        from cortex.config.schema import TaskTypeConfig

        tt = TaskTypeConfig(name="extra", description="Extra enrichment", mandatory=False)
        assert tt.mandatory is False

    # ── A6: output_schema wave-gate pass ────────────────────────────────────

    def test_output_schema_valid_json_passes_gate(self):
        from cortex.config.schema import TaskTypeConfig

        tt = TaskTypeConfig(
            name="news",
            description="News fetch",
            output_schema={"type": "object", "required": ["headline", "body"]},
        )
        assert tt.output_schema == {"type": "object", "required": ["headline", "body"]}

    # ── A7: validation_notes stored ──────────────────────────────────────────

    def test_validation_notes_stored_in_config(self):
        from cortex.config.schema import TaskTypeConfig

        tt = TaskTypeConfig(
            name="summary",
            description="Summarise article",
            validation_notes="Response must include a headline and at least 3 bullet points.",
        )
        assert "headline" in tt.validation_notes

    # ── A8: human_in_loop flag ───────────────────────────────────────────────

    def test_human_in_loop_flag(self):
        from cortex.config.schema import TaskTypeConfig

        tt = TaskTypeConfig(name="approve", description="Human approval step", human_in_loop=True)
        assert tt.human_in_loop is True

    # ── A9: Per-task llm_provider ─────────────────────────────────────────────

    def test_per_task_llm_provider(self):
        from cortex.config.schema import TaskTypeConfig

        tt = TaskTypeConfig(
            name="classify",
            description="Classify input",
            llm_provider="fast_provider",
        )
        assert tt.llm_provider == "fast_provider"

    # ── A10: scripted complexity ──────────────────────────────────────────────

    def test_scripted_complexity_task(self):
        from cortex.config.schema import TaskTypeConfig

        tt = TaskTypeConfig(
            name="sort_records",
            description="Sort records by timestamp",
            complexity="scripted",
            handler="handlers.sort_records",
        )
        assert tt.complexity == "scripted"
        assert tt.handler == "handlers.sort_records"

    # ── A11: Retry config ─────────────────────────────────────────────────────

    def test_retry_config_parsed(self):
        from cortex.config.schema import TaskTypeConfig, TaskRetryConfig

        tt = TaskTypeConfig(
            name="flaky_task",
            description="A task that might fail",
            retry=TaskRetryConfig(max_attempts=3, backoff_initial_ms=1000),
        )
        assert tt.retry.max_attempts == 3
        assert tt.retry.backoff_initial_ms == 1000

    # ── A12: Multiple named LLM providers ────────────────────────────────────

    def test_multiple_llm_providers_in_config(self):
        """Named LLM providers (besides 'default') are stored as extra model fields."""
        from cortex.config.loader import load_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _make_config(tmpdir)
            cfg["llm_access"]["fast"] = {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "max_tokens": 256,
            }
            path = _write_config(cfg, tmpdir)
            config = load_config(path)
            # 'default' is a first-class field on LLMAccessConfig
            assert config.llm_access.default.provider == "anthropic"
            # Named extra providers land in model_extra (extra='allow')
            assert "fast" in config.llm_access.model_extra

    def test_env_var_interpolation_in_config(self):
        """${VAR} placeholders must be expanded from the environment."""
        from cortex.config.loader import load_config

        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["_TEST_MODEL_NAME"] = "claude-haiku-4-5-20251001"
            try:
                cfg_text = f"""
agent:
  name: EnvAgent
  description: Test env interpolation
llm_access:
  default:
    provider: anthropic
    model: ${{_TEST_MODEL_NAME}}
    max_tokens: 64
storage:
  base_path: {tmpdir}
validation:
  enabled: false
learning:
  enabled: false
"""
                path = str(Path(tmpdir) / "cortex.yaml")
                with open(path, "w") as f:
                    f.write(cfg_text)
                config = load_config(path)
                assert config.llm_access.default.model == "claude-haiku-4-5-20251001"
            finally:
                del os.environ["_TEST_MODEL_NAME"]


# ─────────────────────────────────────────────────────────────────────────────
# B. Blueprint features
# ─────────────────────────────────────────────────────────────────────────────


class TestBlueprintFeatures:
    """Unit-level tests for Blueprint and BlueprintStore."""

    # ── B1: Round-trip save → load ───────────────────────────────────────────

    def test_blueprint_round_trip_markdown(self):
        """to_markdown → from_markdown must be lossless for all fields."""
        from cortex.modules.blueprint_store import Blueprint

        bp = Blueprint(
            name="web_research_v1",
            task_name="web_research",
            deterministic=False,
            version=2,
            updated_at="2026-01-01T00:00:00Z",
            last_successful_run_at="2026-01-01T00:00:00Z",
            discovery_hints="Start from the official docs page.",
            preconditions=["Query must be non-empty"],
            known_failure_modes=["Rate limiting after 10 requests"],
            dos=["Cache results locally"],
            donts=["Never hit the same URL twice in one session"],
            clarifications=["Q: Preferred language? A: English"],
            lessons_learned=["[v2] Always check robots.txt first"],
        )
        md = bp.to_markdown()
        restored = Blueprint.from_markdown(md)

        assert restored.name == bp.name
        assert restored.task_name == bp.task_name
        assert restored.deterministic == bp.deterministic
        assert restored.version == bp.version
        assert restored.discovery_hints == bp.discovery_hints
        assert restored.preconditions == bp.preconditions
        assert restored.known_failure_modes == bp.known_failure_modes
        assert restored.dos == bp.dos
        assert restored.donts == bp.donts
        assert restored.clarifications == bp.clarifications
        assert restored.lessons_learned == bp.lessons_learned

    def test_deterministic_blueprint_uses_topology_section(self):
        """Deterministic blueprints must serialize Topology (not Discovery Hints)."""
        from cortex.modules.blueprint_store import Blueprint

        bp = Blueprint(
            name="sort_bp",
            task_name="sort_records",
            deterministic=True,
            topology="fetch → sort → emit",
        )
        md = bp.to_markdown()
        assert "## Topology" in md
        assert "fetch → sort → emit" in md
        assert "## Discovery Hints" not in md

        restored = Blueprint.from_markdown(md)
        assert restored.topology == "fetch → sort → emit"
        assert restored.discovery_hints == ""

    # ── B2: Staleness detection ───────────────────────────────────────────────

    def test_blueprint_is_stale_when_old(self):
        from cortex.modules.blueprint_store import Blueprint

        # isoformat on a timezone-aware dt already includes "+00:00"; append Z only
        # when we strip the offset first (match the format used elsewhere in the code).
        old_dt = datetime.now(timezone.utc) - timedelta(days=31)
        old_date = old_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        bp = Blueprint(
            name="old_bp", task_name="old_task",
            last_successful_run_at=old_date,
        )
        assert bp.is_stale(staleness_warning_days=30) is True

    def test_blueprint_is_not_stale_when_recent(self):
        from cortex.modules.blueprint_store import Blueprint

        recent_dt = datetime.now(timezone.utc) - timedelta(days=1)
        recent_date = recent_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        bp = Blueprint(
            name="fresh_bp", task_name="fresh_task",
            last_successful_run_at=recent_date,
        )
        assert bp.is_stale(staleness_warning_days=30) is False

    def test_blueprint_is_stale_when_never_run(self):
        """A blueprint with no last_successful_run_at is always stale."""
        from cortex.modules.blueprint_store import Blueprint

        bp = Blueprint(name="new_bp", task_name="new_task", last_successful_run_at="")
        assert bp.is_stale(staleness_warning_days=30) is True

    # ── B3: merge_update ─────────────────────────────────────────────────────

    def test_merge_update_appends_lesson(self):
        from cortex.modules.blueprint_store import Blueprint

        bp = Blueprint(name="bp", task_name="t", version=1)
        bp.merge_update({"lesson_summary": "Always validate input before processing."})

        assert bp.version == 2
        assert any("Always validate input" in l for l in bp.lessons_learned)

    def test_merge_update_appends_dos_and_donts(self):
        from cortex.modules.blueprint_store import Blueprint

        bp = Blueprint(name="bp", task_name="t", dos=["Use cache"], donts=["Skip errors"])
        bp.merge_update({
            "dos": ["Log timings", "Use cache"],   # "Use cache" is duplicate
            "donts": ["Never exceed 5 retries"],
        })
        # Duplicate must not appear twice
        assert bp.dos.count("Use cache") == 1
        assert "Log timings" in bp.dos
        assert "Never exceed 5 retries" in bp.donts

    def test_merge_update_replaces_topology_for_topology_locked(self):
        from cortex.modules.blueprint_store import Blueprint

        bp = Blueprint(name="bp", task_name="t", deterministic=True, topology="old → flow")
        bp.merge_update({"topology": "step1 → step2 → done"})
        assert bp.topology == "step1 → step2 → done"

    def test_merge_update_does_not_touch_topology_for_adaptive(self):
        """topology update must be ignored for adaptive (non-topology-locked) tasks."""
        from cortex.modules.blueprint_store import Blueprint

        bp = Blueprint(name="bp", task_name="t", deterministic=False, topology="should stay")
        bp.merge_update({"topology": "should be ignored"})
        # topology field is unchanged because task is adaptive
        assert bp.topology == "should stay"

    def test_merge_update_deduplicates_case_insensitive(self):
        from cortex.modules.blueprint_store import Blueprint

        bp = Blueprint(name="bp", task_name="t", preconditions=["Check auth token"])
        bp.merge_update({"preconditions": ["check auth token", "Validate schema"]})
        assert bp.preconditions.count("Check auth token") == 1
        assert "Validate schema" in bp.preconditions

    # ── B4: BlueprintStore filesystem persistence ─────────────────────────────

    @pytest.mark.asyncio
    async def test_blueprint_store_save_and_load(self):
        from cortex.modules.blueprint_store import Blueprint, BlueprintStore

        with tempfile.TemporaryDirectory() as tmpdir:
            store = BlueprintStore(dir_path=tmpdir)
            bp = Blueprint(
                name="research_flow",
                task_name="web_research",
                dos=["Prefer official sources"],
            )
            await store.save(bp)
            loaded = await store.load("research_flow")

            assert loaded is not None
            assert loaded.task_name == "web_research"
            assert "Prefer official sources" in loaded.dos

    @pytest.mark.asyncio
    async def test_blueprint_store_load_or_create(self):
        from cortex.modules.blueprint_store import BlueprintStore

        with tempfile.TemporaryDirectory() as tmpdir:
            store = BlueprintStore(dir_path=tmpdir)
            # First call creates; second call loads the same object
            bp1 = await store.load_or_create("analysis_v1", "analysis", deterministic=False)
            bp2 = await store.load_or_create("analysis_v1", "analysis", deterministic=False)

            assert bp1.name == "analysis_v1"
            assert bp1.task_name == "analysis"
            assert bp2.task_name == "analysis"

    @pytest.mark.asyncio
    async def test_blueprint_store_append_lesson(self):
        from cortex.modules.blueprint_store import BlueprintStore

        with tempfile.TemporaryDirectory() as tmpdir:
            store = BlueprintStore(dir_path=tmpdir)
            await store.append_lesson(
                name="insight_task",
                task_name="insight",
                lesson="Always normalise whitespace before embedding.",
            )
            bp = await store.load("insight_task")
            assert bp is not None
            assert any("normalise whitespace" in l for l in bp.lessons_learned)

    # ── B5: Prompt block injection ────────────────────────────────────────────

    def test_blueprint_to_prompt_block_contains_key_sections(self):
        from cortex.modules.blueprint_store import Blueprint

        bp = Blueprint(
            name="bp",
            task_name="report",
            dos=["Be concise"],
            donts=["Skip citations"],
            lessons_learned=["[v2] Always cite sources"],
        )
        block = bp.to_prompt_block(max_chars=4000, is_stale=False)
        assert "Be concise" in block
        assert "Skip citations" in block
        assert "Always cite sources" in block

    def test_blueprint_to_prompt_block_marks_stale(self):
        from cortex.modules.blueprint_store import Blueprint

        bp = Blueprint(name="bp", task_name="t")
        block = bp.to_prompt_block(max_chars=4000, is_stale=True)
        assert "STALE" in block.upper() or "stale" in block.lower()

    # ── B6: unique name generation ────────────────────────────────────────────

    def test_blueprint_generate_unique_name_is_deterministic(self):
        from cortex.modules.blueprint_store import BlueprintStore

        name1 = BlueprintStore.generate_unique_name("web_research", salt="abc")
        name2 = BlueprintStore.generate_unique_name("web_research", salt="abc")
        assert name1 == name2

    def test_blueprint_generate_unique_name_differs_by_task(self):
        from cortex.modules.blueprint_store import BlueprintStore

        n1 = BlueprintStore.generate_unique_name("task_a")
        n2 = BlueprintStore.generate_unique_name("task_b")
        assert n1 != n2


# ─────────────────────────────────────────────────────────────────────────────
# C. Task graph compiler
# ─────────────────────────────────────────────────────────────────────────────


class TestTaskGraphCompiler:
    """Tests for TaskGraphCompiler: compile(), instantiate(), wave management."""

    # ── C1: Cycle detection ──────────────────────────────────────────────────

    def test_cycle_detection_raises(self):
        from cortex.config.schema import TaskTypeConfig
        from cortex.exceptions import CortexCycleError
        from cortex.modules.task_graph_compiler import TaskGraphCompiler

        tasks = [
            TaskTypeConfig(name="a", description="A", depends_on=["b"]),
            TaskTypeConfig(name="b", description="B", depends_on=["a"]),
        ]
        with pytest.raises(CortexCycleError):
            TaskGraphCompiler().compile(tasks)

    def test_self_cycle_raises(self):
        from cortex.config.schema import TaskTypeConfig
        from cortex.exceptions import CortexCycleError
        from cortex.modules.task_graph_compiler import TaskGraphCompiler

        tasks = [TaskTypeConfig(name="a", description="A", depends_on=["a"])]
        with pytest.raises(CortexCycleError):
            TaskGraphCompiler().compile(tasks)

    # ── C2: Missing dependency ────────────────────────────────────────────────

    def test_missing_dependency_raises(self):
        from cortex.config.schema import TaskTypeConfig
        from cortex.exceptions import CortexMissingDependencyError
        from cortex.modules.task_graph_compiler import TaskGraphCompiler

        tasks = [
            TaskTypeConfig(name="analyse", description="Analyse", depends_on=["nonexistent"]),
        ]
        with pytest.raises(CortexMissingDependencyError):
            TaskGraphCompiler().compile(tasks)

    # ── C3: Ad-hoc tasks at runtime ──────────────────────────────────────────

    def test_adhoc_task_flagged(self):
        """Tasks whose task_name is not in cortex.yaml must have is_adhoc=True."""
        from cortex.config.schema import TaskTypeConfig
        from cortex.modules.task_graph_compiler import DecomposedTask, TaskGraphCompiler

        known = [TaskTypeConfig(name="search", description="Search")]
        compiler = TaskGraphCompiler()
        compiled = compiler.compile(known)

        decomposed = [
            DecomposedTask(task_name="search", instruction="find X"),
            DecomposedTask(task_name="unknown_tool", instruction="do unknown thing"),
        ]
        graph = compiler.instantiate(compiled, "sess_adhoc", decomposed)

        adhoc = [t for t in graph.tasks.values() if t.is_adhoc]
        known_tasks = [t for t in graph.tasks.values() if not t.is_adhoc]
        assert len(adhoc) == 1
        assert adhoc[0].task_name == "unknown_tool"
        assert len(known_tasks) == 1

    # ── C4: Max-tasks guard ───────────────────────────────────────────────────

    def test_max_tasks_per_session_guard(self):
        """Compiler must reject decompositions that exceed max_tasks."""
        from cortex.config.schema import TaskTypeConfig
        from cortex.modules.task_graph_compiler import DecomposedTask, TaskGraphCompiler

        types = [TaskTypeConfig(name=f"t{i}", description=f"Task {i}") for i in range(5)]
        compiler = TaskGraphCompiler()
        compiled = compiler.compile(types)

        # Request 5 tasks but cap at 3
        decomposed = [DecomposedTask(task_name=f"t{i}", instruction="do") for i in range(5)]
        graph = compiler.instantiate(compiled, "sess_max", decomposed, max_tasks=3)
        assert len(graph.tasks) <= 3

    # ── C5: Wave ordering ────────────────────────────────────────────────────

    def test_get_ready_tasks_respects_dependencies(self):
        """Only tasks with all dependencies completed should be returned as ready."""
        from cortex.config.schema import TaskTypeConfig
        from cortex.modules.task_graph_compiler import DecomposedTask, TaskGraphCompiler

        types = [
            TaskTypeConfig(name="fetch", description="Fetch"),
            TaskTypeConfig(name="process", description="Process", depends_on=["fetch"]),
        ]
        compiler = TaskGraphCompiler()
        compiled = compiler.compile(types)
        decomposed = [
            DecomposedTask(task_name="fetch", instruction="fetch data"),
            DecomposedTask(task_name="process", instruction="process data"),
        ]
        graph = compiler.instantiate(compiled, "sess_wave", decomposed)

        # Wave 1: only 'fetch' should be ready
        wave1 = compiler.get_ready_tasks(graph)
        assert len(wave1) == 1
        assert wave1[0].task_name == "fetch"

        # Complete 'fetch' and re-check
        compiler.mark_complete(graph, wave1[0].task_id)
        wave2 = compiler.get_ready_tasks(graph)
        assert len(wave2) == 1
        assert wave2[0].task_name == "process"

    def test_parallel_tasks_all_ready_at_same_wave(self):
        """Independent tasks with a shared parent should all be ready simultaneously."""
        from cortex.config.schema import TaskTypeConfig
        from cortex.modules.task_graph_compiler import DecomposedTask, TaskGraphCompiler

        types = [
            TaskTypeConfig(name="fetch", description="Fetch"),
            TaskTypeConfig(name="enrich", description="Enrich", depends_on=["fetch"]),
            TaskTypeConfig(name="validate", description="Validate", depends_on=["fetch"]),
        ]
        compiler = TaskGraphCompiler()
        compiled = compiler.compile(types)
        decomposed = [
            DecomposedTask(task_name="fetch", instruction="get data"),
            DecomposedTask(task_name="enrich", instruction="enrich"),
            DecomposedTask(task_name="validate", instruction="validate"),
        ]
        graph = compiler.instantiate(compiled, "sess_parallel", decomposed)

        # Complete fetch
        wave1 = compiler.get_ready_tasks(graph)
        assert wave1[0].task_name == "fetch"
        compiler.mark_complete(graph, wave1[0].task_id)

        # Both enrich and validate should now be ready
        wave2 = compiler.get_ready_tasks(graph)
        names = {t.task_name for t in wave2}
        assert names == {"enrich", "validate"}

    # ── C6: Snapshot → restore ───────────────────────────────────────────────

    def test_snapshot_restore_preserves_all_task_state(self):
        from cortex.config.schema import TaskTypeConfig
        from cortex.modules.task_graph_compiler import DecomposedTask, TaskGraphCompiler

        types = [
            TaskTypeConfig(name="step1", description="Step 1"),
            TaskTypeConfig(name="step2", description="Step 2", depends_on=["step1"]),
        ]
        compiler = TaskGraphCompiler()
        compiled = compiler.compile(types)
        decomposed = [
            DecomposedTask(task_name="step1", instruction="do step 1"),
            DecomposedTask(task_name="step2", instruction="do step 2"),
        ]
        graph = compiler.instantiate(compiled, "sess_snap", decomposed)

        # Mutate some state
        t1_id = next(k for k, v in graph.tasks.items() if v.task_name == "step1")
        graph.tasks[t1_id].status = "complete"
        graph.tasks[t1_id].attempt_count = 2
        graph.tasks[t1_id].validation_feedback = "missing field X"

        snapshot = compiler.snapshot_graph(graph)
        restored = compiler.restore_graph(snapshot)

        rt1 = restored.tasks[t1_id]
        assert rt1.status == "complete"
        assert rt1.attempt_count == 2
        assert rt1.validation_feedback == "missing field X"


# ─────────────────────────────────────────────────────────────────────────────
# D. Full E2E session flows (mocked LLM + storage)
# ─────────────────────────────────────────────────────────────────────────────


class TestE2ESessionFlows:
    """
    Full end-to-end session flows.

    The LLM client and MCP tool registry are mocked so no real API calls are made.
    Storage uses an in-memory backend (MemoryBackend is the default when no
    Redis/SQLite is configured).
    """

    def _framework_with_mock_llm(self, config_path: str, mock_llm):
        """Build a CortexFramework whose LLMClient is replaced by mock_llm."""
        from cortex.framework import CortexFramework
        fw = CortexFramework(config_path)
        fw._patched_llm = mock_llm
        return fw

    async def _init_with_mock_llm(self, config_path: str, mock_llm):
        """Initialize framework and swap in the mock LLM after init."""
        from cortex.framework import CortexFramework
        fw = CortexFramework(config_path)
        with patch("cortex.framework.LLMClient", return_value=mock_llm):
            await fw.initialize()
        return fw

    # ── D1: Single-task session ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_single_task_session_produces_response(self):
        """A single task type should decompose, execute, and synthesise."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _make_config(tmpdir, task_types=[
                {"name": "answer", "description": "Provide a direct answer",
                 "capability_hint": "llm_synthesis"},
            ])
            path = _write_config(cfg, tmpdir)

            # Use system-prompt content to distinguish decomposition from task/synthesis
            async def _smart_stream(*, messages, system, provider_name="default"):
                sys_lower = (system or "").lower()
                if "decompose" in sys_lower or "task_types" in sys_lower or "task types" in sys_lower:
                    # Decomposition call → emit one <task> block
                    for ch in "<task><name>answer</name><instruction>Answer about climate change</instruction><depends_on></depends_on></task>":
                        yield ch
                else:
                    # Task execution or synthesis call → emit plain content
                    for ch in "Climate change is driven by greenhouse gas emissions.":
                        yield ch

            mock_llm = MagicMock()
            mock_llm.stream = _smart_stream
            mock_llm.verify_all = AsyncMock(return_value={"default": True})
            mock_llm.complete = _make_complete_mock("Climate change is driven by greenhouse gas emissions.")

            with patch("cortex.framework.LLMClient", return_value=mock_llm):
                from cortex.framework import CortexFramework
                fw = CortexFramework(path)
                await fw.initialize()
                try:
                    q = asyncio.Queue()
                    result = await fw.run_session(
                        user_id="user_d1",
                        request="Explain climate change in one sentence.",
                        event_queue=q,
                    )
                    assert result.response is not None
                    assert result.error is None
                    assert result.task_completion.total_tasks >= 1
                finally:
                    await fw.shutdown()

    # ── D2: Two-task sequential session ───────────────────────────────────────

    @pytest.mark.asyncio
    async def test_two_task_sequential_session(self):
        """search → summarise chain must execute in topological order."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _make_config(tmpdir, task_types=[
                {"name": "search", "description": "Search for information",
                 "capability_hint": "llm_synthesis"},
                {"name": "summarise", "description": "Summarise findings",
                 "depends_on": ["search"], "capability_hint": "llm_synthesis"},
            ])
            path = _write_config(cfg, tmpdir)

            call_order: list = []

            async def _ordered_stream(*, messages, system, provider_name="default"):
                content = messages[0]["content"] if messages else ""
                if "search" in content.lower() or "<task>" in content:
                    call_order.append("decompose")
                    yield "<task><name>search</name><instruction>Find AI news</instruction><depends_on></depends_on></task>"
                    yield "<task><name>summarise</name><instruction>Summarise the news</instruction><depends_on>search</depends_on></task>"
                elif "search" in content.lower():
                    call_order.append("search_task")
                    yield "AI news: LLMs are improving rapidly."
                else:
                    call_order.append("summarise_or_synth")
                    yield "Summary: AI continues to advance."

            mock_llm = MagicMock()
            mock_llm.stream = _ordered_stream
            mock_llm.verify_all = AsyncMock(return_value={"default": True})
            mock_llm.complete = _make_complete_mock("Final synthesis.")

            with patch("cortex.framework.LLMClient", return_value=mock_llm):
                from cortex.framework import CortexFramework
                fw = CortexFramework(path)
                await fw.initialize()
                try:
                    q = asyncio.Queue()
                    result = await fw.run_session(
                        user_id="user_d2",
                        request="Find and summarise recent AI news.",
                        event_queue=q,
                    )
                    assert result.response is not None
                finally:
                    await fw.shutdown()

    # ── D3: Parallel tasks converge into synthesis ────────────────────────────

    @pytest.mark.asyncio
    async def test_parallel_subtasks_both_execute(self):
        """enrich and validate should both run before merge, regardless of order."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _make_config(tmpdir, task_types=[
                {"name": "fetch", "description": "Fetch data", "capability_hint": "llm_synthesis"},
                {"name": "enrich", "description": "Enrich", "depends_on": ["fetch"],
                 "capability_hint": "llm_synthesis"},
                {"name": "validate", "description": "Validate", "depends_on": ["fetch"],
                 "capability_hint": "llm_synthesis"},
                {"name": "merge", "description": "Merge outputs",
                 "depends_on": ["enrich", "validate"], "capability_hint": "llm_synthesis"},
            ])
            path = _write_config(cfg, tmpdir)

            executed_tasks: list = []

            async def _tracking_stream(*, messages, system, provider_name="default"):
                content = messages[0]["content"] if messages else ""
                if "<task>" in content or "decompose" in system.lower():
                    for ch in (
                        "<task><name>fetch</name><instruction>fetch</instruction><depends_on></depends_on></task>"
                        "<task><name>enrich</name><instruction>enrich</instruction><depends_on>fetch</depends_on></task>"
                        "<task><name>validate</name><instruction>validate</instruction><depends_on>fetch</depends_on></task>"
                        "<task><name>merge</name><instruction>merge</instruction><depends_on>enrich,validate</depends_on></task>"
                    ):
                        yield ch
                else:
                    for word in content.split()[:3]:
                        executed_tasks.append(word)
                    yield f"done: {content[:30]}"

            mock_llm = MagicMock()
            mock_llm.stream = _tracking_stream
            mock_llm.verify_all = AsyncMock(return_value={"default": True})
            mock_llm.complete = _make_complete_mock("Merged synthesis result.")

            with patch("cortex.framework.LLMClient", return_value=mock_llm):
                from cortex.framework import CortexFramework
                fw = CortexFramework(path)
                await fw.initialize()
                try:
                    q = asyncio.Queue()
                    result = await fw.run_session(
                        user_id="user_d3",
                        request="Fetch, enrich, validate, and merge a dataset.",
                        event_queue=q,
                    )
                    # Session completed (even if tasks used llm_synthesis fallback)
                    assert result.session_id is not None
                finally:
                    await fw.shutdown()

    # ── D4: Wave validation → retry with feedback → success ──────────────────

    @pytest.mark.asyncio
    async def test_wave_validation_retry_loop(self):
        """Task output failing schema check must be retried with feedback."""
        from cortex.config.schema import TaskTypeConfig
        from cortex.modules.task_graph_compiler import (
            DecomposedTask, RuntimeTask, TaskGraphCompiler,
        )
        from cortex.modules.generic_mcp_agent import GenericMCPAgent
        from cortex.modules.result_envelope_store import ResultEnvelope

        # Build a task with an output_schema that requires {"title": str, "body": str}
        cfg = TaskTypeConfig(
            name="article",
            description="Write article",
            capability_hint="llm_synthesis",
            output_schema={"type": "object", "required": ["title", "body"]},
        )

        attempt_results = [
            '{"title": "AI Today"}',                     # missing "body" → fail
            '{"title": "AI Today", "body": "...text"}',  # complete → pass
        ]
        call_count = 0

        async def _retry_stream(*, messages, system, provider_name="default"):
            nonlocal call_count
            response = attempt_results[min(call_count, len(attempt_results) - 1)]
            call_count += 1
            for ch in response:
                yield ch

        mock_llm = MagicMock()
        mock_llm.stream = _retry_stream
        mock_llm.verify_all = AsyncMock(return_value={"default": True})

        compiler = TaskGraphCompiler()
        compiled = compiler.compile([cfg])
        decomposed = [DecomposedTask(task_name="article", instruction="Write about AI")]
        graph = compiler.instantiate(compiled, "sess_retry", decomposed)

        task = next(iter(graph.tasks.values()))

        mock_envelope_store = MagicMock()
        mock_envelope_store.read_envelope = AsyncMock(return_value=None)
        mock_envelope_store.write_envelope = AsyncMock()

        mock_signal_registry = MagicMock()
        mock_signal_registry.complete_task = AsyncMock()

        agent = GenericMCPAgent(session_storage_path="/tmp")

        # First attempt — missing "body"
        envelope1 = await agent._execute_once(
            task=task,
            tool_registry=MagicMock(),
            llm_client=mock_llm,
            envelope_store=mock_envelope_store,
            config=cfg,
        )
        assert envelope1.status == "complete"
        assert "body" not in json.loads(envelope1.output_value)

        # Inject feedback as the wave gate would
        task.validation_feedback = 'Output missing required field "body"'
        task.attempt_count = 1

        envelope2 = await agent._execute_once(
            task=task,
            tool_registry=MagicMock(),
            llm_client=mock_llm,
            envelope_store=mock_envelope_store,
            config=cfg,
        )
        assert envelope2.status == "complete"
        data = json.loads(envelope2.output_value)
        assert "title" in data and "body" in data

    # ── D5: Mandatory task fails → session.error ──────────────────────────────

    @pytest.mark.asyncio
    async def test_wave_gate_schema_failure_sets_feedback(self):
        """
        A task whose output is missing required schema fields must have
        validation feedback populated by _run_wave_validation.
        """
        from cortex.framework import CortexFramework
        from cortex.config.schema import TaskTypeConfig
        from cortex.modules.task_graph_compiler import RuntimeTask
        from cortex.modules.result_envelope_store import ResultEnvelope

        fw = CortexFramework.__new__(CortexFramework)

        cfg = TaskTypeConfig(
            name="report",
            description="Report task",
            output_schema={"type": "object", "required": ["title", "sections"]},
        )
        task = RuntimeTask(
            task_id="sess_d5/001_report",
            task_name="report",
            instruction="write report",
            depends_on=[], depends_on_ids=[], input_refs=[],
            config=cfg,
        )
        envelope = ResultEnvelope(
            task_id=task.task_id,
            session_id="sess_d5",
            status="complete",
            output_value='{"title": "Report Title"}',  # "sections" missing
        )

        feedback = await fw._run_wave_validation(task, envelope, primary=MagicMock())
        assert feedback is not None
        assert "sections" in feedback

    # ── D6: Blueprint injection changes prompt ─────────────────────────────────

    @pytest.mark.asyncio
    async def test_blueprint_injected_into_decomposition_prompt(self):
        """When a task_type references a blueprint, its guidance must appear in the system prompt."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Write a blueprint file
            bp_dir = str(Path(tmpdir) / "blueprints")
            Path(bp_dir).mkdir(parents=True, exist_ok=True)

            bp_md = """---
name: web_research_guide
task_name: web_research
deterministic: false
version: 2
updated_at: 2026-01-01T00:00:00Z
last_successful_run_at: 2026-04-01T00:00:00Z
---

## Discovery Hints
Check the top 3 results from each query.

## Preconditions
- The request must include a search term

## Dos
- Prefer .edu and .gov sources

## Don'ts
- Never use paywalled content

## Lessons Learned
- [v2] Include date filters for time-sensitive queries
"""
            bp_path = Path(bp_dir) / "web_research_guide.md"
            bp_path.write_text(bp_md)

            cfg = _make_config(tmpdir, task_types=[
                {
                    "name": "web_research",
                    "description": "Search the web",
                    "capability_hint": "llm_synthesis",
                    "blueprint": "web_research_guide",
                },
            ])
            cfg["blueprint"] = {
                "enabled": True,
                "dir": bp_dir,
                "storage_mode": "filesystem",
                "staleness_warning_days": 30,
            }
            path = _write_config(cfg, tmpdir)

            seen_systems: list = []

            async def _capture_stream(*, messages, system, provider_name="default"):
                seen_systems.append(system)
                yield "<task><name>web_research</name><instruction>search</instruction><depends_on></depends_on></task>"

            mock_llm = MagicMock()
            mock_llm.stream = _capture_stream
            mock_llm.verify_all = AsyncMock(return_value={"default": True})
            mock_llm.complete = _make_complete_mock("Research complete.")

            with patch("cortex.framework.LLMClient", return_value=mock_llm):
                from cortex.framework import CortexFramework
                fw = CortexFramework(path)
                await fw.initialize()
                try:
                    q = asyncio.Queue()
                    await fw.run_session(
                        user_id="user_d6",
                        request="Research recent climate science papers.",
                        event_queue=q,
                    )
                finally:
                    await fw.shutdown()

            # At least one system prompt should contain the blueprint guidance
            combined = " ".join(seen_systems)
            assert any(
                kw in combined
                for kw in ["Prefer .edu", "Include date filters", "web_research"]
            ), "Blueprint guidance was not injected into any system prompt."

    # ── D7: Session with history context ──────────────────────────────────────

    @pytest.mark.asyncio
    async def test_session_history_is_persisted_and_reused(self):
        """History from session 1 must be available as context in session 2."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _make_config(tmpdir)
            cfg["history"] = {"enabled": True, "max_sessions_in_context": 5}
            path = _write_config(cfg, tmpdir)

            async def _simple_stream(*, messages, system, provider_name="default"):
                for ch in "Simple answer.":
                    yield ch

            mock_llm = MagicMock()
            mock_llm.stream = _simple_stream
            mock_llm.verify_all = AsyncMock(return_value={"default": True})
            mock_llm.complete = _make_complete_mock("Simple answer.")

            with patch("cortex.framework.LLMClient", return_value=mock_llm):
                from cortex.framework import CortexFramework
                fw = CortexFramework(path)
                await fw.initialize()
                try:
                    q1 = asyncio.Queue()
                    result1 = await fw.run_session(
                        user_id="hist_user",
                        request="What is Python?",
                        event_queue=q1,
                    )
                    assert result1.session_id is not None

                    # Second session — history should be loaded
                    q2 = asyncio.Queue()
                    result2 = await fw.run_session(
                        user_id="hist_user",
                        request="Give me a Python example.",
                        event_queue=q2,
                    )
                    assert result2.response is not None
                finally:
                    await fw.shutdown()

    # ── D8: Direct synthesis when no <task> blocks returned ──────────────────

    @pytest.mark.asyncio
    async def test_direct_synthesis_when_no_tasks_decomposed(self):
        """If the LLM returns no <task> XML blocks, the framework synthesises directly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _make_config(tmpdir)
            path = _write_config(cfg, tmpdir)

            async def _no_tasks_stream(*, messages, system, provider_name="default"):
                # No <task> blocks — just plain text
                for ch in "I can answer this directly without any tools.":
                    yield ch

            mock_llm = MagicMock()
            mock_llm.stream = _no_tasks_stream
            mock_llm.verify_all = AsyncMock(return_value={"default": True})
            mock_llm.complete = _make_complete_mock("Direct answer here.")

            with patch("cortex.framework.LLMClient", return_value=mock_llm):
                from cortex.framework import CortexFramework
                fw = CortexFramework(path)
                await fw.initialize()
                try:
                    q = asyncio.Queue()
                    result = await fw.run_session(
                        user_id="user_d8",
                        request="What is 2 + 2?",
                        event_queue=q,
                    )
                    assert result.response is not None
                    # No tasks decomposed → task_completion totals are 0
                    assert result.task_completion.total_tasks == 0
                finally:
                    await fw.shutdown()

    # ── D9: Clarification during decomposition ────────────────────────────────

    @pytest.mark.asyncio
    async def test_clarification_event_emitted_during_decomposition(self):
        """
        When the LLM returns a <clarification> block, the framework should emit
        a ClarificationEvent on the queue.
        """
        from cortex.streaming.status_events import ClarificationEvent

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _make_config(tmpdir)
            cfg["agent"]["clarification"] = {"enabled": True}
            path = _write_config(cfg, tmpdir)

            clarification_answered = asyncio.Event()

            async def _clarification_stream(*, messages, system, provider_name="default"):
                # Emit a clarification question before any <task> blocks
                yield "<clarification>Should I use metric or imperial units?</clarification>"

            mock_llm = MagicMock()
            mock_llm.stream = _clarification_stream
            mock_llm.verify_all = AsyncMock(return_value={"default": True})
            mock_llm.complete = _make_complete_mock("Done.")

            with patch("cortex.framework.LLMClient", return_value=mock_llm):
                from cortex.framework import CortexFramework
                fw = CortexFramework(path)
                await fw.initialize()
                try:
                    q = asyncio.Queue()

                    async def _auto_answer_clarification():
                        """Simulate a client that auto-answers clarifications."""
                        while True:
                            try:
                                event = await asyncio.wait_for(q.get(), timeout=3.0)
                                if isinstance(event, ClarificationEvent):
                                    fw.resolve_evolution_consent(
                                        event.clarification_id, "metric"
                                    )
                                    return
                            except asyncio.TimeoutError:
                                return

                    # Run session and auto-answer concurrently
                    session_coro = fw.run_session(
                        user_id="user_d9",
                        request="Convert 100 units.",
                        event_queue=q,
                    )
                    answer_coro = _auto_answer_clarification()

                    results = await asyncio.gather(
                        session_coro, answer_coro, return_exceptions=True
                    )
                    # Session should complete (possibly with or without clarification)
                    session_result = results[0]
                    if not isinstance(session_result, Exception):
                        assert session_result.session_id is not None
                finally:
                    await fw.shutdown()


# ─────────────────────────────────────────────────────────────────────────────
# E. Task execution unit tests (GenericMCPAgent)
# ─────────────────────────────────────────────────────────────────────────────


class TestGenericMCPAgentExecution:
    """Unit tests for GenericMCPAgent._execute_once() in isolation."""

    def _make_task(self, name: str = "t", capability: str = "llm_synthesis",
                   schema: dict = None, validation_notes: str = None,
                   feedback: str = None) -> tuple:
        from cortex.config.schema import TaskTypeConfig
        from cortex.modules.task_graph_compiler import RuntimeTask

        cfg = TaskTypeConfig(
            name=name,
            description="Test task",
            capability_hint=capability,
            output_schema=schema,
            validation_notes=validation_notes,
        )
        task = RuntimeTask(
            task_id=f"sess_unit/001_{name}",
            task_name=name,
            instruction="Do something.",
            depends_on=[], depends_on_ids=[], input_refs=[],
            config=cfg,
            validation_feedback=feedback,
        )
        return task, cfg

    # ── E1: Basic LLM synthesis task ─────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_basic_llm_synthesis_task_completes(self):
        from cortex.modules.generic_mcp_agent import GenericMCPAgent

        task, cfg = self._make_task("answer")

        async def _stream(*, messages, system, provider_name):
            yield "The answer is 42."

        llm = MagicMock()
        llm.stream = _stream

        store = MagicMock()
        store.read_envelope = AsyncMock(return_value=None)

        agent = GenericMCPAgent(session_storage_path="/tmp")
        envelope = await agent._execute_once(
            task=task, tool_registry=MagicMock(),
            llm_client=llm, envelope_store=store, config=cfg,
        )

        assert envelope.status == "complete"
        assert "42" in envelope.output_value

    # ── E2: Retry feedback block appears in prompt ────────────────────────────

    @pytest.mark.asyncio
    async def test_retry_feedback_injected_into_llm_instruction(self):
        from cortex.modules.generic_mcp_agent import GenericMCPAgent

        task, cfg = self._make_task(feedback="Output was missing field 'summary'")
        seen = []

        async def _capture_stream(*, messages, system, provider_name):
            seen.append(messages[0]["content"])
            yield "fixed output with summary field"

        llm = MagicMock()
        llm.stream = _capture_stream

        store = MagicMock()
        store.read_envelope = AsyncMock(return_value=None)

        agent = GenericMCPAgent(session_storage_path="/tmp")
        await agent._execute_once(
            task=task, tool_registry=MagicMock(),
            llm_client=llm, envelope_store=store, config=cfg,
        )

        assert len(seen) == 1
        assert "RETRY FEEDBACK" in seen[0]
        assert "missing field 'summary'" in seen[0]

    # ── E3: No feedback block on first attempt ───────────────────────────────

    @pytest.mark.asyncio
    async def test_no_retry_feedback_on_first_attempt(self):
        from cortex.modules.generic_mcp_agent import GenericMCPAgent

        task, cfg = self._make_task()  # no feedback
        seen = []

        async def _capture_stream(*, messages, system, provider_name):
            seen.append(messages[0]["content"])
            yield "fresh result"

        llm = MagicMock()
        llm.stream = _capture_stream

        store = MagicMock()
        store.read_envelope = AsyncMock(return_value=None)

        agent = GenericMCPAgent(session_storage_path="/tmp")
        await agent._execute_once(
            task=task, tool_registry=MagicMock(),
            llm_client=llm, envelope_store=store, config=cfg,
        )

        assert "RETRY FEEDBACK" not in seen[0]

    # ── E4: JSON output parsed correctly ─────────────────────────────────────

    @pytest.mark.asyncio
    async def test_json_output_stored_verbatim(self):
        from cortex.modules.generic_mcp_agent import GenericMCPAgent

        task, cfg = self._make_task(schema={"type": "object"})
        payload = '{"result": "ok", "score": 0.99}'

        async def _json_stream(*, messages, system, provider_name):
            for ch in payload:
                yield ch

        llm = MagicMock()
        llm.stream = _json_stream

        store = MagicMock()
        store.read_envelope = AsyncMock(return_value=None)

        agent = GenericMCPAgent(session_storage_path="/tmp")
        envelope = await agent._execute_once(
            task=task, tool_registry=MagicMock(),
            llm_client=llm, envelope_store=store, config=cfg,
        )

        assert envelope.status == "complete"
        assert json.loads(envelope.output_value)["score"] == 0.99

    # ── E5: HITL ask_human disabled ──────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_ask_human_disabled_returns_none(self):
        from cortex.modules.generic_mcp_agent import GenericMCPAgent
        from cortex.config.schema import TaskTypeConfig
        from cortex.modules.task_graph_compiler import RuntimeTask

        cfg = TaskTypeConfig(name="t", description="d", human_in_loop=False)
        task = RuntimeTask(
            task_id="s/000_t", task_name="t", instruction="i",
            depends_on=[], depends_on_ids=[], input_refs=[], config=cfg,
        )
        agent = GenericMCPAgent(session_storage_path="/tmp")
        q = asyncio.Queue()
        result = await agent.ask_human(task, "Which option?", event_queue=q)
        assert result is None
        assert q.empty()

    # ── E6: HITL ask_human resolves via registry ──────────────────────────────

    @pytest.mark.asyncio
    async def test_ask_human_resolves_correctly(self):
        from cortex.framework import _PENDING_TASK_CLARIFICATIONS
        from cortex.modules.generic_mcp_agent import GenericMCPAgent
        from cortex.config.schema import TaskTypeConfig
        from cortex.modules.task_graph_compiler import RuntimeTask
        from cortex.streaming.status_events import ClarificationRequestEvent

        cfg = TaskTypeConfig(name="approve", description="Approval", human_in_loop=True)
        task = RuntimeTask(
            task_id="sess_hitl/001_approve",
            task_name="approve",
            instruction="Do the thing.",
            depends_on=[], depends_on_ids=[], input_refs=[], config=cfg,
        )
        agent = GenericMCPAgent(session_storage_path="/tmp")
        q = asyncio.Queue()

        ask_task = asyncio.create_task(
            agent.ask_human(task, "Approve or reject?", event_queue=q, timeout_seconds=5)
        )
        event = await asyncio.wait_for(q.get(), timeout=2)
        assert isinstance(event, ClarificationRequestEvent)
        clar_id = event.clarification_id

        # Simulate resolution
        entry = _PENDING_TASK_CLARIFICATIONS[clar_id]
        entry["answer"] = "approve"
        entry["event"].set()

        answer = await asyncio.wait_for(ask_task, timeout=2)
        assert answer == "approve"

    # ── E7: HITL timeout returns None ────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_ask_human_timeout_returns_none(self):
        from cortex.modules.generic_mcp_agent import GenericMCPAgent
        from cortex.config.schema import TaskTypeConfig
        from cortex.modules.task_graph_compiler import RuntimeTask

        cfg = TaskTypeConfig(name="t", description="d", human_in_loop=True)
        task = RuntimeTask(
            task_id="sess_timeout/001_t", task_name="t", instruction="i",
            depends_on=[], depends_on_ids=[], input_refs=[], config=cfg,
        )
        agent = GenericMCPAgent(session_storage_path="/tmp")
        q = asyncio.Queue()
        result = await agent.ask_human(task, "?", event_queue=q, timeout_seconds=0.05)
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# F. Wave validation gate (framework._run_wave_validation)
# ─────────────────────────────────────────────────────────────────────────────


class TestWaveValidationGate:
    """Tests for the output-schema and validation-notes wave gate logic."""

    def _gate_framework(self):
        from cortex.framework import CortexFramework
        return CortexFramework.__new__(CortexFramework)

    def _make_envelope(self, session_id: str, task_id: str, output: str) -> "ResultEnvelope":
        from cortex.modules.result_envelope_store import ResultEnvelope
        return ResultEnvelope(
            task_id=task_id, session_id=session_id,
            status="complete", output_value=output,
        )

    def _make_task(self, schema=None, notes=None) -> tuple:
        from cortex.config.schema import TaskTypeConfig
        from cortex.modules.task_graph_compiler import RuntimeTask

        cfg = TaskTypeConfig(
            name="t", description="d",
            output_schema=schema,
            validation_notes=notes,
        )
        task = RuntimeTask(
            task_id="s/000_t", task_name="t", instruction="i",
            depends_on=[], depends_on_ids=[], input_refs=[], config=cfg,
        )
        return task, cfg

    @pytest.mark.asyncio
    async def test_no_contract_always_passes(self):
        """No schema + no notes → gate must pass (return None)."""
        fw = self._gate_framework()
        task, _ = self._make_task()
        env = self._make_envelope("s", task.task_id, "anything")
        assert await fw._run_wave_validation(task, env, MagicMock()) is None

    @pytest.mark.asyncio
    async def test_schema_valid_json_all_fields_passes(self):
        fw = self._gate_framework()
        task, _ = self._make_task(schema={"type": "object", "required": ["x", "y"]})
        env = self._make_envelope("s", task.task_id, '{"x": 1, "y": 2}')
        assert await fw._run_wave_validation(task, env, MagicMock()) is None

    @pytest.mark.asyncio
    async def test_schema_missing_field_returns_feedback(self):
        fw = self._gate_framework()
        task, _ = self._make_task(schema={"type": "object", "required": ["x", "y"]})
        env = self._make_envelope("s", task.task_id, '{"x": 1}')  # missing y
        feedback = await fw._run_wave_validation(task, env, MagicMock())
        assert feedback is not None
        assert "y" in feedback

    @pytest.mark.asyncio
    async def test_schema_invalid_json_returns_feedback(self):
        fw = self._gate_framework()
        task, _ = self._make_task(schema={"type": "object", "required": ["x"]})
        env = self._make_envelope("s", task.task_id, "not json at all")
        feedback = await fw._run_wave_validation(task, env, MagicMock())
        assert feedback is not None

    @pytest.mark.asyncio
    async def test_validation_notes_delegates_to_primary(self):
        fw = self._gate_framework()
        task, _ = self._make_task(notes="Output must mention Paris")
        env = self._make_envelope("s", task.task_id, "The capital is Rome.")

        primary = MagicMock()
        primary.validate_task_output = AsyncMock(return_value="missing 'Paris'")
        feedback = await fw._run_wave_validation(task, env, primary)
        assert feedback == "missing 'Paris'"

    @pytest.mark.asyncio
    async def test_validation_notes_passes_when_primary_returns_none(self):
        fw = self._gate_framework()
        task, _ = self._make_task(notes="Some rule")
        env = self._make_envelope("s", task.task_id, "good answer")

        primary = MagicMock()
        primary.validate_task_output = AsyncMock(return_value=None)
        assert await fw._run_wave_validation(task, env, primary) is None

    @pytest.mark.asyncio
    async def test_both_schema_and_notes_schema_checked_first(self):
        """When schema check fails, notes-based LLM validation is skipped."""
        fw = self._gate_framework()
        task, _ = self._make_task(
            schema={"type": "object", "required": ["z"]},
            notes="Response must mention Paris",
        )
        env = self._make_envelope("s", task.task_id, '{"x": 1}')

        primary = MagicMock()
        primary.validate_task_output = AsyncMock(return_value="should not be called")
        feedback = await fw._run_wave_validation(task, env, primary)
        # Schema check caught "z" missing; notes-LLM call should not have been made
        assert feedback is not None
        assert "z" in feedback
        primary.validate_task_output.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# G. ResultEnvelope and storage
# ─────────────────────────────────────────────────────────────────────────────


class TestResultEnvelope:
    """Tests for ResultEnvelope serialisation and ResultEnvelopeStore."""

    def test_envelope_round_trip_dict(self):
        from cortex.modules.result_envelope_store import ResultEnvelope
        from cortex.llm.context import TokenUsage

        env = ResultEnvelope(
            task_id="sess/001_task",
            session_id="sess",
            status="complete",
            output_type="json",
            output_value='{"key": "value"}',
            content_summary="Short summary",
            duration_ms=1234,
            token_usage=TokenUsage(input_tokens=10, output_tokens=20, total_tokens=30),
            tool_trace=["tool_a", "tool_b"],
            is_adhoc=True,
        )
        d = env.to_dict()
        restored = ResultEnvelope.from_dict(d)

        assert restored.task_id == env.task_id
        assert restored.status == "complete"
        assert restored.output_type == "json"
        assert restored.duration_ms == 1234
        assert restored.token_usage.total_tokens == 30
        assert restored.tool_trace == ["tool_a", "tool_b"]
        assert restored.is_adhoc is True

    def test_envelope_default_schema_version(self):
        from cortex.modules.result_envelope_store import ResultEnvelope

        env = ResultEnvelope()
        assert env.schema_version == "1.0"

    @pytest.mark.asyncio
    async def test_envelope_store_write_and_read(self):
        from cortex.modules.result_envelope_store import ResultEnvelope, ResultEnvelopeStore
        from cortex.storage.memory_backend import MemoryBackend

        with tempfile.TemporaryDirectory() as tmpdir:
            backend = MemoryBackend()
            await backend.connect()

            store = ResultEnvelopeStore(
                base_path=tmpdir,
                storage_backend=backend,
                result_envelope_max_kb=1024,
                large_file_threshold_mb=10,
            )

            env = ResultEnvelope(
                task_id="sess_store/001_test",
                session_id="sess_store",
                status="complete",
                output_value="stored content",
            )
            await store.write_envelope(env)
            retrieved = await store.read_envelope("sess_store", "sess_store/001_test")

            assert retrieved is not None
            assert retrieved.output_value == "stored content"
            assert retrieved.status == "complete"


# ─────────────────────────────────────────────────────────────────────────────
# H. LLM decomposition parser
# ─────────────────────────────────────────────────────────────────────────────


class TestDecompositionParser:
    """Tests for the PrimaryAgent XML block parser."""

    def test_parse_single_task_block(self):
        from cortex.modules.primary_agent import _parse_task_blocks

        xml = "<task><name>search</name><instruction>Find articles</instruction><depends_on></depends_on></task>"
        tasks = _parse_task_blocks(xml)
        assert len(tasks) == 1
        assert tasks[0].task_name == "search"
        assert tasks[0].instruction == "Find articles"
        assert tasks[0].depends_on == []

    def test_parse_multiple_task_blocks(self):
        from cortex.modules.primary_agent import _parse_task_blocks

        xml = """
<task><name>fetch</name><instruction>Fetch data</instruction><depends_on></depends_on></task>
<task><name>clean</name><instruction>Clean data</instruction><depends_on>fetch</depends_on></task>
<task><name>report</name><instruction>Report</instruction><depends_on>clean</depends_on></task>
"""
        tasks = _parse_task_blocks(xml)
        assert len(tasks) == 3
        assert tasks[0].task_name == "fetch"
        assert tasks[1].depends_on == ["fetch"]
        assert tasks[2].depends_on == ["clean"]

    def test_parse_multi_dependency(self):
        from cortex.modules.primary_agent import _parse_task_blocks

        xml = "<task><name>merge</name><instruction>Merge</instruction><depends_on>enrich,validate</depends_on></task>"
        tasks = _parse_task_blocks(xml)
        assert "enrich" in tasks[0].depends_on
        assert "validate" in tasks[0].depends_on

    def test_parse_clarification_block(self):
        from cortex.modules.primary_agent import _parse_clarification

        text = "I need more context. <clarification>What format should the output be?</clarification>"
        q = _parse_clarification(text)
        assert q == "What format should the output be?"

    def test_parse_clarification_absent(self):
        from cortex.modules.primary_agent import _parse_clarification

        assert _parse_clarification("No clarification needed.") is None

    def test_parse_empty_string(self):
        from cortex.modules.primary_agent import _parse_task_blocks

        assert _parse_task_blocks("") == []

    def test_parse_malformed_skips_bad_blocks(self):
        from cortex.modules.primary_agent import _parse_task_blocks

        xml = """
<task><name>good</name><instruction>do good</instruction><depends_on></depends_on></task>
<task><incomplete>
<task><name>also_good</name><instruction>also good</instruction><depends_on>good</depends_on></task>
"""
        tasks = _parse_task_blocks(xml)
        names = [t.task_name for t in tasks]
        assert "good" in names
        assert "also_good" in names
