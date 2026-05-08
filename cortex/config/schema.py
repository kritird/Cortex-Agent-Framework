"""Pydantic schema models for cortex.yaml configuration."""
from __future__ import annotations
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, ConfigDict


class AgentTimeConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    default_max_wait_seconds: int = 1000
    default_task_timeout_seconds: int = 400


class AgentPerformanceConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    streaming_decomposition: bool = False


class AgentConcurrencyConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    max_concurrent_sessions: int = 50
    max_concurrent_sessions_per_user: int = 3
    max_tasks_per_session: int = 20
    max_parallel_tasks: int = 5
    max_mcp_agent_llm_calls: int = 10


class AgentStreamingConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    status_updates: bool = False
    include_task_detail: bool = True
    mcp_agent_updates: bool = False
    reconnect_buffer_size: int = 50
    min_delivery_interval_ms: int = 200


class AgentClarificationConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    enabled: bool = False


class IntentGateConfig(BaseModel):
    """Pre-scout intent classifier that routes a turn as chat / task / hybrid.

    Stage 1 is a cheap heuristic; Stage 2 is an LLM classifier that only fires
    when the heuristic is under-confident. When disabled, every turn is routed
    through the full decomposition path (legacy behaviour).
    """
    model_config = ConfigDict(extra='allow')
    enabled: bool = True
    # Heuristic confidence at/above this threshold skips the LLM classifier.
    heuristic_confidence_threshold: float = 0.7
    # LLM provider key used for Stage 2. When falsy, the PrimaryAgent's
    # "default" provider is used — matches existing framework convention.
    llm_provider: str = "default"
    # Hard upper bound on classifier latency before we fall back to task mode.
    timeout_seconds: float = 50.0


class ExternalMCPDiscoveryConfig(BaseModel):
    """Config for the internet-based external MCP discovery sub-system."""
    model_config = ConfigDict(extra='allow')
    enabled: bool = True
    # Path to the auto-discovery store file.  Relative paths are resolved
    # against storage.base_path at runtime by CortexFramework.
    auto_discovery_file: str = "cortex_auto_mcps.yaml"
    # Curated pool of public MCP registries the scout may query.
    # Entries are tried in order; failures are swallowed so one dead registry
    # does not block the others.
    registry_sources: List[str] = Field(default_factory=lambda: [
        "https://registry.smithery.ai",
        "https://www.pulsemcp.com",
        "https://glama.ai",
        "https://mcp.so",
    ])
    # Maximum number of new external MCPs that may be registered per session.
    max_new_per_session: int = 5
    # Re-verify known external MCPs whose last_verified is older than this.
    max_stale_days: int = 30
    # Per-registry HTTP request timeout (seconds).
    search_timeout_s: float = 100.0


class CapabilityScoutConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    enabled: bool = True          # run scout before decomposition
    max_capabilities: int = 30    # probe at most N capabilities per request
    timeout_seconds: int = 100     # abandon scout if it takes too long
    external_discovery: ExternalMCPDiscoveryConfig = Field(
        default_factory=ExternalMCPDiscoveryConfig
    )


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    name: str
    description: str
    synthesis_guidance: str = ""
    # "interactive" — chat UIs, CLI, dev mode. Intent gate may route a turn to
    # a conversational reply or to task decomposition. "rpc" — agent is a
    # callable (e.g. published as MCP server); every turn is treated as a task
    # and no interactive clarification is emitted.
    interaction_mode: str = "interactive"
    time: AgentTimeConfig = Field(default_factory=AgentTimeConfig)
    performance: AgentPerformanceConfig = Field(default_factory=AgentPerformanceConfig)
    concurrency: AgentConcurrencyConfig = Field(default_factory=AgentConcurrencyConfig)
    streaming: AgentStreamingConfig = Field(default_factory=AgentStreamingConfig)
    clarification: AgentClarificationConfig = Field(default_factory=AgentClarificationConfig)
    capability_scout: CapabilityScoutConfig = Field(default_factory=CapabilityScoutConfig)
    intent_gate: IntentGateConfig = Field(default_factory=IntentGateConfig)
    builtin_web_search_enabled: bool = True


class TaskRetryConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    max_attempts: int = 2
    backoff_initial_ms: int = 500


class TaskOutputConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    content_summary_tokens: int = 400
    max_size_mb: int = 100


class TaskTypeConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    name: str
    description: str
    output_format: str = "text"  # md | json | text | file
    mandatory: bool = True
    complexity: str = "adaptive"  # adaptive | pinned | scripted
    capability_hint: str = "auto"
    tool_servers: List[str] = Field(default_factory=list)
    depends_on: List[str] = Field(default_factory=list)
    retry: TaskRetryConfig = Field(default_factory=TaskRetryConfig)
    output: TaskOutputConfig = Field(default_factory=TaskOutputConfig)
    timeout_seconds: int = 400
    llm_provider: str = "default"
    handler: Optional[str] = None
    # Wave-level validation contract. The wave validation gate runs only if
    # at least one of these is set; otherwise the task is assumed valid on
    # successful execution. Both are optional by design.
    output_schema: Optional[Dict[str, Any]] = None   # JSON Schema-ish dict
    validation_notes: Optional[str] = None            # free-text rules for LLM-based validation
    # Human-in-the-loop. When True, the sub-agent is permitted to pause mid-
    # execution and ask the user for clarification. When False, the sub-agent
    # must never block on user input.
    human_in_loop: bool = False
    # Optional pointer to a blueprint markdown file (path relative to
    # blueprint.dir, or an absolute path). When set, the blueprint is loaded
    # lazily and injected into the primary agent's system prompt so guidance
    # accumulated from prior runs (dos/donts, clarifications, lessons) steers
    # the next decomposition. If None, no blueprint is fetched for this task.
    blueprint: Optional[str] = None


class ToolServerAuthConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    type: str = "none"
    token_env_var: Optional[str] = None
    header: Optional[str] = None
    key_env_var: Optional[str] = None
    username_env_var: Optional[str] = None
    password_env_var: Optional[str] = None
    token_url: Optional[str] = None
    client_id_env_var: Optional[str] = None
    client_secret_env_var: Optional[str] = None
    scope: Optional[str] = None


class ToolServerTLSConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    enabled: bool = False
    verify_cert: bool = True
    ca_cert_file: str = ""
    client_cert_file: str = ""
    client_key_file: str = ""


class ToolServerConnectionConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    timeout_seconds: int = 100
    read_timeout_seconds: int = 600
    max_retries: int = 3
    retry_backoff_ms: int = 500


class ToolServerPoolConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    min_connections: int = 1
    max_connections: int = 10


class ToolServerDiscoveryConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    auto: bool = True
    capability_hints: List[str] = Field(default_factory=list)
    domain_hints: List[str] = Field(default_factory=list)


class ToolServerHealthCheckConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    enabled: bool = True
    endpoint: str = ""
    interval_seconds: int = 30
    failure_threshold: int = 3
    recovery_threshold: int = 2


class ToolServerConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    url: Optional[str] = None
    name: str = ""
    description: str = ""
    transport: str = "sse"  # sse | stdio | websocket
    auth: ToolServerAuthConfig = Field(default_factory=ToolServerAuthConfig)
    tls: ToolServerTLSConfig = Field(default_factory=ToolServerTLSConfig)
    connection: ToolServerConnectionConfig = Field(default_factory=ToolServerConnectionConfig)
    pool: ToolServerPoolConfig = Field(default_factory=ToolServerPoolConfig)
    discovery: ToolServerDiscoveryConfig = Field(default_factory=ToolServerDiscoveryConfig)
    health_check: ToolServerHealthCheckConfig = Field(default_factory=ToolServerHealthCheckConfig)
    headers: Dict[str, str] = Field(default_factory=dict)
    command: Optional[str] = None
    args: List[str] = Field(default_factory=list)
    env: Dict[str, str] = Field(default_factory=dict)
    working_dir: str = ""
    startup_timeout_seconds: int = 100


class LLMProviderConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    provider: str
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 4096
    api_key_env_var: Optional[str] = None
    base_url: Optional[str] = None
    headers: Dict[str, str] = Field(default_factory=dict)
    # Bedrock
    region_env_var: Optional[str] = None
    access_key_env_var: Optional[str] = None
    secret_key_env_var: Optional[str] = None
    session_token_env_var: Optional[str] = None
    # Azure
    endpoint_env_var: Optional[str] = None
    api_version: Optional[str] = None
    # Custom
    function: Optional[str] = None


class LLMAccessConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    default: LLMProviderConfig
    providers: Dict[str, LLMProviderConfig] = Field(default_factory=dict)


class ComplexityTierMap(BaseModel):
    """Maps task complexity tiers to named LLM provider keys from llm_access.providers."""
    model_config = ConfigDict(extra='allow')
    low: str = "default"     # simple retrieval, formatting, translation, short text
    medium: str = "default"  # multi-step reasoning, moderate code, single-doc analysis
    high: str = "default"    # complex architecture, multi-file codegen, deep synthesis


class AdaptiveModelRoutingConfig(BaseModel):
    """Adaptive Model Routing (AMR) — decomposer-driven per-task LLM selection.

    When enabled, the decomposition LLM emits a <model_tier> (low/medium/high) for
    each task it creates. AMR maps that tier to the provider key configured in tiers.
    An explicit llm_provider on a task_type in cortex.yaml always wins over AMR.

    The complexity assessment is objective — the LLM grades based on task nature, not
    provider preference. The tier→provider mapping lives entirely in config so no
    training-time bias can creep into the routing decision.

    validation_provider:
      ""  (empty) — auto-select: picks the first non-default key in llm_access.providers;
                    falls back to "default" when no named providers are configured.
      "<key>"     — use exactly that provider key for wave-level task validation.

    Ant sub-tasks inherit this config and its provider pool, so tasks spawned inside
    an ant's decomposition are also adaptively routed. The ant primary agent itself
    always decomposes on its own configured llm_provider (default).
    """
    model_config = ConfigDict(extra='allow')
    enabled: bool = False
    tiers: ComplexityTierMap = Field(default_factory=ComplexityTierMap)
    # "" = auto-select first non-default provider from llm_access.providers
    validation_provider: str = ""


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    base_path: str
    large_file_threshold_mb: int = 5
    result_envelope_max_kb: int = 64
    session_quota_mb: int = 500
    health_warning_free_gb: float = 10.0
    health_critical_free_gb: float = 2.0
    atomic_cleanup: bool = True


class SQLiteConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    enabled: bool = False
    path: str = ""
    wal_mode: bool = True
    connection_timeout_seconds: int = 50
    ttl_session_data_seconds: int = 3600
    ttl_session_index_seconds: int = 86400


class RedisConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 6379
    db: int = 1
    username: str = ""
    password_env_var: str = ""
    username_env_var: str = ""
    tls_enabled: bool = False
    tls_verify_peer: bool = True
    tls_cert_file: str = ""
    tls_key_file: str = ""
    tls_ca_cert_file: str = ""
    pool_max_connections: int = 20
    pool_min_idle: int = 5
    connection_timeout_ms: int = 2000
    socket_timeout_ms: int = 1000
    key_prefix: str = "cortex"
    ttl_session_data_seconds: int = 3600
    ttl_session_index_seconds: int = 86400
    ttl_pubsub_seconds: int = 300


class FileInputConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    max_size_mb: int = 50
    allowed_mime_types: List[str] = Field(default_factory=lambda: [
        "text/plain", "text/markdown", "text/csv", "text/html",
        "application/json", "application/xml", "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "image/png", "image/jpeg", "image/gif", "image/webp",
        "audio/mpeg", "audio/wav",
    ])


class UIAuthConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    mode: str = "none"  # none | token | basic
    token: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None


class UIConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8090
    title: str = "Cortex Agent"
    auth: UIAuthConfig = Field(default_factory=UIAuthConfig)


class ValidationConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    threshold: float = 0.75
    critical_threshold: float = 0.40
    timeout_seconds: int = 150
    weights_intent_match: float = 0.50
    weights_completeness: float = 0.30
    weights_coherence: float = 0.20
    expose_report_to_user: bool = True
    expose_score_to_user: bool = False
    wave_gate_llm_provider: str = "default"  # provider for wave-level task output LLM judge


class HistoryConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    enabled: bool = False
    retention_days: int = 90
    max_sessions_in_context: int = 5
    persist_task_outputs: List[str] = Field(default_factory=list)
    search_enabled: bool = True
    encryption_enabled: bool = False
    encryption_key_env_var: str = ""


class LearningConfig(BaseModel):
    """Autonomic learning — signal-gated, no user consent.

    Learning triggers off two signals at end-of-session:

      - ``validation_threshold``: composite validation score a response must
        reach before any learning action (blueprint update or delta staging)
        fires. Shared with the blueprint auto-update path.
      - ``complexity_threshold``: minimum ``TaskComplexityScorer`` score an
        ad-hoc task must reach before it is staged to ``pending.yaml``.
        Blueprint updates for *known* tasks ignore this — any successful
        run can refine a known task's guidance.

    The promote-to-cortex.yaml step remains gated by confidence accumulation
    (``auto_apply_min_confidence`` + ``auto_apply_min_confirmations``). A
    single session can stage a delta but can never promote one on its own.

    Chat turns (intent_gate → ``chat``) never trigger learning regardless of
    other signals.
    """
    model_config = ConfigDict(extra='allow')
    enabled: bool = True                          # master switch
    validation_threshold: float = 0.75            # min composite score to learn
    complexity_threshold: float = 0.6             # min score to stage ad-hoc task type
    require_user_identity: bool = True            # RPC without a principal → skip learning
    auto_apply_delta: bool = True                 # auto-promote to cortex.yaml once confidence met
    auto_apply_min_confidence: str = "medium"     # min confidence level for auto-apply
    auto_apply_min_confirmations: int = 3         # distinct principals required for auto-apply
    notify_on_apply: bool = True
    max_lesson_chars: int = 500                   # blueprint poisoning guard
    # Deprecated — retained for backwards-compat config parsing. Ignored at
    # runtime; consent prompts have been replaced by signal-driven learning.
    consent_enabled: Optional[bool] = None


class SecurityConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    max_input_tokens: int = 4000
    secret_scrub_patterns: List[str] = Field(default_factory=lambda: [
        r"Bearer \S+",
        r"api[_-]?key[_-]?=\S+",
        r"password[_-]?=\S+",
        r"token[_-]?=\S+",
    ])


class StartupConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    require_all_servers: bool = False
    discovery_timeout_seconds: int = 150
    log_discovered_tools: bool = True
    verify_auth: bool = True
    eager_discovery: bool = False  # when False, servers are probed on first use, not at startup
    capability_registry_path: Optional[str] = None  # persisted capability cache; auto-derived if None
    background_discovery_concurrency: int = 10  # max parallel probes during background discovery


class UserConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    allow_user_cortex_mcp: bool = True
    allow_user_tool_servers: bool = False


class CodeSandboxConfig(BaseModel):
    """Configuration for the sandboxed Python code execution capability.

    Script persistence is now driven by the autonomic learning gate — a
    generated script is saved to ``AgentCodeStore`` when its parent ad-hoc
    task passes the complexity + validation thresholds. See
    :class:`LearningConfig` for the governing knobs.
    """
    model_config = ConfigDict(extra='allow')
    enabled: bool = True
    timeout_seconds: int = 600
    allow_network: bool = False
    # Deprecated — retained for backwards-compat config parsing. Consent
    # prompts have been replaced by signal-driven learning.
    ask_persist_consent: Optional[bool] = None
    auto_add_to_yaml: bool = False      # If True, auto-apply the delta on stage
                                        # If False, only saves the script — developer applies YAML manually


class BlueprintConfig(BaseModel):
    """Per-task blueprint markdown files. Referenced by TaskTypeConfig.blueprint.

    Storage mode decides where blueprint content lives:
      - "filesystem": markdown files under `dir` (default: {base_path}/blueprints)
      - "backend":    stored via the configured StorageBackend (redis/sqlite)
                       under keys `blueprint:{name}`; `dir` is then only used
                       as a cache path for files a user edits by hand.

    staleness_warning_days is co-located here because it is a blueprint concern:
    it controls when a pinned task's stored topology is treated as stale
    and the LLM is directed to re-discover subtasks instead of following it.
    """
    model_config = ConfigDict(extra='allow')
    enabled: bool = False
    storage_mode: str = "filesystem"  # filesystem | backend
    dir: Optional[str] = None  # defaults to {storage.base_path}/blueprints
    auto_update: bool = True   # append lessons learned / bump version after runs
    inject_max_chars: int = 4000  # cap prompt injection size per blueprint
    staleness_warning_days: int = 90  # days since last successful run before blueprint is stale


