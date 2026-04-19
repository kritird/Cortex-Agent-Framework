"""CortexFramework — main public class. Entry point for developers."""
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from cortex.config.loader import load_config
from cortex.config.schema import CortexConfig
from cortex.exceptions import CortexConfigError, CortexException, CortexInvalidUserError, CortexSecurityError
from cortex.identity import Principal
from cortex.llm.client import LLMClient
from cortex.modules.history_store import (
    HistoryRecord, HistoryStore, TaskCompletion, TokenUsageByRole
)
from cortex.modules.learning_engine import LearningEngine
from cortex.modules.observability_emitter import ObservabilityEmitter
from cortex.modules.result_envelope_store import ResultEnvelope, ResultEnvelopeStore
from cortex.modules.session_manager import SessionManager
from cortex.modules.signal_registry import SignalRegistry
from cortex.modules.task_graph_compiler import (
    CompiledTaskGraph, TaskGraphCompiler
)
from cortex.modules.tool_server_registry import StartupReport, ToolServerRegistry
from cortex.modules.validation_agent import ValidationAgent, ValidationReport
from cortex.security.sanitiser import InputSanitiser
from cortex.security.scrubber import CredentialScrubber
from cortex.storage.memory_backend import MemoryBackend
from cortex.streaming.sse import SSEBuffer, SSEGenerator
from cortex.streaming.status_events import ClarificationEvent, EventType, StatusEvent

logger = logging.getLogger(__name__)

# Pending end-of-session evolution consent events.
# {clarification_id: {"event": asyncio.Event, "answer": str|None}}
_PENDING_EVOLUTION_CONSENTS: Dict[str, dict] = {}

# Pending mid-execution sub-agent clarification requests.
# Populated by GenericMCPAgent.ask_human(); resolved by
# CortexFramework.resolve_task_clarification() from the client side.
# {clarification_id: {"event": asyncio.Event, "answer": str|None, "loop": ...}}
_PENDING_TASK_CLARIFICATIONS: Dict[str, dict] = {}

# Pending "session hit max_wait_seconds — extend?" prompts. Resolved by
# CortexFramework.resolve_timeout_extension() from the client side.
# {clarification_id: {"event": asyncio.Event, "answer": str|None, "loop": ...}}
_PENDING_TIMEOUT_EXTENSIONS: Dict[str, dict] = {}

# How long to wait for the user to answer a timeout-extension prompt before
# defaulting to "exit" (silence = exit, as agreed with the user).
_TIMEOUT_EXTENSION_PROMPT_WINDOW_S: int = 60


async def _prompt_timeout_extension(
    session_id: str,
    event_queue: "asyncio.Queue",
    total_timeout_seconds: int,
) -> bool:
    """Ask the user whether to grant one more full timeout window.

    Returns True if the user answered "yes" within the prompt window,
    False otherwise (explicit "no", silence, or any error).
    """
    ext_id = f"timeout_ext_{session_id[-6:]}_{int(time.monotonic())}"
    consent_event = asyncio.Event()
    _PENDING_TIMEOUT_EXTENSIONS[ext_id] = {
        "event": consent_event,
        "answer": None,
        "loop": asyncio.get_event_loop(),
    }

    minutes = total_timeout_seconds / 60
    question = (
        f"Session reached its {minutes:.1f}-minute time limit. "
        f"Continue for another {minutes:.1f} minutes? (yes/no)"
    )
    await event_queue.put(ClarificationEvent(
        question=question,
        session_id=session_id,
        clarification_id=ext_id,
        options=["yes", "no"],
    ))

    try:
        await asyncio.wait_for(
            consent_event.wait(),
            timeout=_TIMEOUT_EXTENSION_PROMPT_WINDOW_S,
        )
        answer = (_PENDING_TIMEOUT_EXTENSIONS.pop(ext_id, {}) or {}).get("answer")
    except asyncio.TimeoutError:
        _PENDING_TIMEOUT_EXTENSIONS.pop(ext_id, None)
        logger.info(
            "Timeout-extension prompt unanswered for session %s — exiting",
            session_id,
        )
        return False

    return bool(answer and answer.strip().lower() in ("yes", "y"))


@dataclass
class SessionResult:
    """Result returned to the developer's application after a session completes."""
    session_id: str
    response: Optional[str]
    validation_report: Optional[ValidationReport]
    task_completion: TaskCompletion
    token_usage: TokenUsageByRole
    duration_seconds: float
    history_record: Optional[HistoryRecord] = None
    error: Optional[str] = None


