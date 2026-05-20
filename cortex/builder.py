"""CortexBuilder — fluent, programmatic construction of a Cortex agent.

This is the code-first alternative to authoring ``cortex.yaml`` by hand. Build
a :class:`~cortex.config.schema.CortexConfig` in pure Python — LLM providers,
tool servers, task types, and Python *code nodes* — then hand the result
straight to :class:`~cortex.framework.CortexFramework`.

Two ways to define the work:

* ``.task(...)`` — an LLM-routed task type (capability-hint based). The
  decomposition LLM still plans the graph at runtime (``execution_mode``
  stays ``"planned"``).
* ``.node(...)`` — a Python callable wired in as a graph node. Registering
  even one code node flips the agent to ``execution_mode="static"``: the
  task graph runs verbatim, in dependency order, with no decomposition LLM
  call. This is the LangGraph-style path.

Example — a static DAG of two code nodes::

    from cortex import CortexBuilder, CortexFramework

    agent = CortexBuilder("ResearchAgent", "Searches the web, writes reports")
    agent.llm("anthropic", model="claude-sonnet-4-7",
              api_key_env="ANTHROPIC_API_KEY")
    agent.tool_server("brave", url="http://localhost:9000/sse")

    @agent.node()
    async def web_research(ctx):
        return await ctx.call_tool("brave", "search", query=ctx.request)

    @agent.node(depends_on=["web_research"])
    async def write_report(ctx):
        return await ctx.llm(f"Write a report from:\\n{ctx.deps['web_research']}")

    framework = CortexFramework(config=agent.build())
    await framework.initialize()
    result = await framework.run_session("user_1", "Research vector DBs")
    print(result.response)              # synthesised answer
    print(result.node_outputs["write_report"])   # raw node output
"""
from typing import Any, Callable, Dict, List, Optional

from cortex.config.schema import (
    AgentConfig,
    CortexConfig,
    LLMAccessConfig,
    LLMProviderConfig,
    StorageConfig,
    TaskTypeConfig,
    ToolServerConfig,
    ToolServerDiscoveryConfig,
    ValidationConfig,
)
from cortex.exceptions import CortexConfigError
from cortex.handler_registry import register_handler

__all__ = ["CortexBuilder"]