class WorkspaceBashConfig(BaseModel):
    """Configuration for workspace-aware file read/write and command execution."""
    model_config = ConfigDict(extra='allow')
    enabled: bool = True
    hitl_enabled: bool = True  # cannot be False in production — enforced in framework init


class AntColonyConfig(BaseModel):
    """Configuration for the self-spawning ant agent subsystem."""
    model_config = ConfigDict(extra='allow')
    enabled: bool = False
    base_port: int = 8100
    max_ants: int = 20
    auto_restart: bool = True
    # Hatch ants automatically when CapabilityScout finds unresolved gaps
    auto_hatch_on_gap: bool = False
    # LLM profile the ant agents use (defaults to the parent's default provider)
    llm_provider: str = "default"
    llm_model: str = "claude-haiku-4-5-20251001"
    api_key_env_var: str = "ANTHROPIC_API_KEY"


class ToolForgeConfig(BaseModel):
    """Configuration for the ToolForge capability.

    ToolForge registers ``forge_mcp`` as a builtin capability when this section
    is enabled alongside ``code_sandbox`` and ``ant_colony``. The decomposition
    LLM can then create forge tasks that generate, spawn, and register new MCP
    servers at wave boundaries — available to dependent tasks in the same session.

    Forged servers persist to ``ants.yaml`` (``source='forged'``) and are
    supervised and restarted by AntColony exactly like developer-hatched ants.

    The ``forge_mcp`` capability only appears in the decomposition prompt when
    all three guards are true:
      - ``tool_forge.enabled``
      - ``code_sandbox.enabled``
      - ``ant_colony.enabled``
    """
    model_config = ConfigDict(extra='allow')
    enabled: bool = False
    # When True, forged servers survive framework restart (auto_restart=True in
    # ants.yaml). When False, the entry is written with auto_restart=False —
    # the server is not re-hatched on next startup, acting as session-scoped.
    persist_by_default: bool = False
    # Seconds to wait for the generated server process to pass health-check.
    # Separate from code_sandbox.timeout_seconds (codegen+exec) because spawn
    # failures have different causes and acceptable wait times.
    spawn_timeout_seconds: int = 30
    # LLM provider key used for MCP server code generation. Generating valid
    # FastMCP/stdio server code may warrant a more capable model than a typical
    # code_exec task. Defaults to "default" (the primary agent's provider).
    codegen_llm_provider: str = "default"


class CortexConfig(BaseModel):
    model_config = ConfigDict(extra='allow')
    agent: AgentConfig
    task_types: List[TaskTypeConfig] = Field(default_factory=list)
    tool_servers: Dict[str, ToolServerConfig] = Field(default_factory=dict)
    llm_access: LLMAccessConfig
    storage: StorageConfig
    sqlite: SQLiteConfig = Field(default_factory=SQLiteConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    file_input: FileInputConfig = Field(default_factory=FileInputConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    learning: LearningConfig = Field(default_factory=LearningConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    startup: StartupConfig = Field(default_factory=StartupConfig)
    user_config: UserConfig = Field(default_factory=UserConfig)
    code_sandbox: CodeSandboxConfig = Field(default_factory=CodeSandboxConfig)
    workspace_bash: WorkspaceBashConfig = Field(default_factory=WorkspaceBashConfig)
    blueprint: BlueprintConfig = Field(default_factory=BlueprintConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    ant_colony: AntColonyConfig = Field(default_factory=AntColonyConfig)
    tool_forge: ToolForgeConfig = Field(default_factory=ToolForgeConfig)
    adaptive_model_routing: AdaptiveModelRoutingConfig = Field(default_factory=AdaptiveModelRoutingConfig)