class CortexFramework:
    """
    Main public class for the Cortex Agent Framework.

    Developers instantiate this class once and call run_session() for each request.

    Usage:
        framework = CortexFramework("cortex.yaml")
        await framework.initialize()

        result = await framework.run_session(
            user_id="user_123",
            request="Analyse Q3 revenue trends",
            event_queue=asyncio.Queue(),
        )
    """

    def __init__(self, config_path: str = "cortex.yaml"):
        self._config_path = config_path
        self._config: Optional[CortexConfig] = None
        self._initialized = False

        # Core components (set during initialize())
        self._llm_client: Optional[LLMClient] = None
        self._storage: Optional[MemoryBackend] = None
        self._tool_registry: Optional[ToolServerRegistry] = None
        self._session_manager: Optional[SessionManager] = None
        self._task_compiler: Optional[TaskGraphCompiler] = None
        self._compiled_graph: Optional[CompiledTaskGraph] = None
        self._signal_registry: Optional[SignalRegistry] = None
        self._envelope_store: Optional[ResultEnvelopeStore] = None
        self._history_store: Optional[HistoryStore] = None
        self._validation_agent: Optional[ValidationAgent] = None
        self._learning_engine: Optional[LearningEngine] = None
        self._observability: Optional[ObservabilityEmitter] = None
        self._sanitiser: Optional[InputSanitiser] = None
        self._scrubber: Optional[CredentialScrubber] = None
        self._startup_report: Optional[StartupReport] = None
        self._code_sandbox = None
        self._code_store = None
        self._external_mcp_registry = None
        self._ant_colony = None
        self._intent_gate = None  # cortex.modules.intent_gate.IntentGate

    async def initialize(self) -> "CortexFramework":
        """
        Load config, initialize all subsystems, connect tool servers.
        Must be called before run_session().
        Returns self for chaining.
        """
        logger.info("Initializing Cortex Agent Framework from %s", self._config_path)

        # Load and validate config
        self._config = load_config(self._config_path)
        cfg = self._config

        # Environment override for interaction_mode. `cortex publish mcp`
        # (and any future deployment wrapper that exposes the agent as a
        # callable RPC) sets CORTEX_INTERACTION_MODE=rpc so the loaded
        # cortex.yaml stays unchanged. Accepted values: "interactive", "rpc".
        import os as _os
        env_mode = _os.environ.get("CORTEX_INTERACTION_MODE", "").strip().lower()
        if env_mode in ("interactive", "rpc"):
            if cfg.agent.interaction_mode != env_mode:
                logger.info(
                    "CORTEX_INTERACTION_MODE=%s overriding cortex.yaml "
                    "(agent.interaction_mode was %r)",
                    env_mode, cfg.agent.interaction_mode,
                )
            cfg.agent.interaction_mode = env_mode
        elif env_mode:
            logger.warning(
                "Ignoring unknown CORTEX_INTERACTION_MODE=%r "
                "(expected 'interactive' or 'rpc')", env_mode,
            )

        # Validate validation threshold floor
        if cfg.validation.threshold < 0.60:
            raise CortexConfigError(
                f"validation.threshold {cfg.validation.threshold} is below the minimum floor of 0.60"
            )

        # Setup storage backend
        self._storage = await self._build_storage()

        # Security
        self._sanitiser = InputSanitiser(
            max_input_tokens=cfg.security.max_input_tokens,
            allowed_mime_types=cfg.file_input.allowed_mime_types,
            max_file_size_mb=cfg.file_input.max_size_mb,
        )
        self._scrubber = CredentialScrubber(
            extra_patterns=cfg.security.secret_scrub_patterns
        )

        # LLM client
        self._llm_client = LLMClient(cfg.llm_access)
        verify_results = await self._llm_client.verify_all()
        for pname, ok in verify_results.items():
            if not ok:
                logger.warning(
                    "LLM provider '%s' failed verification at startup — check credentials and connectivity",
                    pname,
                )

        # Observability
        audit_log = str(Path(cfg.storage.base_path) / "audit.log")
        self._observability = ObservabilityEmitter(audit_log_path=audit_log)

        # History store
        self._history_store = HistoryStore(
            base_path=cfg.storage.base_path,
            encryption_enabled=cfg.history.encryption_enabled,
            encryption_key=(
                __import__("os").environ.get(cfg.history.encryption_key_env_var)
                if cfg.history.encryption_key_env_var else None
            ),
        )

        # Session manager
        self._session_manager = SessionManager(
            agent_config=cfg.agent,
            storage_config=cfg.storage,
            history_config=cfg.history,
            history_store=self._history_store,
            storage_backend=self._storage,
        )
        await self._session_manager.initialize()

        # Signal registry
        self._signal_registry = SignalRegistry(storage_backend=self._storage)
        await self._signal_registry.start()

        # Task graph compiler
        self._task_compiler = TaskGraphCompiler()
        self._compiled_graph = self._task_compiler.compile(cfg.task_types)

        # Result envelope store
        self._envelope_store = ResultEnvelopeStore(
            base_path=cfg.storage.base_path,
            storage_backend=self._storage,
            result_envelope_max_kb=cfg.storage.result_envelope_max_kb,
            large_file_threshold_mb=cfg.storage.large_file_threshold_mb,
        )

        # Blueprint store — per-task markdown guidance referenced by task_type.blueprint.
        # Built unconditionally so PrimaryAgent can ask for blueprints on demand;
        # nothing is loaded until a task actually references one.
        from cortex.modules.blueprint_store import BlueprintStore
        blueprint_dir = cfg.blueprint.dir or str(Path(cfg.storage.base_path) / "blueprints")
        self._blueprint_store = BlueprintStore(
            dir_path=blueprint_dir,
            storage_mode=cfg.blueprint.storage_mode,
            storage_backend=self._storage if cfg.blueprint.storage_mode == "backend" else None,
        )

        # Tool server registry
        self._tool_registry = ToolServerRegistry(user_config=cfg.user_config)
        registry_path = cfg.startup.capability_registry_path or str(
            Path(cfg.storage.base_path) / ".capability_registry.json"
        )
        self._startup_report = await self._tool_registry.initialize_all(
            config=cfg.tool_servers,
            require_all=cfg.startup.require_all_servers,
            discovery_timeout=cfg.startup.discovery_timeout_seconds,
            verify_auth=cfg.startup.verify_auth,
            log_tools=cfg.startup.log_discovered_tools,
            eager_discovery=cfg.startup.eager_discovery,
            registry_path=registry_path,
        )
        if cfg.startup.eager_discovery:
            await self._tool_registry.start_health_check_loop()
        else:
            # Probe all servers in the background; results are saved to the
            # registry file so the next startup has pre-warmed capabilities.
            await self._tool_registry.start_background_discovery(
                registry_path=registry_path,
                concurrency=cfg.startup.background_discovery_concurrency,
            )

        # External MCP registry — auto-discovered servers from the internet.
        # Loaded unconditionally; discovery is gated at runtime by
        # capability_scout.external_discovery.enabled in the config.
        from cortex.modules.external_mcp_registry import ExternalMCPRegistry
        ext_discovery_cfg = cfg.agent.capability_scout.external_discovery
        ext_store_path = ext_discovery_cfg.auto_discovery_file
        if not Path(ext_store_path).is_absolute():
            ext_store_path = str(Path(cfg.storage.base_path) / ext_store_path)
        self._external_mcp_registry = ExternalMCPRegistry(store_path=ext_store_path)
        self._external_mcp_registry.load()
        logger.info(
            "ExternalMCPRegistry loaded from %s (%d known external server(s))",
            ext_store_path,
            len(self._external_mcp_registry.get_all_verified()),
        )

        # Validation agent
        self._validation_agent = ValidationAgent(
            llm_client=self._llm_client,
            config=cfg.validation,
        )

        # Learning engine
        delta_path = str(Path(cfg.storage.base_path) / "cortex_delta")
        self._learning_engine = LearningEngine(
            delta_path=delta_path,
            config=cfg.learning,
        )
        self._learning_engine.set_reload_callback(self.hot_reload)

        # Intent gate — pre-scout turn classifier.
        from cortex.modules.intent_gate import IntentGate
        self._intent_gate = IntentGate(
            config=cfg.agent.intent_gate,
            llm_client=self._llm_client,
        )

        # Code execution sandbox (enabled via code_sandbox.enabled in cortex.yaml)
        if cfg.code_sandbox.enabled:
            from cortex.sandbox.code_sandbox import CodeSandbox
            from cortex.sandbox.code_store import AgentCodeStore
            self._code_sandbox = CodeSandbox(
                base_path=cfg.storage.base_path,
                timeout_seconds=cfg.code_sandbox.timeout_seconds,
                allow_network=cfg.code_sandbox.allow_network,
            )
            await self._code_sandbox.ensure_venv()
            self._code_store = AgentCodeStore(base_path=cfg.storage.base_path)
            # Wire code_store into LearningEngine — both are evolution concerns
            self._learning_engine.set_code_store(self._code_store)
            logger.info(
                "Code sandbox enabled (timeout=%ds, network=%s, %d persisted scripts loaded)",
                cfg.code_sandbox.timeout_seconds,
                cfg.code_sandbox.allow_network,
                len(self._code_store.list_scripts()),
            )

        # Ant Colony — self-spawning specialist agent subsystem
        if cfg.ant_colony.enabled:
            from cortex.ants.ant_colony import AntColony
            self._ant_colony = AntColony(
                base_path=cfg.storage.base_path,
                base_port=cfg.ant_colony.base_port,
                max_ants=cfg.ant_colony.max_ants,
                auto_restart=cfg.ant_colony.auto_restart,
                llm_provider=cfg.ant_colony.llm_provider,
                llm_model=cfg.ant_colony.llm_model,
                api_key_env_var=cfg.ant_colony.api_key_env_var,
            )
            self._ant_colony.set_register_callback(self._on_ant_registered)
            self._ant_colony.set_deregister_callback(self._on_ant_deregistered)
            # Re-hatch any previously running ants from ants.yaml
            await self._ant_colony.resume_colony(self._tool_registry)
            logger.info(
                "Ant Colony enabled (base_port=%d, max_ants=%d, auto_restart=%s, %d ant(s) loaded)",
                cfg.ant_colony.base_port,
                cfg.ant_colony.max_ants,
                cfg.ant_colony.auto_restart,
                len(self._ant_colony.list_ants()),
            )

        self._initialized = True
        logger.info("Cortex Agent Framework initialized successfully")
        return self

    async def _build_storage(self):
        """Build the storage backend from config."""
        cfg = self._config
        if cfg.redis.enabled:
            import os
            from cortex.storage.redis_backend import RedisBackend
            password = os.environ.get(cfg.redis.password_env_var) if cfg.redis.password_env_var else None
            username = os.environ.get(cfg.redis.username_env_var) if cfg.redis.username_env_var else None
            backend = RedisBackend(
                host=cfg.redis.host,
                port=cfg.redis.port,
                db=cfg.redis.db,
                password=password,
                username=username,
                key_prefix=cfg.redis.key_prefix,
                pool_max_connections=cfg.redis.pool_max_connections,
            )
            await backend.connect()
            return backend
        elif cfg.sqlite.enabled:
            from cortex.storage.sqlite_backend import SQLiteBackend
            path = cfg.sqlite.path or str(Path(cfg.storage.base_path) / "cortex.db")
            backend = SQLiteBackend(db_path=path, wal_mode=cfg.sqlite.wal_mode)
            await backend.connect()
            return backend
        else:
            backend = MemoryBackend()
            await backend.connect()
            return backend

    async def run_session(
        self,
        user_id: str,
        request: str,
        event_queue: asyncio.Queue,
        file_refs: Optional[List[str]] = None,
        user_task_types: Optional[List] = None,
        user_consent: str = "none",
        resume_session_id: Optional[str] = None,
        principal: Optional[Principal] = None,
    ) -> SessionResult:
        """
        Execute a complete agent session: decompose → fan-out → fan-in → synthesise → validate.

        Args:
            user_id: Application-provided user identifier (trusted as-is)
            request: The user's natural language request
            event_queue: asyncio.Queue for streaming status/result events to the caller
            file_refs: Optional list of file paths attached to the request
            user_task_types: Optional task type overrides from user config
            user_consent: "positive" | "negative" | "none" for learning engine
            resume_session_id: If set, resume a previously timed-out session instead of
                               starting fresh. The original decomposition is skipped and
                               only the remaining pending tasks are executed.
            principal: Optional Principal identity. If not provided, one is
                       constructed automatically from ``user_id`` via
                       ``Principal.from_user_id()``. Pass an explicit principal
                       for system agents or agent-to-agent delegation.

        Returns:
            SessionResult with final response and all metadata
        """
        self._assert_initialized()
        self._assert_valid_user_id(user_id)
        start_time = time.time()

        # Build principal — use explicit one if provided, otherwise derive from user_id
        if principal is None:
            principal = Principal.from_user_id(user_id)

        # ── Resume path: re-open timed-out session ────────────────────────────
        if resume_session_id:
            return await self._resume_session(
                resume_session_id=resume_session_id,
                user_id=user_id,
                event_queue=event_queue,
                user_task_types=user_task_types,
                user_consent=user_consent,
                start_time=start_time,
                principal=principal,
            )

        # Sanitise input
        request = self._sanitiser.sanitise_text_input(request)

        # Create session
        session = await self._session_manager.create_session(user_id, request)
        session_id = session.session_id
        self._observability.emit_session_start(session_id, user_id, principal=principal)

        # Auto-cleanup expired history
        await self._session_manager.auto_cleanup_expired_history(user_id)

        # Session storage path
        session_path = str(Path(self._config.storage.base_path) / session_id)

        task_completion = TaskCompletion()
        token_usage = TokenUsageByRole()
        final_response = None
        validation_report = None

        try:
            from cortex.modules.primary_agent import PrimaryAgent
            from cortex.modules.generic_mcp_agent import GenericMCPAgent
            from cortex.modules.capability_scout import CapabilityScout as _CapabilityScout

            _scout_instance = _CapabilityScout()
            _ext_disc_cfg = self._config.agent.capability_scout.external_discovery

            async def _mid_run_discovery(capability: str) -> bool:
                """Discovery callback injected into GenericMCPAgent.

                Returns True if at least one new tool was registered for
                *capability*, False otherwise.
                """
                if not self._external_mcp_registry or not _ext_disc_cfg.enabled:
                    return False
                new_tools = await _scout_instance.discover_for_task(
                    capability=capability,
                    registry=self._tool_registry,
                    llm_client=self._llm_client,
                    external_registry=self._external_mcp_registry,
                    discovery_config=_ext_disc_cfg,
                )
                return bool(new_tools)

            primary = PrimaryAgent(self._config, self._llm_client, blueprint_store=self._blueprint_store)
            primary.reset_session_state()
            mcp_agent = GenericMCPAgent(
                session_storage_path=session_path,
                scrubber=self._scrubber,
                code_sandbox=self._code_sandbox,
                code_store=self._code_store,
                sandbox_config=self._config.code_sandbox,
                discovery_callback=_mid_run_discovery,
            )

            # Load history context
            history_context = []
            if self._config.history.enabled:
                history_context = await self._history_store.get_context_sessions(
                    user_id, self._config.history.max_sessions_in_context
                )

            # Available capabilities
            capabilities = list(set(
                cap
                for cap, servers in self._tool_registry._capability_map.items()
                if servers
            ))

            # Emit session start capability message
            start_msg = self._tool_registry.emit_session_start_event()
            await event_queue.put(StatusEvent(
                message=start_msg,
                session_id=session_id,
                event_type=EventType.SESSION_START,
            ))

            # ── Intent Gate (pre-scout) ─────────────────────────────────────────
            # Decides chat vs task vs hybrid. In "rpc" interaction_mode the gate
            # forces task and never clarifies — published MCP callers cannot
            # answer interactive prompts. See cortex/modules/intent_gate.py.
            task_type_names = [tt.name for tt in self._config.task_types]
            code_util_names: List[str] = []
            if self._code_store is not None:
                try:
                    code_util_names = [
                        r.task_name for r in self._code_store.list_all()
                    ]
                except Exception as e:  # defensive — never block a session on this
                    logger.debug("Code store list_all failed in intent gate: %s", e)

            intent_decision = await self._intent_gate.classify(
                request=request,
                history=history_context,
                file_refs=file_refs or [],
                task_type_names=task_type_names,
                code_util_names=code_util_names,
                capabilities=capabilities,
                interaction_mode=self._config.agent.interaction_mode,
            )

            # Optional single clarification round *before* any heavy work.
            # Only valid in interactive mode with clarification enabled.
            if (
                intent_decision.needs_clarify
                and intent_decision.clarify_q
                and self._config.agent.interaction_mode == "interactive"
                and self._config.agent.clarification.enabled
            ):
                clar_id = f"intent_{session_id[-4:]}"
                clar_event = asyncio.Event()
                primary._clarification_events[session_id] = clar_event
                await event_queue.put(ClarificationEvent(
                    question=intent_decision.clarify_q,
                    session_id=session_id,
                    clarification_id=clar_id,
                ))
                try:
                    await asyncio.wait_for(clar_event.wait(), timeout=300)
                    clar_answer = primary._clarification_answers.pop(session_id, "")
                    primary._clarification_events.pop(session_id, None)
                    if clar_answer:
                        request = f"{request}\n\nClarification: {clar_answer}"
                        intent_decision = await self._intent_gate.classify(
                            request=request,
                            history=history_context,
                            file_refs=file_refs or [],
                            task_type_names=task_type_names,
                            code_util_names=code_util_names,
                            capabilities=capabilities,
                            interaction_mode=self._config.agent.interaction_mode,
                        )
                except asyncio.TimeoutError:
                    primary._clarification_events.pop(session_id, None)
                    logger.info(
                        "Intent-gate clarification timed out for session %s — "
                        "defaulting to task routing", session_id,
                    )
                    intent_decision.mode = "task"
                    intent_decision.needs_clarify = False

            intent_is_chat = (intent_decision.mode == "chat")
            logger.info(
                "Intent gate: session=%s mode=%s source=%s rationale=%r",
                session_id, intent_decision.mode,
                intent_decision.source, intent_decision.rationale[:120],
            )

            stale_task_names: set = set()
            scout_result = None
            decomposed_tasks: List = []
            all_envelopes: List[ResultEnvelope] = []
            timed_out = False

            # ── Capability Scout (pre-decomposition) ────────────────────────────
            # Identifies which MCP servers are relevant to this request and fetches
            # their actual tool descriptions, giving the decomposition LLM real
            # vocabulary instead of abstract capability names.
            # Skipped on chat turns — nothing to route.
            if not intent_is_chat and self._config.agent.capability_scout.enabled and capabilities:
                await event_queue.put(StatusEvent(
                    message="Identifying relevant tools...",
                    session_id=session_id,
                    event_type=EventType.STATUS,
                ))
                # Scripted tasks run a Python handler — no LLM call and no
                # MCP probing needed. Only exclude capabilities with an
                # explicit hint (not "auto") so we don't accidentally suppress
                # unknown mappings.
                no_probe_caps: set = {
                    tt.capability_hint
                    for tt in self._config.task_types
                    if (
                        tt.complexity == "scripted"  # scripted handler — no LLM, no MCP probing needed
                        and tt.name not in stale_task_names
                        and tt.capability_hint
                        and tt.capability_hint != "auto"
                    )
                }
                # Note: 'pinned' tasks are intentionally excluded — they still run the LLM
                # and may need MCP capabilities, only their sub-task graph is topology-locked.

                try:
                    scout_result = await asyncio.wait_for(
                        _scout_instance.run(
                            request=request,
                            available_capabilities=capabilities,
                            registry=self._tool_registry,
                            llm_client=self._llm_client,
                            max_capabilities=self._config.agent.capability_scout.max_capabilities,
                            code_store=self._code_store,
                            no_probe_capabilities=no_probe_caps or None,
                            external_registry=self._external_mcp_registry,
                            discovery_config=_ext_disc_cfg,
                            ant_colony=self._ant_colony,
                            auto_hatch_on_gap=self._config.ant_colony.auto_hatch_on_gap,
                        ),
                        timeout=self._config.agent.capability_scout.timeout_seconds,
                    )
                    if scout_result.has_tools:
                        logger.info(
                            "Scout identified %d tools across %d capabilities for session %s",
                            len(scout_result.tools),
                            len(scout_result.matched_capabilities),
                            session_id,
                        )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Capability scout timed out for session %s — proceeding without enrichment",
                        session_id,
                    )
                except Exception as e:
                    logger.warning("Capability scout failed (%s) — proceeding without enrichment", e)

            # ── Blueprint staleness check (pre-decomposition) ────────────────────
            # For each task type that references a blueprint, check whether the
            # blueprint's last_successful_run_at is older than the configured
            # staleness_warning_days. Stale task names are threaded into the
            # decomposition prompt so the LLM re-discovers subtasks rather than
            # blindly following the stored topology.
            # Skipped on chat turns.
            if not intent_is_chat and self._config.blueprint.enabled:
                staleness_days = self._config.blueprint.staleness_warning_days
                for tt in self._config.task_types:
                    ref = getattr(tt, "blueprint", None)
                    if not ref:
                        continue
                    bp = await self._blueprint_store.load(ref)
                    if bp and bp.is_stale(staleness_days):
                        stale_task_names.add(tt.name)
                if stale_task_names:
                    logger.info(
                        "Stale blueprints detected for session %s: %s",
                        session_id, stale_task_names,
                    )

            # ── LLM CALL #1: Decomposition ──────────────────────────────────────
            # Persisted agent scripts are surfaced via scout_result.code_utils.
            # Skipped on chat turns — converse() handles them directly below.
            if not intent_is_chat:
                async for task in primary.decompose(
                    session_id=session_id,
                    user_id=user_id,
                    request=request,
                    file_refs=file_refs or [],
                    history_context=history_context,
                    available_capabilities=capabilities,
                    event_queue=event_queue,
                    scout_result=scout_result,
                    stale_task_names=stale_task_names,
                ):
                    decomposed_tasks.append(task)

            if intent_is_chat:
                # Chat turn — direct conversational reply, no tasks.
                final_response = await primary.converse(
                    session_id=session_id,
                    request=request,
                    history_context=history_context,
                    available_capabilities=capabilities,
                    event_queue=event_queue,
                    task_type_names=task_type_names,
                )
            elif not decomposed_tasks:
                # Task mode but decomposer returned no tasks. In rpc mode this
                # is a client error (no actionable instruction); in interactive
                # mode the hardened synthesise() will still respond directly.
                if self._config.agent.interaction_mode == "rpc":
                    logger.warning(
                        "RPC session %s: no tasks decomposed — returning empty response",
                        session_id,
                    )
                    final_response = (
                        "No actionable task was identified in the request."
                    )
                else:
                    logger.warning("No tasks decomposed for session %s", session_id)
                    final_response = await primary.synthesise(
                        session_id=session_id,
                        result_envelopes=[],
                        bash_excerpts={},
                        original_request=request,
                        event_queue=event_queue,
                        storage_base_path=session_path,
                    )
            else:
                # Instantiate runtime graph
                runtime_graph = self._task_compiler.instantiate(
                    compiled=self._compiled_graph,
                    session_id=session_id,
                    decomposed_tasks=decomposed_tasks,
                    user_task_types=user_task_types,
                    max_tasks=self._config.agent.concurrency.max_tasks_per_session,
                    sandbox_enabled=self._config.code_sandbox.enabled,
                    principal=principal,
                )

                # Register all task signals
                for task_id in runtime_graph.tasks:
                    self._signal_registry.register_task(session_id, task_id)

                # ── Fan-Out / Fan-In execution waves ──────────────────────────
                deadline = time.monotonic() + self._config.agent.time.default_max_wait_seconds
                deadline_extended = False

                while True:
                    ready = self._task_compiler.get_ready_tasks(runtime_graph)
                    if not ready:
                        break

                    # Dispatch ready tasks in parallel, capped at max_parallel_tasks
                    max_parallel = self._config.agent.concurrency.max_parallel_tasks
                    sem = asyncio.Semaphore(max_parallel)

                    for task in ready:
                        task.status = "running"
                        self._observability.emit_task_dispatch(session_id, task.task_id, task.task_name, principal=task.principal)

                    async def _run_with_sem(task):
                        async with sem:
                            return await mcp_agent.execute_task(
                                task=task,
                                tool_registry=self._tool_registry,
                                llm_client=self._llm_client,
                                envelope_store=self._envelope_store,
                                signal_registry=self._signal_registry,
                                config=task.config,
                                event_queue=event_queue,
                            )

                    task_coros = [_run_with_sem(task) for task in ready]
                    wave_results = await asyncio.gather(*task_coros, return_exceptions=True)

                    for task, result in zip(ready, wave_results):
                        if isinstance(result, Exception):
                            logger.error("Task %s raised exception: %s", task.task_id, result)
                            self._task_compiler.mark_failed(runtime_graph, task.task_id)
                            task_completion.failed_tasks += 1
                            continue

                        self._observability.emit_task_complete(session_id, result)

                        if result.status == "complete":
                            # ── Wave validation gate ─────────────────────
                            # Runs only when the task has an output_schema or
                            # validation_notes contract. Otherwise passes.
                            feedback = await self._run_wave_validation(task, result, primary)

                            if feedback is None:
                                all_envelopes.append(result)
                                self._task_compiler.mark_complete(runtime_graph, task.task_id)
                                task_completion.completed_tasks += 1
                            else:
                                task.attempt_count += 1
                                if task.attempt_count >= 3:
                                    # Exhausted wave-level retries. Mark failed
                                    # and keep the last envelope for synthesis.
                                    logger.warning(
                                        "Task %s failed validation after 3 attempts: %s",
                                        task.task_id, feedback[:200],
                                    )
                                    all_envelopes.append(result)
                                    self._task_compiler.mark_failed(runtime_graph, task.task_id)
                                    task_completion.failed_tasks += 1
                                    await event_queue.put(StatusEvent(
                                        message=(
                                            f"Task '{task.task_name}' failed validation "
                                            f"after 3 attempts and was abandoned."
                                        ),
                                        session_id=session_id,
                                        event_type=EventType.ERROR,
                                    ))
                                else:
                                    # Re-queue for another attempt with feedback.
                                    # Do NOT append the envelope — the next
                                    # attempt's envelope will replace it.
                                    task.validation_feedback = feedback
                                    task.hitl_ask_count = 0
                                    task.status = "pending"
                                    await event_queue.put(StatusEvent(
                                        message=(
                                            f"Retrying '{task.task_name}' "
                                            f"(attempt {task.attempt_count + 1}/3) "
                                            f"with validation feedback."
                                        ),
                                        session_id=session_id,
                                        event_type=EventType.STATUS,
                                    ))
                        elif result.status == "failed":
                            all_envelopes.append(result)
                            self._task_compiler.mark_failed(runtime_graph, task.task_id)
                            task_completion.failed_tasks += 1
                        elif result.status == "timeout":
                            all_envelopes.append(result)
                            self._task_compiler.mark_failed(runtime_graph, task.task_id)
                            task_completion.timed_out_tasks += 1

                    task_completion.total_tasks = len(runtime_graph.tasks)

                    # ── Conditional replan (between waves) ───────────────
                    # Hard triggers (always replan):
                    #  (a) a stale-blueprint task just completed, OR
                    #  (b) a mandatory task just failed.
                    # Soft trigger (replan only if wave was not clean):
                    #  (c) an adaptive task completed — skipped when all tasks
                    #      in the wave passed on the first attempt with no
                    #      validation feedback, indicating nothing surprising
                    #      happened and the existing plan remains valid.
                    _wave_pairs = [
                        (t, res) for t, res in zip(ready, wave_results)
                        if not isinstance(res, Exception)
                    ]
                    _hard_trigger = any(
                        (res.status == "complete" and t.task_name in stale_task_names)
                        or (res.status == "failed" and getattr(getattr(t, "config", None), "mandatory", False))
                        for t, res in _wave_pairs
                    )
                    _wave_is_clean = all(
                        res.status == "complete"
                        and getattr(t, "attempt_count", 0) == 0
                        and not getattr(t, "validation_feedback", None)
                        for t, res in _wave_pairs
                    )
                    _soft_trigger = not _wave_is_clean and any(
                        res.status == "complete"
                        and getattr(getattr(t, "config", None), "complexity", "adaptive") == "adaptive"
                        for t, res in _wave_pairs
                    )
                    _wave_needs_replan = _hard_trigger or _soft_trigger

                    if not _wave_needs_replan and _wave_pairs:
                        logger.debug(
                            "Wave was clean (%d task(s) passed on first attempt) — skipping replan",
                            len(_wave_pairs),
                        )

                    if _wave_needs_replan:
                        try:
                            await primary.replan(
                                runtime_graph=runtime_graph,
                                completed_envelopes=all_envelopes,
                                task_compiler=self._task_compiler,
                                event_queue=event_queue,
                                principal=principal,
                            )
                        except Exception as e:
                            logger.debug("Replan hook raised (non-fatal): %s", e)

                    # Check time remaining
                    remaining = deadline - time.monotonic()
                    if remaining < 0:
                        total_time = self._config.agent.time.default_max_wait_seconds
                        if not deadline_extended:
                            logger.warning(
                                "Session %s reached max_wait_seconds — asking user to extend",
                                session_id,
                            )
                            granted = await _prompt_timeout_extension(
                                session_id=session_id,
                                event_queue=event_queue,
                                total_timeout_seconds=total_time,
                            )
                            if granted:
                                deadline = time.monotonic() + total_time
                                deadline_extended = True
                                await event_queue.put(StatusEvent(
                                    message=(
                                        f"Time limit extended by "
                                        f"{total_time}s — continuing."
                                    ),
                                    session_id=session_id,
                                    event_type=EventType.STATUS,
                                ))
                                continue
                        logger.warning("Session %s exceeded max_wait_seconds", session_id)
                        timed_out = True
                        break

                    # Check if 80% of time used — log warning
                    total_time = self._config.agent.time.default_max_wait_seconds
                    if remaining < total_time * 0.20:
                        await event_queue.put(StatusEvent(
                            message="⚠ Approaching time limit — wrapping up remaining tasks",
                            session_id=session_id,
                        ))

                # ── Aggregate token usage from all task envelopes ─────────────
                mcp_tokens = sum(e.token_usage.total_tokens for e in all_envelopes)
                token_usage.mcp_agent_tokens = mcp_tokens
                token_usage.total_tokens += mcp_tokens

                # ── Snapshot on timeout so session can be resumed ─────────────
                if timed_out:
                    try:
                        graph_snapshot = self._task_compiler.snapshot_graph(runtime_graph)
                        await self._session_manager.save_graph_snapshot(
                            session_id=session_id,
                            user_id=user_id,
                            original_request=request,
                            snapshot=graph_snapshot,
                        )
                        await event_queue.put(StatusEvent(
                            message=(
                                "Session timed out. Your progress has been saved — "
                                "call run_session() with resume_session_id to continue."
                            ),
                            session_id=session_id,
                            event_type=EventType.STATUS,
                        ))
                    except Exception as e:
                        logger.warning("Failed to save graph snapshot on timeout: %s", e)

                # ── LLM CALL #2 (optional): Re-evaluation if needed ──────────
                # (Skipped if all tasks completed normally)

                # ── LLM CALL #3: Final Synthesis ─────────────────────────────
                final_response = await primary.synthesise(
                    session_id=session_id,
                    result_envelopes=all_envelopes,
                    bash_excerpts={},
                    original_request=request,
                    event_queue=event_queue,
                    storage_base_path=session_path,
                )

            # ── Validation ───────────────────────────────────────────────────
            # Validation is scoped to task synthesis — skipped for chat turns
            # because the validator's rubric targets task completeness, not
            # conversational quality.
            if final_response and not intent_is_chat:
                final_response, validation_report = await self._validation_agent.validate_with_remediation(
                    user_request=request,
                    initial_response=final_response,
                    primary_agent=primary,
                    config=self._config.validation,
                    session_id=session_id,
                    event_queue=event_queue,
                )
                if validation_report:
                    self._observability.emit_validation_result(session_id, validation_report)

            # ── End-of-session evolution consent ─────────────────────────────────
            # Collect envelopes for tasks that were ad-hoc (not in cortex.yaml).
            adhoc_envelopes = [
                env for env in (all_envelopes if decomposed_tasks else [])
                if env.is_adhoc and env.status == "complete"
            ]

            if (
                adhoc_envelopes
                and self._config.learning.consent_enabled
                and validation_report
                and validation_report.passed
                and self._config.code_sandbox.ask_persist_consent
            ):
                evo_id = f"evolve_{session_id[-6:]}"
                consent_event = asyncio.Event()
                _PENDING_EVOLUTION_CONSENTS[evo_id] = {
                    "event": consent_event,
                    "answer": None,
                    "loop": asyncio.get_event_loop(),
                }

                task_names_display = ", ".join(
                    env.task_id.split("/")[-1].lstrip("0123456789_")
                    for env in adhoc_envelopes
                )
                scripts_count = sum(1 for env in adhoc_envelopes if env.generated_script)
                question = (
                    f"I completed {len(adhoc_envelopes)} new task type(s) this session "
                    f"({task_names_display})"
                )
                if scripts_count:
                    question += f" and generated {scripts_count} reusable Python script(s)"
                question += ". Would you like me to remember these for future sessions? (yes/no)"

                await event_queue.put(ClarificationEvent(
                    question=question,
                    session_id=session_id,
                    clarification_id=evo_id,
                ))

                try:
                    await asyncio.wait_for(consent_event.wait(), timeout=120)
                    evo_answer = (_PENDING_EVOLUTION_CONSENTS.pop(evo_id, {}) or {}).get("answer", "no")
                except asyncio.TimeoutError:
                    evo_answer = "no"
                    _PENDING_EVOLUTION_CONSENTS.pop(evo_id, None)
                    logger.info("Evolution consent timed out for session %s — not saving", session_id)

                if evo_answer and evo_answer.strip().lower() in ("yes", "y"):
                    try:
                        evo_result = await self._learning_engine.persist_evolution(
                            ad_hoc_envelopes=adhoc_envelopes,
                            user_id=user_id,
                            validation_report=validation_report,
                            cortex_yaml_path=self._config_path,
                        )
                        await event_queue.put(StatusEvent(
                            message=evo_result.message,
                            session_id=session_id,
                            event_type=EventType.STATUS,
                        ))
                        logger.info(
                            "Evolution: staged=%s scripts=%s auto_applied=%s",
                            evo_result.staged_tasks,
                            evo_result.scripts_persisted,
                            evo_result.auto_applied,
                        )
                    except Exception as e:
                        logger.warning("Evolution persist failed: %s", e)
                else:
                    logger.info("User declined evolution consent for session %s", session_id)

            # ── Blueprint auto-update (gated on consent) ─────────────────────────
            # When the user has consented (either via evolution flow above or by
            # passing user_consent='positive') and blueprint.auto_update is on,
            # append a lessons-learned entry to each task type's blueprint so the
            # next session benefits from what was learned this run. Task types
            # with no `blueprint:` reference are implicitly opted out.
            try:
                blueprint_cfg = getattr(self._config, "blueprint", None)
                consent_positive = (
                    user_consent == "positive"
                    or ('evo_answer' in locals() and str(evo_answer).strip().lower() in ("yes", "y"))
                )
                if (
                    blueprint_cfg
                    and blueprint_cfg.enabled
                    and blueprint_cfg.auto_update
                    and consent_positive
                    and decomposed_tasks
                ):
                    await self._persist_blueprints_from_session(
                        session_id=session_id,
                        primary_agent=primary,
                        decomposed_tasks=decomposed_tasks,
                        envelopes=all_envelopes,
                        validation_report=validation_report,
                    )
            except Exception as e:
                logger.warning("Blueprint auto-update failed for session %s: %s", session_id, e)

        except Exception as e:
            logger.error("Session %s failed with exception: %s", session_id, e, exc_info=True)
            await event_queue.put(StatusEvent(
                message=f"Session error: {str(e)[:200]}",
                session_id=session_id,
                event_type=EventType.ERROR,
            ))
            # Build a partial result
            duration = time.time() - start_time
            await self._session_manager.complete_session(session_id)
            self._signal_registry.cleanup_session(session_id)
            self._envelope_store.cleanup_session(session_id)
            return SessionResult(
                session_id=session_id,
                response=None,
                validation_report=None,
                task_completion=task_completion,
                token_usage=token_usage,
                duration_seconds=duration,
                error=str(e),
            )

        duration = time.time() - start_time
        val_score = validation_report.composite_score if validation_report else None
        val_passed = validation_report.passed if validation_report else None

        # Build history record
        history_record = HistoryRecord(
            session_id=session_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            user_id=user_id,
            original_request=request,
            response_summary=(final_response or "")[:500],
            task_completion=task_completion,
            validation_score=val_score,
            validation_passed=val_passed,
            user_consent=user_consent,
            token_usage=token_usage,
            persisted_files=[],
            duration_seconds=duration,
        )

        # Complete session — skip storage wipe if timed out (keep data for resume)
        await self._session_manager.complete_session(
            session_id,
            record=history_record,
            skip_storage_cleanup=timed_out,
        )
        self._signal_registry.cleanup_session(session_id)
        if not timed_out:
            self._envelope_store.cleanup_session(session_id)

        # ── External MCP auth-pending notification ───────────────────────────────
        # Surface any external MCPs discovered during this session that require
        # authentication so the developer can configure them for the next run.
        if self._external_mcp_registry:
            auth_pending = self._external_mcp_registry.get_auth_pending()
            if auth_pending:
                from cortex.streaming.status_events import ExternalMCPAuthRequiredEvent
                servers_payload = [
                    {
                        "url": r.url,
                        "name": r.name,
                        "capabilities": r.capabilities,
                        "reason": r.auth_required_reason or "authentication required",
                    }
                    for r in auth_pending
                ]
                # Build a ready-to-paste cortex.yaml snippet for the developer
                snippet_lines = ["# Add to tool_servers: in cortex.yaml"]
                for r in auth_pending:
                    safe_name = r.name.replace(" ", "_").lower()
                    snippet_lines += [
                        f"  {safe_name}:",
                        f"    url: {r.url}",
                        "    auth:",
                        "      type: bearer          # adjust to match the server's auth scheme",
                        f"      token_env_var: {safe_name.upper()}_API_KEY",
                        "    discovery:",
                        f"      capability_hints: {r.capabilities}",
                    ]
                cortex_yaml_snippet = "\n".join(snippet_lines)
                await event_queue.put(ExternalMCPAuthRequiredEvent(
                    session_id=session_id,
                    servers=servers_payload,
                    cortex_yaml_snippet=cortex_yaml_snippet,
                ))
                self._external_mcp_registry.clear_auth_pending()

        # Emit session end
        self._observability.emit_session_end(session_id, history_record)
        await event_queue.put(StatusEvent(
            message="Session complete.",
            session_id=session_id,
            event_type=EventType.SESSION_END,
        ))
        await event_queue.put(None)  # SSE sentinel

        return SessionResult(
            session_id=session_id,
            response=final_response,
            validation_report=validation_report,
            task_completion=task_completion,
            token_usage=token_usage,
            duration_seconds=duration,
            history_record=history_record,
        )

    async def _resume_session(
        self,
        resume_session_id: str,
        user_id: str,
        event_queue: asyncio.Queue,
        user_task_types: Optional[List],
        user_consent: str,
        start_time: float,
        principal: Optional[Principal] = None,
    ) -> SessionResult:
        """
        Resume a timed-out session from its saved graph snapshot.
        Re-opens the original session_id, restores the task graph,
        re-loads completed envelopes, and re-enters the wave loop
        for the remaining pending tasks only.
        """
        task_completion = TaskCompletion()
        token_usage = TokenUsageByRole()
        final_response = None
        validation_report = None
        session_id = resume_session_id

        # Load snapshot
        saved = await self._session_manager.load_graph_snapshot(resume_session_id)
        if not saved:
            raise CortexException(
                f"No resumable snapshot found for session '{resume_session_id}'. "
                "The session may have already been resumed or expired."
            )

        # Verify the requesting user owns this session
        original_user_id = saved.get("user_id")
        if original_user_id and original_user_id != user_id:
            raise CortexSecurityError(
                f"Session '{resume_session_id}' belongs to a different user. "
                "Cannot resume another user's session."
            )

        original_request = saved.get("original_request", "")
        snapshot = saved["snapshot"]

        # Re-open the session in SessionManager
        await self._session_manager.reopen_session(resume_session_id, user_id)
        self._observability.emit_session_start(session_id, user_id, principal=principal)

        await event_queue.put(StatusEvent(
            message="Resuming session — re-executing remaining tasks...",
            session_id=session_id,
            event_type=EventType.SESSION_START,
        ))

        try:
            from cortex.modules.primary_agent import PrimaryAgent
            from cortex.modules.generic_mcp_agent import GenericMCPAgent

            session_path = str(Path(self._config.storage.base_path) / session_id)
            primary = PrimaryAgent(self._config, self._llm_client, blueprint_store=self._blueprint_store)
            mcp_agent = GenericMCPAgent(
                session_storage_path=session_path,
                scrubber=self._scrubber,
                code_sandbox=self._code_sandbox,
                code_store=self._code_store,
                sandbox_config=self._config.code_sandbox,
            )

            # Restore the runtime graph (completed tasks keep status, pending reset to pending)
            runtime_graph = self._task_compiler.restore_graph(snapshot)

            # Re-register task signals
            for task_id in runtime_graph.tasks:
                self._signal_registry.register_task(session_id, task_id)

            # Re-load completed envelopes from storage into the envelope store
            prior_envelopes = await self._envelope_store.read_all_session_envelopes(session_id)
            all_envelopes: List[ResultEnvelope] = list(prior_envelopes)

            task_completion.total_tasks = len(runtime_graph.tasks)
            task_completion.completed_tasks = len(prior_envelopes)

            # ── Resume wave loop (pending tasks only) ─────────────────────────
            deadline = time.monotonic() + self._config.agent.time.default_max_wait_seconds
            timed_out = False
            deadline_extended = False

            while True:
                ready = self._task_compiler.get_ready_tasks(runtime_graph)
                if not ready:
                    break

                max_parallel = self._config.agent.concurrency.max_parallel_tasks
                sem = asyncio.Semaphore(max_parallel)

                for task in ready:
                    task.status = "running"
                    self._observability.emit_task_dispatch(session_id, task.task_id, task.task_name, principal=task.principal)

                async def _run_with_sem(task):
                    async with sem:
                        return await mcp_agent.execute_task(
                            task=task,
                            tool_registry=self._tool_registry,
                            llm_client=self._llm_client,
                            envelope_store=self._envelope_store,
                            signal_registry=self._signal_registry,
                            config=task.config,
                            event_queue=event_queue,
                        )

                wave_results = await asyncio.gather(
                    *[_run_with_sem(t) for t in ready], return_exceptions=True
                )

                for task, result in zip(ready, wave_results):
                    if isinstance(result, Exception):
                        logger.error("Task %s raised exception: %s", task.task_id, result)
                        self._task_compiler.mark_failed(runtime_graph, task.task_id)
                        task_completion.failed_tasks += 1
                        continue

                    self._observability.emit_task_complete(session_id, result)

                    if result.status == "complete":
                        feedback = await self._run_wave_validation(task, result, primary)
                        if feedback is None:
                            all_envelopes.append(result)
                            self._task_compiler.mark_complete(runtime_graph, task.task_id)
                            task_completion.completed_tasks += 1
                        else:
                            task.attempt_count += 1
                            if task.attempt_count >= 3:
                                logger.warning(
                                    "Task %s failed validation after 3 attempts: %s",
                                    task.task_id, feedback[:200],
                                )
                                all_envelopes.append(result)
                                self._task_compiler.mark_failed(runtime_graph, task.task_id)
                                task_completion.failed_tasks += 1
                            else:
                                task.validation_feedback = feedback
                                task.hitl_ask_count = 0
                                task.status = "pending"
                    elif result.status == "failed":
                        all_envelopes.append(result)
                        self._task_compiler.mark_failed(runtime_graph, task.task_id)
                        task_completion.failed_tasks += 1
                    elif result.status == "timeout":
                        all_envelopes.append(result)
                        self._task_compiler.mark_failed(runtime_graph, task.task_id)
                        task_completion.timed_out_tasks += 1

                # Conditional replan: mandatory-task failure OR adaptive-task
                # completion (see main-loop comment above — same policy).
                _resume_wave_needs_replan = any(
                    (
                        not isinstance(res, Exception)
                        and res.status == "failed"
                        and getattr(getattr(t, "config", None), "mandatory", False)
                    ) or (
                        not isinstance(res, Exception)
                        and res.status == "complete"
                        and getattr(getattr(t, "config", None), "complexity", "adaptive") == "adaptive"
                    )
                    for t, res in zip(ready, wave_results)
                )
                if _resume_wave_needs_replan:
                    try:
                        await primary.replan(
                            runtime_graph=runtime_graph,
                            completed_envelopes=all_envelopes,
                            task_compiler=self._task_compiler,
                            event_queue=event_queue,
                            principal=principal,
                        )
                    except Exception as e:
                        logger.debug("Replan hook raised (non-fatal): %s", e)

                remaining = deadline - time.monotonic()
                if remaining < 0:
                    total_time = self._config.agent.time.default_max_wait_seconds
                    if not deadline_extended:
                        logger.warning(
                            "Resumed session %s reached max_wait_seconds — asking user to extend",
                            session_id,
                        )
                        granted = await _prompt_timeout_extension(
                            session_id=session_id,
                            event_queue=event_queue,
                            total_timeout_seconds=total_time,
                        )
                        if granted:
                            deadline = time.monotonic() + total_time
                            deadline_extended = True
                            await event_queue.put(StatusEvent(
                                message=(
                                    f"Time limit extended by "
                                    f"{total_time}s — continuing."
                                ),
                                session_id=session_id,
                                event_type=EventType.STATUS,
                            ))
                            continue
                    logger.warning("Resumed session %s timed out again", session_id)
                    timed_out = True
                    break

            # ── Aggregate token usage ──────────────────────────────────────────
            mcp_tokens = sum(e.token_usage.total_tokens for e in all_envelopes)
            token_usage.mcp_agent_tokens = mcp_tokens
            token_usage.total_tokens += mcp_tokens

            # ── Snapshot again if timed out a second time ──────────────────────
            if timed_out:
                try:
                    graph_snapshot = self._task_compiler.snapshot_graph(runtime_graph)
                    await self._session_manager.save_graph_snapshot(
                        session_id=session_id, user_id=user_id,
                        original_request=original_request, snapshot=graph_snapshot,
                    )
                    await event_queue.put(StatusEvent(
                        message="Session timed out again. Progress saved — resume when ready.",
                        session_id=session_id, event_type=EventType.STATUS,
                    ))
                except Exception as e:
                    logger.warning("Failed to re-snapshot on second timeout: %s", e)
            else:
                # Resume succeeded — discard the snapshot
                await self._session_manager.discard_snapshot(session_id, user_id)

            # ── Synthesis ──────────────────────────────────────────────────────
            final_response = await primary.synthesise(
                session_id=session_id,
                result_envelopes=all_envelopes,
                bash_excerpts={},
                original_request=original_request,
                event_queue=event_queue,
                storage_base_path=session_path,
            )

            # ── Validation ─────────────────────────────────────────────────────
            if final_response:
                final_response, validation_report = await self._validation_agent.validate_with_remediation(
                    user_request=original_request,
                    initial_response=final_response,
                    primary_agent=primary,
                    config=self._config.validation,
                    session_id=session_id,
                    event_queue=event_queue,
                )

        except Exception as e:
            logger.error("Resume session %s failed: %s", session_id, e, exc_info=True)
            await event_queue.put(StatusEvent(
                message=f"Resume error: {str(e)[:200]}",
                session_id=session_id, event_type=EventType.ERROR,
            ))
            duration = time.time() - start_time
            await self._session_manager.complete_session(session_id, skip_storage_cleanup=True)
            self._signal_registry.cleanup_session(session_id)
            return SessionResult(
                session_id=session_id, response=None, validation_report=None,
                task_completion=task_completion, token_usage=token_usage,
                duration_seconds=duration, error=str(e),
            )

        duration = time.time() - start_time
        history_record = HistoryRecord(
            session_id=session_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            user_id=user_id,
            original_request=original_request,
            response_summary=(final_response or "")[:500],
            task_completion=task_completion,
            validation_score=validation_report.composite_score if validation_report else None,
            validation_passed=validation_report.passed if validation_report else None,
            user_consent=user_consent,
            token_usage=token_usage,
            persisted_files=[],
            duration_seconds=duration,
        )
        await self._session_manager.complete_session(
            session_id, record=history_record, skip_storage_cleanup=timed_out,
        )
        self._signal_registry.cleanup_session(session_id)
        if not timed_out:
            self._envelope_store.cleanup_session(session_id)

        await event_queue.put(StatusEvent(
            message="Session complete.", session_id=session_id,
            event_type=EventType.SESSION_END,
        ))
        await event_queue.put(None)

        return SessionResult(
            session_id=session_id,
            response=final_response,
            validation_report=validation_report,
            task_completion=task_completion,
            token_usage=token_usage,
            duration_seconds=duration,
            history_record=history_record,
        )

    def get_sse_generator(self, event_queue: asyncio.Queue) -> SSEGenerator:
        """Get an SSE generator for streaming events from a session's event queue."""
        self._assert_initialized()
        buffer_size = self._config.agent.streaming.reconnect_buffer_size
        min_interval = self._config.agent.streaming.min_delivery_interval_ms
        return SSEGenerator(
            event_queue=event_queue,
            buffer=SSEBuffer(max_size=buffer_size),
            min_delivery_interval_ms=min_interval,
        )

    def hot_reload(self, new_config: CortexConfig) -> None:
        """
        Hot-reload task_types, synthesis_guidance, and time budgets.
        Does NOT reload llm_access, tool_servers, or storage (restart required).
        """
        logger.info("Hot-reload: updating task_types and agent config")
        self._config = new_config
        if self._task_compiler:
            self._compiled_graph = self._task_compiler.compile(new_config.task_types)
        logger.info("Hot-reload complete: %d task types loaded", len(new_config.task_types))

    async def register_tool_server(
        self,
        name: str,
        url: str,
        auth: Dict = None,
        session_id: Optional[str] = None,
    ) -> None:
        """Register a tool server at runtime."""
        self._assert_initialized()
        await self._tool_registry.register_tool_server(
            name=name,
            url=url,
            auth=auth or {},
            session_id=session_id,
        )

    async def deregister_tool_server(self, name: str) -> None:
        """Deregister a tool server."""
        self._assert_initialized()
        await self._tool_registry.deregister_tool_server(name)

    # ── Ant Colony public API ──────────────────────────────────────────────────

    async def hatch_ant(self, name: str, capability: str, description: str = "") -> Dict:
        """Hatch a new specialist ant agent for the given capability.

        Returns a dict with ant info (name, capability, url, port, pid, status).
        Raises CortexAntError if ant_colony is not enabled.
        """
        self._assert_initialized()
        if not self._ant_colony:
            from cortex.exceptions import CortexAntError
            raise CortexAntError("Ant Colony is not enabled. Set ant_colony.enabled: true in cortex.yaml.")
        info = await self._ant_colony.hatch(
            name=name,
            capability=capability,
            description=description or f"Specialist agent for {capability}",
            llm_client=self._llm_client,
        )
        return info.to_dict()

    async def stop_ant(self, name: str) -> None:
        """Stop a running ant agent by name."""
        self._assert_initialized()
        if not self._ant_colony:
            from cortex.exceptions import CortexAntError
            raise CortexAntError("Ant Colony is not enabled.")
        await self._ant_colony.stop(name)

    def list_ants(self) -> List[Dict]:
        """Return status of all ants in the colony."""
        self._assert_initialized()
        if not self._ant_colony:
            return []
        return [a.to_dict() for a in self._ant_colony.list_ants()]

    async def _on_ant_registered(self, name: str, url: str) -> None:
        """Callback: register a freshly hatched ant with the tool registry."""
        ant = self._ant_colony.get_ant(name) if self._ant_colony else None
        capability = ant.capability if ant else "llm_synthesis"
        try:
            await self._tool_registry.register_ant_server(name=name, url=url, capability=capability)
            logger.info("Framework: ant '%s' registered at %s", name, url)
        except Exception as exc:
            logger.warning("Framework: failed to register ant '%s': %s", name, exc)

    async def _on_ant_deregistered(self, name: str) -> None:
        """Callback: remove a stopped/crashed ant from the tool registry."""
        try:
            await self._tool_registry.deregister_tool_server(name)
        except Exception:
            pass

    async def apply_delta(
        self,
        min_confidence: str = "high",
    ) -> Dict:
        """Apply pending learning deltas to cortex.yaml."""
        self._assert_initialized()
        result = await self._learning_engine.apply_delta(
            delta_path=None,
            cortex_yaml_path=self._config_path,
            min_confidence=min_confidence,
        )
        return {"applied": result.applied, "skipped": result.skipped}

    async def shutdown(self, timeout_seconds: int = 30) -> None:
        """Gracefully shut down the framework."""
        logger.info("Shutting down Cortex Agent Framework...")
        if self._session_manager:
            await self._session_manager.graceful_shutdown(timeout_seconds)
        if self._tool_registry:
            await self._tool_registry.stop_health_check_loop()
            await self._tool_registry.close_all()
        if self._signal_registry:
            await self._signal_registry.stop()
        if self._storage:
            await self._storage.disconnect()
        logger.info("Cortex Agent Framework shutdown complete")

    def get_startup_report(self) -> Optional[StartupReport]:
        """Return the startup report from tool server initialization."""
        return self._startup_report

    async def _persist_blueprints_from_session(
        self,
        session_id: str,
        primary_agent,
        decomposed_tasks,
        envelopes,
        validation_report,
    ) -> None:
        """LLM-authored blueprint update, gated on user consent.

        Pipeline:
          1. Pair each decomposed task with its completed envelope and the
             existing blueprint (if any) — tasks without ``blueprint:`` are
             silently skipped.
          2. Make ONE batched LLM call via
             :meth:`PrimaryAgent.generate_blueprint_updates` that returns a
             structured update per task (new workflow text, additional dos/donts,
             clarifications, one-line lesson_summary).
          3. Merge each update into the existing blueprint via
             :meth:`Blueprint.merge_update` (bumps version, dedups bullets)
             and persist.

        Failures in the LLM call return an empty dict and are logged; we fall
        through without touching blueprints — user work is never blocked.
        """
        if not getattr(self, "_blueprint_store", None):
            return


        task_type_by_name = {tt.name: tt for tt in self._config.task_types}
        envelopes_by_task: dict = {}
        for env in envelopes or []:
            label = env.task_id.split("_", 1)[-1] if "_" in env.task_id else env.task_id
            envelopes_by_task.setdefault(label, []).append(env)

        # Collect (task_type, decomposed_task, envelope, existing_blueprint) tuples
        # so we can batch one LLM call for the whole session.
        bundles = []
        for task in decomposed_tasks or []:
            tt = task_type_by_name.get(task.task_name)
            if tt is None or not getattr(tt, "blueprint", None):
                continue
            matched = envelopes_by_task.get(task.task_name, [])
            if not matched:
                continue
            env = matched[0]
            is_topology_locked = getattr(tt, "complexity", "adaptive") in ("scripted", "pinned")
            existing = await self._blueprint_store.load_or_create(
                tt.blueprint, tt.name, deterministic=is_topology_locked
            )
            bundles.append((tt, task, env, existing))

        if not bundles:
            return

        # Build LLM inputs — fields match generate_blueprint_updates() expectation
        task_inputs = []
        for tt, task, env, existing in bundles:
            task_inputs.append({
                "task_name": tt.name,
                "complexity": getattr(tt, "complexity", "adaptive"),
                "instruction": task.instruction or "",
                "summary": env.content_summary or "",
                "status": env.status,
                "existing_topology": existing.topology,
                "existing_discovery_hints": existing.discovery_hints,
                "existing_preconditions": existing.preconditions,
                "existing_known_failure_modes": existing.known_failure_modes,
                "existing_dos": existing.dos,
                "existing_donts": existing.donts,
            })

        # Clarifications observed this session (best-effort — only the primary
        # decomposition clarification is stored centrally today).
        clarifications: list = []
        prim_clar = getattr(primary_agent, "_clarification_answers", {}) or {}
        answer = prim_clar.get(session_id)
        if answer:
            clarifications.append(f"A: {answer}")

        validation_findings: list = []
        if validation_report and getattr(validation_report, "findings", None):
            for f in validation_report.findings[:5]:
                issue = getattr(f, "issue", "") or ""
                suggestion = getattr(f, "suggestion", "") or ""
                if issue:
                    validation_findings.append(f"{issue} → {suggestion}" if suggestion else issue)

        updates = await primary_agent.generate_blueprint_updates(
            session_id=session_id,
            task_inputs=task_inputs,
            clarifications=clarifications,
            validation_findings=validation_findings,
        )
        if not updates:
            logger.info("Blueprint auto-update: LLM returned no updates for session %s", session_id)
            return

        touched = 0
        for tt, _task, env, existing in bundles:
            update = updates.get(tt.name)
            if not update:
                continue
            try:
                existing.merge_update(update)
                # Stamp the successful-run timestamp so staleness can be derived
                # from the blueprint itself without querying HistoryStore.
                if env.status == "complete":
                    existing.last_successful_run_at = (
                        datetime.now(timezone.utc).isoformat(timespec="seconds")
                    )
                await self._blueprint_store.save(existing)
                touched += 1
            except Exception as e:
                logger.warning("Failed to save blueprint for %s: %s", tt.name, e)

        if touched:
            logger.info(
                "Blueprint auto-update: LLM-merged %d blueprint(s) for session %s",
                touched, session_id,
            )

    def resolve_evolution_consent(self, clarification_id: str, answer: str) -> bool:
        """
        Called by the application when the user answers the end-of-session
        evolution consent question (ClarificationEvent with clarification_id
        starting with "evolve_").

        answer: "yes" | "no"
        Returns True if the clarification_id was found and resolved.

        Usage:
            # The event_queue emits a ClarificationEvent.
            # When the user responds via your API, call:
            framework.resolve_evolution_consent(clarification_id, "yes")
        """
        entry = _PENDING_EVOLUTION_CONSENTS.get(clarification_id)
        if entry:
            entry["answer"] = answer
            event = entry["event"]
            loop = entry.get("loop")
            if loop and loop.is_running():
                loop.call_soon_threadsafe(event.set)
            else:
                event.set()
            return True
        return False

    def resolve_timeout_extension(self, clarification_id: str, answer: str) -> bool:
        """
        Called by the application when the user answers a "session reached
        max_wait_seconds — extend?" prompt (ClarificationEvent with
        clarification_id starting with "timeout_ext_").

        answer: "yes" | "no"
        Returns True if the clarification_id was found and resolved.
        """
        entry = _PENDING_TIMEOUT_EXTENSIONS.get(clarification_id)
        if entry:
            entry["answer"] = answer
            event = entry["event"]
            loop = entry.get("loop")
            if loop and loop.is_running():
                loop.call_soon_threadsafe(event.set)
            else:
                event.set()
            return True
        return False

    def resolve_task_clarification(self, clarification_id: str, answer: str) -> bool:
        """
        Called by the application when the user answers a mid-execution
        clarification request emitted by a sub-agent via
        GenericMCPAgent.ask_human() (ClarificationRequestEvent with
        clarification_id starting with "hitl_").

        Returns True if the clarification_id was found and resolved, False
        if it had already timed out or never existed.

        Usage:
            # A ClarificationRequestEvent is placed on the event queue.
            # When the user responds via your API, call:
            framework.resolve_task_clarification(clarification_id, "the user's answer")
        """
        entry = _PENDING_TASK_CLARIFICATIONS.get(clarification_id)
        if entry:
            entry["answer"] = answer
            event = entry["event"]
            loop = entry.get("loop")
            if loop and loop.is_running():
                loop.call_soon_threadsafe(event.set)
            else:
                event.set()
            return True
        return False

    async def get_resumable_sessions(self, user_id: str) -> List[Dict]:
        """
        Return sessions that timed out and can be resumed for this user.

        Each entry: {"session_id": str, "saved_at": str, "original_request": str}

        Usage:
            resumable = await framework.get_resumable_sessions("user_123")
            if resumable:
                result = await framework.run_session(
                    user_id="user_123",
                    request="",                          # ignored on resume
                    event_queue=q,
                    resume_session_id=resumable[0]["session_id"],
                )
        """
        self._assert_initialized()
        self._assert_valid_user_id(user_id)
        return await self._session_manager.get_resumable_sessions(user_id)

    def list_agent_scripts(self) -> list:
        """Return all persisted agent scripts from the code store."""
        if not self._code_store:
            return []
        return self._code_store.list_scripts()

    def delete_agent_script(self, task_name: str) -> bool:
        """Delete a persisted script for a task type. Returns True if deleted."""
        if not self._code_store:
            return False
        return self._code_store.delete_script(task_name)

    async def _run_wave_validation(
        self,
        task,
        envelope,
        primary,
    ) -> Optional[str]:
        """Wave-level validation gate for a single task envelope.

        Runs only if the task has at least one validation contract set:
        `output_schema` (deterministic JSON check) or `validation_notes`
        (LLM-based soft check). If neither is set the gate is skipped and
        the task is assumed valid.

        Returns:
            None — the output is valid (or no contract was set).
            str  — feedback describing what is wrong; the wave loop will
                   re-queue the task with this feedback attached so the
                   sub-agent can correct itself on the next attempt.
        """
        cfg = getattr(task, "config", None)
        if cfg is None:
            return None
        if not cfg.output_schema and not cfg.validation_notes:
            return None

        # Deterministic JSON schema check (cheap, runs first).
        if cfg.output_schema:
            try:
                import json as _json
                raw = envelope.output_value
                try:
                    parsed = _json.loads(raw) if isinstance(raw, str) else raw
                except _json.JSONDecodeError:
                    return (
                        "Output is not valid JSON but output_schema is declared. "
                        f"Raw output (first 300 chars): {str(raw)[:300]}"
                    )
                required = cfg.output_schema.get("required", []) if isinstance(cfg.output_schema, dict) else []
                if isinstance(parsed, dict) and required:
                    missing = [k for k in required if k not in parsed]
                    if missing:
                        return (
                            f"Output is missing required fields: {missing}. "
                            f"Schema required: {required}"
                        )
            except Exception as e:
                logger.debug("Schema validation raised for task %s: %s", task.task_id, e)

        # LLM-based soft check against free-text validation_notes.
        if cfg.validation_notes:
            try:
                return await primary.validate_task_output(
                    task=task,
                    envelope=envelope,
                    validation_notes=cfg.validation_notes,
                )
            except Exception as e:
                logger.debug("LLM validation raised for task %s: %s", task.task_id, e)

        return None

    def _assert_initialized(self) -> None:
        if not self._initialized:
            raise CortexException(
                "CortexFramework has not been initialized. Call await framework.initialize() first."
            )

    @staticmethod
    def _assert_valid_user_id(user_id: str) -> None:
        """Ensure user_id is present and non-empty before any session or storage operation."""
        if not user_id or not user_id.strip():
            raise CortexInvalidUserError(
                "user_id must be a non-empty string. "
                "All framework operations are keyed per user — a missing or blank user_id "
                "would corrupt storage paths and session isolation."
            )