class CortexBuilder:
    """Fluent builder that assembles a :class:`CortexConfig` in Python.

    Every configuration method returns ``self`` so calls can be chained.
    :meth:`node` is the exception — it is a decorator and returns the
    decorated function unchanged.
    """

    def __init__(
        self,
        name: str,
        description: str = "",
        *,
        interaction_mode: str = "interactive",
    ) -> None:
        self._name = name
        self._description = description or name
        self._interaction_mode = interaction_mode
        self._default_provider: Optional[LLMProviderConfig] = None
        self._named_providers: Dict[str, LLMProviderConfig] = {}
        self._tool_servers: Dict[str, ToolServerConfig] = {}
        self._task_types: List[TaskTypeConfig] = []
        self._task_names: set = set()
        self._storage_base: str = "./cortex_storage"
        self._storage_kw: Dict[str, Any] = {}
        self._validation_kw: Dict[str, Any] = {}
        self._agent_kw: Dict[str, Any] = {}
        self._execution_mode: Optional[str] = None
        self._has_code_nodes = False
        self._extra_sections: Dict[str, Any] = {}

    # ── LLM providers ───────────────────────────────────────────────────────

    @staticmethod
    def _make_provider(
        provider: str,
        model: Optional[str],
        api_key_env: Optional[str],
        max_tokens: int,
        base_url: Optional[str],
        extra: Dict[str, Any],
    ) -> LLMProviderConfig:
        kw: Dict[str, Any] = {"provider": provider, "max_tokens": max_tokens}
        if model:
            kw["model"] = model
        if api_key_env:
            kw["api_key_env_var"] = api_key_env
        if base_url:
            kw["base_url"] = base_url
        kw.update(extra)
        return LLMProviderConfig(**kw)

    def llm(
        self,
        provider: str,
        *,
        model: Optional[str] = None,
        api_key_env: Optional[str] = None,
        max_tokens: int = 4096,
        base_url: Optional[str] = None,
        **extra: Any,
    ) -> "CortexBuilder":
        """Set the ``default`` LLM provider — the one every task uses unless
        it specifies ``llm_provider=``. Required before :meth:`build`."""
        self._default_provider = self._make_provider(
            provider, model, api_key_env, max_tokens, base_url, extra
        )
        return self

    def provider(
        self,
        key: str,
        provider: str,
        *,
        model: Optional[str] = None,
        api_key_env: Optional[str] = None,
        max_tokens: int = 4096,
        base_url: Optional[str] = None,
        **extra: Any,
    ) -> "CortexBuilder":
        """Register a *named* LLM provider, addressable via ``llm_provider=key``
        on a task or node (e.g. route synthesis to a flagship model)."""
        if key == "default":
            raise CortexConfigError(
                "CortexBuilder.provider(): use .llm() to set the 'default' provider"
            )
        self._named_providers[key] = self._make_provider(
            provider, model, api_key_env, max_tokens, base_url, extra
        )
        return self

    # ── Storage / tuning ────────────────────────────────────────────────────

    def storage(self, base_path: str = "./cortex_storage", **kw: Any) -> "CortexBuilder":
        """Set the storage base path (and any other StorageConfig fields)."""
        self._storage_base = base_path
        self._storage_kw.update(kw)
        return self

    def validation(self, **kw: Any) -> "CortexBuilder":
        """Override ValidationConfig fields, e.g. ``validation(threshold=0.8)``."""
        self._validation_kw.update(kw)
        return self

    def agent(self, **kw: Any) -> "CortexBuilder":
        """Override extra AgentConfig fields (synthesis_guidance, time, …)."""
        self._agent_kw.update(kw)
        return self

    def execution_mode(self, mode: str) -> "CortexBuilder":
        """Force ``"planned"`` or ``"static"`` execution.

        Usually unnecessary: registering a code node via :meth:`node` flips
        the agent to ``"static"`` automatically. Call this to run a static
        DAG built entirely from :meth:`task` (capability) nodes, or to keep
        an agent ``"planned"`` despite having code nodes.
        """
        if mode not in ("planned", "static"):
            raise CortexConfigError(
                f"execution_mode must be 'planned' or 'static', got {mode!r}"
            )
        self._execution_mode = mode
        return self

    def configure(self, **sections: Any) -> "CortexBuilder":
        """Escape hatch — merge raw config sections (dicts or pydantic models)
        into the final CortexConfig, e.g. ``configure(history={"enabled": True})``
        or ``configure(playwright_mcp=PlaywrightMCPConfig(enabled=True))``."""
        self._extra_sections.update(sections)
        return self

    # ── Tool servers ────────────────────────────────────────────────────────

    def tool_server(
        self,
        name: str,
        *,
        url: Optional[str] = None,
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        transport: Optional[str] = None,
        description: str = "",
        capability_hints: Optional[List[str]] = None,
        **kw: Any,
    ) -> "CortexBuilder":
        """Register an MCP tool server.

        Pass ``url=`` for an HTTP/SSE server or ``command=``/``args=`` for a
        stdio server. ``capability_hints`` seeds discovery routing.
        """
        cfg: Dict[str, Any] = {"name": name, "description": description}
        if url:
            cfg["url"] = url
            cfg["transport"] = transport or "sse"
        elif command:
            cfg["command"] = command
            cfg["args"] = list(args or [])
            cfg["transport"] = transport or "stdio"
        else:
            raise CortexConfigError(
                f"tool_server('{name}'): pass either url= or command="
            )
        if capability_hints:
            cfg["discovery"] = ToolServerDiscoveryConfig(
                capability_hints=list(capability_hints)
            )
        cfg.update(kw)
        self._tool_servers[name] = ToolServerConfig(**cfg)
        return self

    # ── Tasks (LLM-routed) ──────────────────────────────────────────────────

    def task(
        self,
        name: str,
        *,
        description: Optional[str] = None,
        capability: str = "auto",
        depends_on: Optional[List[str]] = None,
        output: str = "text",
        tool_servers: Optional[List[str]] = None,
        llm_provider: str = "default",
        mandatory: bool = True,
        timeout: int = 400,
        **kw: Any,
    ) -> "CortexBuilder":
        """Add an LLM-routed task type — the capability-hint style of task,
        equivalent to a ``task_types:`` entry in cortex.yaml. No Python code
        runs; the agent routes the task to a tool server or built-in by
        ``capability``."""
        self._add_task_type(TaskTypeConfig(
            name=name,
            description=description or f"Task: {name}",
            capability_hint=capability,
            depends_on=list(depends_on or []),
            output_format=output,
            tool_servers=list(tool_servers or []),
            llm_provider=llm_provider,
            mandatory=mandatory,
            timeout_seconds=timeout,
            **kw,
        ))
        return self

    # ── Nodes (Python code) ─────────────────────────────────────────────────

    def node(
        self,
        fn: Optional[Callable] = None,
        *,
        name: Optional[str] = None,
        depends_on: Optional[List[str]] = None,
        output: str = "text",
        description: Optional[str] = None,
        mandatory: bool = True,
        timeout: int = 400,
        llm_provider: str = "default",
        output_schema: Optional[Dict[str, Any]] = None,
        validation_notes: Optional[str] = None,
    ) -> Callable:
        """Register a Python callable as a graph node (decorator).

        Works bare (``@agent.node``) or parameterised
        (``@agent.node(depends_on=["fetch"])``). The decorated function takes
        a single :class:`~cortex.llm.context.TaskContext` argument and may be
        ``async`` or sync. Its return value becomes the node's output:

        * ``str``        → the output text
        * ``(str, fmt)`` → output text + explicit output_format
        * ``dict`` / ``list`` → JSON-serialised, output_format ``json``
        * ``None``       → empty output

        Registering any code node sets ``execution_mode="static"``.
        """

        def _decorate(func: Callable) -> Callable:
            node_name = name or func.__name__
            sentinel = register_handler(f"{self._name}::{node_name}", func)
            self._add_task_type(TaskTypeConfig(
                name=node_name,
                description=(
                    description
                    or (func.__doc__ or "").strip().split("\n")[0]
                    or f"Code node: {node_name}"
                ),
                complexity="scripted",
                handler=sentinel,
                # Non-"auto" hint so the executor skips capability inference;
                # the scripted-handler branch dispatches the node directly.
                capability_hint="code_node",
                depends_on=list(depends_on or []),
                output_format=output,
                mandatory=mandatory,
                timeout_seconds=timeout,
                llm_provider=llm_provider,
                output_schema=output_schema,
                validation_notes=validation_notes,
            ))
            self._has_code_nodes = True
            return func

        if callable(fn):
            return _decorate(fn)
        return _decorate

    # ── Build ───────────────────────────────────────────────────────────────

    def _add_task_type(self, tt: TaskTypeConfig) -> None:
        if tt.name in self._task_names:
            raise CortexConfigError(
                f"Duplicate task/node name '{tt.name}' in agent '{self._name}'"
            )
        self._task_names.add(tt.name)
        self._task_types.append(tt)

    def build(self) -> CortexConfig:
        """Validate and assemble the :class:`CortexConfig`.

        Raises :class:`CortexConfigError` if no LLM provider was set or a
        node references an unknown dependency.
        """
        if self._default_provider is None:
            raise CortexConfigError(
                f"CortexBuilder('{self._name}'): no LLM provider — "
                f"call .llm(provider=...) before .build()"
            )

        # Validate dependency references up front for a clear error message
        # (TaskGraphCompiler would also catch this at initialize()).
        for tt in self._task_types:
            for dep in tt.depends_on:
                if dep not in self._task_names:
                    raise CortexConfigError(
                        f"Node/task '{tt.name}' depends on unknown '{dep}'"
                    )

        exec_mode = self._execution_mode or (
            "static" if self._has_code_nodes else "planned"
        )
        agent = AgentConfig(
            name=self._name,
            description=self._description,
            interaction_mode=self._interaction_mode,
            execution_mode=exec_mode,
            **self._agent_kw,
        )
        llm_access = LLMAccessConfig(
            default=self._default_provider,
            providers=dict(self._named_providers),
        )
        storage = StorageConfig(base_path=self._storage_base, **self._storage_kw)

        cfg_kw: Dict[str, Any] = {
            "agent": agent,
            "task_types": list(self._task_types),
            "tool_servers": dict(self._tool_servers),
            "llm_access": llm_access,
            "storage": storage,
        }
        if self._validation_kw:
            cfg_kw["validation"] = ValidationConfig(**self._validation_kw)
        cfg_kw.update(self._extra_sections)
        return CortexConfig(**cfg_kw)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        mode = self._execution_mode or ("static" if self._has_code_nodes else "planned")
        return (
            f"CortexBuilder(name={self._name!r}, mode={mode}, "
            f"tasks={len(self._task_types)}, tool_servers={len(self._tool_servers)})"
        )
