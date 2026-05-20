# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.0] - 2026-05-15

Headline changes this release: a **code-first way to define agents** — build
the whole agent in Python, including LangGraph-style code nodes; a **ReAct
execution loop for every sub-agent**, replacing the old single-pass task
dispatch; two new automation capabilities — **AppControl** (cross-platform
desktop app control) and a built-in **Playwright** browser server; a
**full-featured `cortex` CLI** for running agents and managing sessions,
blueprints, MCPs, providers, and storage; and a prompt-engineering pass on the
framework's iterative LLM calls, where Cortex previously lost context between
repeated calls for the same unit of work.

### Added

#### ReAct sub-agent execution — every sub-task now reasons in a loop

Sub-agents no longer execute a task in a single LLM call. Once the primary
agent decomposes a request, each sub-task is run by a **ReAct (reason → act →
observe) loop**: the sub-agent's LLM picks one action, observes its result,
and repeats until it decides the task is done.

- **`ReactLoop`** (`cortex/modules/react_loop.py`) — drives the reason/act/
  observe cycle for one task. It owns the reasoning conversation; the agent
  owns action execution. Each step the model emits a JSON object with a
  `thought`, an `action`, an `action_input`, and an `expectation`; the loop
  runs the action and feeds the observation back **alongside the model's own
  stated expectation**, so every reasoning turn sees both what happened and
  what was intended.
- **Actions are the agent's capabilities** — `llm_synthesis`, `web_search`,
  `code_exec`, `bash`, `workspace_bash`, `forge_mcp`, and `ask_user` (when
  `human_in_loop` is set), the new `app_control` capability (see below), plus
  every registered MCP tool-server capability. The action menu is built per
  task from what the agent actually has wired up.
  `GenericMCPAgent._execute_action()` runs one action and returns an
  observation; the old `_infer_capability_hint()` router and single-pass
  capability dispatch are gone — the loop's reasoning step replaces them.
- **No flag — it is the execution model.** Every non-scripted task runs the
  loop; scripted code-node handlers still run their Python directly (no LLM
  step to loop over). The loop terminates as soon as the sub-agent emits a
  `finish` action.
- **Context management between iterations** — observations are truncated to
  `react.observation_max_tokens`; once the running conversation passes
  `react.context_char_budget` the oldest steps are digested into a compact
  summary appended to the opening instruction. A failed action becomes an
  observation the loop can adapt to rather than aborting the task.
- **`TaskTypeConfig.react`** (`ReactConfig`) — `max_iterations` (default `10`;
  a safety cap that forces a best-effort final answer), `observation_max_tokens`
  (default `600`), and `context_char_budget` (default `24000`). There is no
  enable/disable knob — the loop is always on for LLM-driven tasks.
- **Wave validation still wraps the loop** — on a validation retry, prior
  attempts and judge feedback are threaded into the loop's opening instruction.
  Token usage, generated scripts, forged servers, and produced files are
  aggregated across every step onto the `ResultEnvelope`.
- New prompt fragments `REACT_SYSTEM`, `REACT_USER_INITIAL`,
  `REACT_OBSERVATION`, `REACT_UNKNOWN_ACTION`, `REACT_FORCE_FINAL`,
  `REACT_BUILTIN_ACTIONS`, and `REACT_COMPACTION_HEADER` in `cortex/prompts.py`.

#### Code-first agents — `CortexBuilder`, code nodes, and static DAGs

Cortex agents no longer require a `cortex.yaml` file. The whole agent —
providers, tool servers, task graph — can be built in Python, and individual
graph nodes can be plain Python functions.

- **`CortexBuilder`** (`cortex/builder.py`) — a fluent builder that assembles a
  `CortexConfig` in code: `.llm()`, `.provider()`, `.tool_server()`, `.task()`,
  `.node()`, `.storage()`, `.validation()`, `.configure()`, `.build()`.
- **`CortexFramework(config=...)`** — the framework constructor now accepts a
  pre-built `CortexConfig` object, not just a YAML path. No file is read.
- **`@builder.node` decorator** — register a Python callable as a graph node.
  The function receives a `TaskContext` and returns its output (string, tuple,
  dict/list → JSON, or `None`). Works bare or parameterised
  (`@agent.node(depends_on=["fetch"])`).
- **Static-DAG execution** — `AgentConfig.execution_mode` (`"planned"` |
  `"static"`). Registering any code node flips the agent to `"static"`: the
  task graph runs verbatim in dependency order with **no decomposition,
  intent-gate, or capability-scout LLM calls**. The fan-out/fan-in wave engine,
  validation gate, retries, streaming events, and envelope store are all
  reused unchanged.
- **Enriched `TaskContext`** — code nodes get `ctx.request`, `ctx.deps`
  (upstream node outputs keyed by node name), `await ctx.llm(prompt)`, and
  `await ctx.call_tool(server, tool, **params)`, wiring them into the same
  LLM providers and MCP tool servers the rest of the agent uses.
- **`SessionResult.node_outputs`** — per-node raw outputs keyed by node name,
  populated for every task-mode session.
- **`run_session(event_queue=...)` is now optional** — omit it when you only
  want the returned `SessionResult`; an internal queue is created and discarded.
- **`cortex.handler_registry`** — in-process registry backing code-node
  handlers, addressed by the `cortex:node:<id>` sentinel scheme. Dotted-path
  handlers (`"module.function"`) in `cortex.yaml` continue to work unchanged.
- New top-level exports: `CortexBuilder`, `CortexConfig`, `TaskContext`,
  `LLMResponse`, `TokenUsage`.

#### `ValidationConfig.enabled`

- The post-synthesis Validation Agent's master switch is now a declared schema
  field (default `true`) instead of an extra key, so a config built in code
  has a sensible default. `cortex.yaml` files may still set it explicitly.

#### Session context for sub-tasks — `agent.inject_session_context`

- **`AgentConfig.inject_session_context`** — new `cortex.yaml` key under `agent:` (default `true`). When enabled, every LLM-synthesis sub-task receives the original user request and the planner's current reasoning scratchpad in its system prompt.
- **`RuntimeTask.session_goal` / `session_scratchpad`** — transient fields refreshed by the framework at each wave dispatch (not serialized; re-populated on resume). Previously a worker sub-agent saw only its own instruction and ran blind to the session.
- New prompt fragments `TASK_EXEC_SESSION_CONTEXT` and `TASK_EXEC_SESSION_SCRATCHPAD_LINE` in `cortex/prompts.py`. The worker is still instructed to produce output for its own task only — the context is for consistency, not scope expansion.
- The request is truncated to ~800 chars and the scratchpad to ~1500 chars to bound per-call token overhead; set the flag to `false` for budget-sensitive deployments.

#### Iterative remediation — `validation.max_remediation_attempts`

- **`ValidationConfig.max_remediation_attempts`** — new `cortex.yaml` key under `validation:` (default `2`). A sub-threshold response is now remediated over multiple passes instead of a single shot.
- Each pass after the first receives the prior attempt's response and the findings it still failed on (`REMEDIATE_PRIOR_ATTEMPT` prompt fragment), so the model corrects without repeating a fix that already proved insufficient.
- Set to `1` for the legacy single-shot behaviour.

#### AppControl — cross-platform desktop application control

A new `app_control` capability lets a sub-agent launch and drive native
desktop applications on macOS, Windows, and Linux. Disabled by default;
enable with `app_control.enabled: true`.

- **`cortex/modules/app_control.py`** — `AppControl` (action execution) and
  `AppCapabilityScout` (automation-interface discovery).
- **Two-path execution model.** *Primary:* discover the app's scripting
  dictionary — macOS `sdef` XML, Windows UI Automation tree / COM type-library
  introspection, Linux AT-SPI / `xdotool` — and inject a compact summary into
  the LLM prompt so it generates actions against the app's real API.
  *Fallback:* when no scripting dictionary exists, enter a **screenshot vision
  loop** — capture the screen, ask a vision-capable LLM for the next action,
  execute it, and repeat up to `max_vision_steps` times.
- **HITL is mandatory.** Every mutating action (launch, script, screenshot)
  requires user approval; read-only queries (`get_running_apps`,
  `get_window_text`) never prompt. `app_control.hitl_enabled` cannot be
  disabled — the framework forces it back to `true` at init.
- **`AppControlConfig`** — new `cortex.yaml` block: `enabled` (default
  `false`), `hitl_enabled`, `timeout_seconds` (`30`), `sdef_max_chars`
  (`8000`), `max_vision_steps` (`10`), and `vision_provider` (`"default"`).
- Wired into the ReAct action menu as `app_control` and dispatched by
  `GenericMCPAgent._call_app_control()`. New prompt fragments
  `APP_CONTROL_SYSTEM`, `APP_CONTROL_USER`, `APP_CONTROL_WITH_CAPS_USER`, and
  `APP_CONTROL_VISION_USER` in `cortex/prompts.py`.

#### Built-in Playwright MCP server — browser automation

- **`PlaywrightMCPConfig`** — new `cortex.yaml` block. When
  `playwright_mcp.enabled: true`, the framework starts `@playwright/mcp` as an
  internal stdio MCP server at startup and grants the agent a `browser`
  capability automatically. It is **not** listed under `tool_servers` — users
  don't configure it as an external server. Requires Node.js / `npx` on PATH.
- Options: `browser` (`chromium` | `firefox` | `webkit`), `headless`
  (default `false` — visible window), `startup_timeout_seconds` (`60`),
  `viewport_width` / `viewport_height` (`1280×720`), and session persistence
  via either `storage_state_path` (cookies + localStorage; defaults to
  `{storage_base}/playwright_session.json`) or `user_data_dir` (full
  persistent context — `user_data_dir` wins if both are set).
- If the server fails to start, the framework logs a warning and continues —
  the `browser` capability is simply unavailable rather than aborting init.

#### Expanded `cortex` CLI — run agents and manage state from the terminal

Nine new commands registered in `cortex/cli/main.py`, alongside the existing
`setup` / `dev` / `dry-run` / `ants` / `config-ui` commands:

- **`cortex run`** — run a single request through the agent and print the
  response, streaming framework events.
- **`cortex chat`** — interactive chat REPL, with mid-run message injection
  (polls stdin and injects into the live session).
- **`cortex sessions`** — `list`, `show`, `delete`, and `export` (JSON or
  Markdown) session history.
- **`cortex blueprints`** — `list`, `show`, `edit` (in `$EDITOR`), and
  `delete` per-task learning blueprints.
- **`cortex mcps`** — `list`, `test`, `add`, and `remove` external MCP
  servers in the auto-discovered registry.
- **`cortex stats`** — token usage and session statistics, per-user or
  aggregate, in text or JSON.
- **`cortex config`** — `validate` (`cortex.yaml`, with `--strict`) and
  `show` a summary of the loaded configuration.
- **`cortex providers`** — `list` configured LLM providers and `test`
  reachability with a minimal prompt.
- **`cortex storage`** — `status` (paths, sizes, backend health) and
  `purge` (delete session history older than N days).

Documented in full in `docs/CLI.md`.

### Changed

#### Iterative, best-candidate remediation

- `ValidationAgent.validate_with_remediation()` loops up to `max_remediation_attempts` times. If no pass clears `threshold`, the **best-scoring** candidate across the original response and all attempts is delivered — previously a failed remediation always fell back to the original even when the remediation scored higher.
- `PrimaryAgent.remediate()` gains a `prior_attempts` argument (the `(response, findings)` history) and a `stream` flag. Intermediate passes run with `stream=False`, so discarded attempts are never streamed to the user — `validate_with_remediation()` emits only the single response that is actually delivered.

#### Richer decomposition history

- The prior-session snippet fed into the decomposition prompt now includes each session's **outcome** — task completion counts (`4/5 tasks completed (1 failed)`) and the validation verdict — via the new `_format_history_record()` helper, not just request/summary. The decomposer can now weigh whether a similar plan shape succeeded before. Request/summary truncation also switched to head-and-tail so trailing detail survives.

#### Conversational retry-with-feedback in the wave validation gate

- When a task fails the validation gate, the retry no longer just appends a `[RETRY FEEDBACK]` blob to the instruction. The failed attempt is recorded on the new **`RuntimeTask.attempt_history`** field as an `{output, feedback}` pair, and `GenericMCPAgent._call_llm()` threads those pairs into the next call as real conversation turns (`user → assistant → user → …`).
- The model now sees **its own prior output** followed by the judge's feedback, so it can fix only what was flagged instead of regenerating blind. Attempts accumulate — attempt 3 sees both attempt 1 and attempt 2, preventing oscillation between two failure modes.
- A "Retry Context" stanza is added to the sub-agent system prompt on retries (`This is attempt N of 3 …`).
- `_execute_once()` now splits a clean `base_instruction` from the feedback-appended `full_instruction`; non-LLM dispatch paths (code_exec, bash, app_control, MCP tool calls) keep the appended-block behaviour since they don't take a message list.
- `attempt_history` is included in `RuntimeTaskGraph` snapshot/restore.

#### Context-rich replan prompt

- **`PrimaryAgent.replan()`** accepts a new `trigger_reason` argument and `REPLAN_USER` surfaces it. The framework's wave loop builds a specific label — `mandatory_failure:<task>`, `stale_blueprint:<task>`, or `adaptive_completed` — so the replanner knows *why* it was invoked instead of inferring intent from completed-task content.
- Pending tasks are now rendered with their full **instruction and `depends_on` edges**, not just their names — a `modify`/`remove` op now acts on a task body the LLM has actually seen.
- Completed-task and pending-task summaries use a new `_head_tail()` helper (head + tail truncation) instead of a plain `[:300]` / `[:1000]` slice, so trailing URLs, IDs, and error codes survive truncation.
- The same enrichment (`trigger_reason` aside) is applied to `handle_user_interrupt()` so an in-flight user interrupt sees the same quality of context.

#### UI surface — Synapse mid-run interrupt, Config Studio & Setup Wizard

- **Synapse — mid-run interrupt.** The composer now stays active while a session streams: typing a message and pressing send (`↪`) or `Cmd+Enter` injects it via the new `POST /api/session/{ui_id}/interrupt` endpoint, which calls `framework.inject_user_message()`. An empty composer still cancels the session. The injected message renders as a dashed "mid-run" bubble, and `user_interrupt` SSE events update it with the framework's decision (`queued` → `replanning` / `stopping`).
- **Synapse — ReAct progress.** The Task Graph rail and inline task stream now show a `↻ step N · action` badge per task, read from the `react_step` / `action` metadata already carried on `status` events.
- **Synapse — HITL options.** Approval cards render the clarification event's own `options` as buttons (so App Control's `yes` / `no` prompts get matching buttons), falling back to approve/deny when no options are supplied.
- **Config Studio.** Added `validation.enabled`, `validation.max_remediation_attempts`, and `validation.expose_report_to_user` to the validation settings, and a per-task-type **ReAct Loop** group (`react.max_iterations`, `react.observation_max_tokens`, `react.context_char_budget`).
- **Setup Wizard.** Added a `max_remediation_attempts` field to the validation step. The generated `validation` block now writes `enabled` explicitly and the loader reads it — previously disabling validation omitted the block entirely, which let the schema default (`enabled: true`) silently switch it back on.

#### Auto-tuned LLM concurrency

- **`max_parallel_llm_calls` is now auto-derived.** New module **`cortex/llm/model_power.py`** maps `provider:model` patterns to a sensible starting ceiling — `1` for `local:*`, `8` for `anthropic:*haiku*` / `openai:*mini*` / `gemini:*flash*`, `6` for `anthropic:*sonnet*` / `openai:*gpt-4o*`, `4` for `anthropic:*opus*` / `openai:*gpt-4*`, falling back to `2` for unknowns. Picked at startup and logged (`max_parallel_llm_calls auto-derived: N (provider:model)`).
- **`AdaptiveLLMGate` is now actually wired in.** The pre-existing AIMD self-tuning gate (`cortex/llm/adaptive_gate.py`) replaces the static `asyncio.Semaphore` inside `LLMClient` whenever `agent.concurrency.adaptive_llm_concurrency` is true (the default). Each `complete()` / `stream()` call reports `ok`, `empty`, and `latency` to the gate; it halves multiplicatively on bad signals (errors, empty responses, latency spikes >2.5× the best observed) and grows additively after sustained clean-and-saturated streaks. Queue-wait credit (`_credit_gate_wait`) is preserved.
- **Setup wizard.** The "Max parallel LLM calls" field is removed from the Runtime & delivery step; Config UI keeps the field as an optional override with an updated hint ("leave blank for default behavior").
- **Schema.** `agent.concurrency.max_parallel_llm_calls` is now `Optional[int]` with default `None` (= auto-derive). Existing configs that set an explicit integer are honoured as a pin — backward-compatible. Set `adaptive_llm_concurrency: false` to disable self-tuning and pin the gate at the value exactly.
- **Docs.** See [`docs/CONFIGURATION.md` § "LLM concurrency auto-tuning"](docs/CONFIGURATION.md#llm-concurrency-auto-tuning).

### Fixed

- **`CredentialScrubber` now masks Anthropic API keys.** The `sk-` scrub pattern matched only `[A-Za-z0-9]`, so it stopped at the first hyphen and left Anthropic keys (`sk-ant-api03-…`) exposed in logs and streamed output. A dedicated `sk-ant-…` pattern was added.
- **`LLMClient` accepts the `adaptive_llm_concurrency` kwarg.** `framework.py` was already passing this kwarg when constructing the client, but `LLMClient.__init__` didn't declare it — a startup `TypeError` waiting to fire. Now wired through correctly and gates on the value.

## [1.4.1] - 2026-05-08

### Fixed

#### Workspace path resolution — first-class `default_workspace` config

Previously, `WorkspaceBash` had no concept of a default workspace. It relied on a regex (`extract_workspace_path`) that tried to fish an absolute path out of free-form task instruction text, and the Synapse UI worked around this by prepending `WORKSPACE: /path\n\n` to every outgoing message. This was fragile (paths in instruction text could be misidentified as the workspace) and broken for Docker deployments where host paths are invalid inside the container.

**Changes:**

- **`WorkspaceBashConfig`** — new `default_workspace: Optional[str]` field in `cortex.yaml` under `workspace_bash:`.
- **`WorkspaceBash`** — accepts `default_workspace` at init; exposes `set_default_workspace(path)` for runtime updates.
- **`framework.py`** — `CORTEX_DEFAULT_WORKSPACE` environment variable overrides `cortex.yaml` at startup (useful for Docker `docker run -e CORTEX_DEFAULT_WORKSPACE=/workspace`).
- **`GenericMCPAgent._call_workspace_bash`** — workspace resolution is now: (1) `WORKSPACE:` field in structured instruction (per-task MCP override), then (2) `_default_workspace`. The `extract_workspace_path` regex is removed entirely.
- **Synapse UI** — `GET /api/workspace` seeds the sidebar on page load from the server-configured default. `POST /api/workspace` persists the user's sidebar input back to `cortex.yaml` and updates the live instance. The `WORKSPACE:` message prepend is removed; workspace is now server-side state.

**Precedence (highest → lowest):** `WORKSPACE:` in instruction → `CORTEX_DEFAULT_WORKSPACE` env var → `workspace_bash.default_workspace` in `cortex.yaml` (updated live via UI sidebar).

## [1.4.0] - 2026-05-07

### Added

#### Built-in Web Search

- **`DuckDuckGoSearch`** (`cortex/modules/builtin_search.py`) — zero-config web search via DuckDuckGo Lite. No API key required. Scrapes `lite.duckduckgo.com` and the instant-answer API; returns formatted markdown results (up to 8 results by default). Includes a fallback chain: configured tool server → built-in DuckDuckGo.
- **`agent.builtin_web_search_enabled`** config key (default `true`) — controls whether the built-in search is activated. When enabled, `web_search` is always registered as an available capability.
- `GenericMCPAgent` routes `web_search` capability hits through the fallback chain automatically.

#### Adaptive Model Routing (AMR)

- **`AdaptiveModelRoutingConfig`** — new `adaptive_model_routing` config block: `enabled` (default `false`), `tiers` (`ComplexityTierMap` mapping `low`/`medium`/`high` to provider keys), and `validation_provider` (auto-selects if empty).
- **`ComplexityTierMap`** — Pydantic model mapping the three complexity tiers to named LLM provider keys defined in `llm_access`.
- Decomposer LLM now emits a `<model_tier>` tag (`low`|`medium`|`high`) per task when AMR is enabled. `PrimaryAgent._amr_resolve_provider()` maps the tier to a provider key; `_amr_validation_provider()` selects the validation provider (explicit or auto-pick first non-default).
- `DecomposedTask` carries two new optional fields: `complexity_tier` and `llm_provider`. `TaskGraphCompiler` applies the AMR-resolved provider at `RuntimeTask` instantiation via `model_copy(update={...})`.
- Ant Colony subprocesses inherit `amr_config` and the parent's provider pool so sub-task routing mirrors the orchestrator's tier policy.
- System prompt includes detailed complexity-tier assessment guidelines when AMR is active.

#### ToolForge — Runtime MCP Server Code Generation

- **`ToolForgeConfig`** — new `tool_forge` config block: `enabled` (default `false`), `persist_by_default` (default `false`), `spawn_timeout_seconds` (default `30`), `codegen_llm_provider` (default `"default"`).
- New capability hint `forge_mcp` registered when `tool_forge.enabled`, `code_sandbox.enabled`, and `ant_colony.enabled` are all `true`.
- **`GenericMCPAgent._call_forge_mcp()`** — generates an MCP server Python script via the code sandbox, writes it to `{storage_base}/ants/{task_name}/server.py`, and returns the script path in the result envelope (`forged_server_path`).
- **`AntColony.hatch_from_script()`** — spawns a pre-written MCP server, performs a `/health` check, and optionally persists it to `ants.yaml` with `source='forged'` and `auto_restart` metadata.
- Wave-boundary hook in `framework.py`: after each wave, envelopes with a `forged_server_path` trigger `hatch_from_script()` so dependent tasks in the next wave see the new capability. Failures are non-fatal — the session continues.

#### Expanded Streaming Event Suite

Seven new typed event classes in `cortex/streaming/status_events.py`:

| Event class | `EventType` value | Purpose |
|---|---|---|
| `TaskToolCallEvent` | `TASK_TOOL_CALL` | Emitted before every tool server invocation |
| `TaskBlueprintEvent` | `TASK_BLUEPRINT` | Full DAG (tasks, dependencies, wave assignments) after decomposition |
| `IntentClassifiedEvent` | `INTENT_CLASSIFIED` | Intent Gate decision with confidence and reasoning |
| `SynthesisTierEvent` | `SYNTHESIS_TIER` | Excerpt tier selected during synthesis context assembly |
| `WorkspaceEvent` | `WORKSPACE_EVENT` | File I/O operation (created/modified/deleted/listed) performed by WorkspaceBash |
| `FileOutputEvent` | `FILE_OUTPUT` | File written as a task output (name, MIME type, size) |
| `SessionTokenUsageEvent` | `SESSION_TOKEN_USAGE` | Cumulative input/output/cache token counts at session end |

#### Stdio MCP Transport

- **`_StdioMCPSession`** (`cortex/modules/tool_server_registry.py`) — full MCP-over-stdio session: spawns the server process, performs the `initialize` / `notifications/initialized` handshake, exposes `list_tools()` and `call_tool()`, and tears down cleanly on exit.
- `ToolServerConnection` gains a `stdio_session` field for the active stdio session object.
- `GenericMCPAgent._call_stdio_tool_server()` — routes tasks to stdio MCP servers.
- `GenericMCPAgent._map_instruction_to_args()` — maps natural-language task instructions to MCP tool argument schemas (search → `query`, URL tools → extracted URL, fallback → first required property).

#### Cortex Synapse Chat UI — Complete Redesign

- `cortex/ui/static/index.html` fully rewritten. Stack: Tailwind CSS (CDN), Alpine.js, marked.js, highlight.js.
- Features: live SSE streaming with markdown rendering, intent classification badge, task blueprint DAG before execution, per-task progress chips, tool-call indicators, send-time and mid-session file uploads, output file download + session artifact ZIP, workspace event feed, inline HITL clarification prompts, token usage footer, session history sidebar with full-text search, service launcher (Config Studio, Setup Wizard).

Five new server endpoints in `cortex/ui/server.py`:

| Endpoint | Purpose |
|---|---|
| `POST /session/{id}/clarify` | HITL answer ingestion |
| `POST /session/{id}/upload` | Mid-session file upload (multipart) |
| `GET /session/{id}/artifacts.zip` | ZIP download of all output files |
| `GET /history/search` | Full-text search across sessions |
| `POST /session/{id}/ant-stop` | Request ant task cancellation |

- `GET /history/{session_id}/file/{filename}` updated with `?inline=1` query parameter for in-browser preview.

#### Learning Engine — Manual Delta Control

- **`LearningEngine.promote_delta(task_name)`** — force-applies a staged delta by name, bypassing the confidence gate.
- **`LearningEngine.discard_delta(task_name)`** — removes a staged delta from `pending.yaml`.
- **`LearningEngine.set_cortex_yaml_path(path)`** — stores the config path used by `promote_delta`'s `apply_delta()` call.
- `CortexFramework` calls `set_cortex_yaml_path` during `initialize()` so the path is always wired up.

#### Setup Wizard — AMR & ToolForge Configuration

- New wizard toggles: `builtin_web_search_enabled`, `tool_forge_enabled` (gated on ant_colony + code_sandbox), `amr_enabled`, per-tier provider mappings (low/medium/high), and `validation_provider`.
- `_load_existing_config` parses and surfaces all new options from an existing `cortex.yaml`.
- Docker publish in the wizard now accepts a `--with-ui` flag.
- MCP and UI server publish flows start as non-blocking background `Popen` with correct endpoint hints.
- Logo updated to `cortex-logo-new-v1.svg`.

#### Documentation

- **`docs/CORTEX_SYNAPSE.md`** — new comprehensive guide to the Cortex Synapse chat UI: features overview, full REST API reference (sessions, history, runtime management, configuration), authentication modes (none/token/basic), headless curl examples, and technology stack notes.

### Changed

- **Primary Agent system prompt** now includes a human-readable capability descriptions table so the decomposer understands the purpose of each available capability. Includes explicit guidance: use `web_search` for live data, `workspace_bash` for file I/O, `llm_synthesis` for pure reasoning; never refuse by saying it cannot create files.
- **`CortexFramework.run_session()`** emits `IntentClassifiedEvent` (after Intent Gate), `TaskBlueprintEvent` (after decomposition), and `SessionTokenUsageEvent` (at session end).
- **`AntColony._build_ant_yaml()`** replaces the old hardcoded template with a builder function that injects AMR config and provider pool into each generated `cortex.yaml`.
- **`cortex publish mcp`** (`cortex/cli/publish.py`) now runs a real aiohttp server at `POST /mcp` (alias `POST /run`) that calls `run_session()` and returns the response. Previously logged a placeholder. Sets `CORTEX_INTERACTION_MODE=rpc`.
- **`cortex publish package`** uses `sys.executable` instead of a hardcoded `"python"` binary.
- **`cortex publish ui`** shows the `localhost` URL to the user even when the server binds to `0.0.0.0`.

### Internal

- `available_capabilities` derived from tool registry + `llm_synthesis` + `workspace_bash` flags and passed to `GenericMCPAgent` for inclusion in the LLM context.
- `ResultEnvelope` carries two new optional fields: `task_name` (string, not just ID) and `forged_server_path` (path to a ToolForge-generated script).

---

## [1.3.1] - 2026-05-01

### Added

- **`WorkspaceBash`** (`cortex/modules/workspace_bash.py`) — workspace-scoped file read/write and command execution with hardcoded HITL gating. Read-only ops (`read_file`, `list_dir`) never prompt; mutating ops (`write_file`, `execute`) fire a mandatory `ClarificationRequestEvent` before acting. `write_file` shows a unified diff when the file already exists. `hitl_enabled` is enforced `true` at runtime.
- **`HITLRelayServer`** — lightweight aiohttp server spawned per-session so ant subprocesses can relay HITL prompts to the parent framework event queue via `CORTEX_HITL_URL`.
- **`cortex config-ui`** CLI command — launches the Cortex Config Studio browser UI at `localhost:7801` for inspecting and editing `cortex.yaml`, blueprints, staged deltas, and session metadata (`cortex/config_ui/`).
- **`CortexHITLDeniedError`** — exported from the top-level `cortex` package; raised when a WorkspaceBash HITL prompt is denied or times out.
- **`workspace_bash` config block** — `WorkspaceBashConfig` added to `CortexConfig` schema with `enabled` (default `true`) and `hitl_enabled` (enforced `true`).

### Fixed

- `cortex.config_ui` static files added to `pyproject.toml` package-data so the Config Studio assets are bundled in the wheel.
- `.gitignore` updated to suppress generated local files (`Dockerfile.cortex`, `META_PROMPT_CONFIG_UI.md`, `docs/WORKSPACE_BASH_DESIGN.md`).
- Stale lifecycle steps in `ARCHITECTURE.md` (steps 14–15) corrected — replaced old consent-prompt language with the autonomic learning gate description introduced in 1.3.0.
- Docs fully updated across `ARCHITECTURE.md`, `CONFIGURATION.md`, `CLI.md`, `FEATURES.md`, and `GETTING_STARTED.md` to cover WorkspaceBash, Config Studio, `CORTEX_HITL_URL`, and the autonomic learning gate.

---

## [1.3.0] - 2026-04-24

### Added

#### Autonomic learning (replaces evolution consent)

- **Signal-driven learning gate**: `CortexFramework.run_session()` now decides whether to stage deltas / refine blueprints from observable signals at end of session — the previous `ask_persist_consent` clarification prompt is gone. The gate skips chat turns, RPC calls with no principal (configurable), and sessions below the validation or complexity thresholds.
- **`TaskComplexityScorer`** (`cortex/modules/task_complexity_scorer.py`) — deterministic 0.0–1.0 scorer combining six signals with fixed weights: code synthesis (0.35), tool-trace length (0.20), decomposed-task count (0.15), has-dependencies (0.15), total tokens (0.10), duration (0.05). Emits a structured `ComplexityBreakdown` for observability.
- **Draft blueprints on first stage**: `LearningEngine.persist_evolution()` seeds a draft blueprint under `drafts/{task_name}__{hash}` the moment a new ad-hoc task is staged, pre-populated with the observed tool trace, validator findings (as initial don'ts), and a session lesson summary. Promoted to a permanent `blueprint:` reference on `apply_delta()`.
- **`auto_apply_delta: true` by default**: staged proposals promote themselves into `cortex.yaml` as soon as the distinct-principal confirmation threshold is met (default `medium` = 3). Explicit `cortex delta apply` remains available for manual workflows (`auto_apply_delta: false`).
- **`LearningEvent`** streaming event — emitted once per session with `action`, `complexity_score`, `validation_score`, `intent_mode`, and the staged / applied task lists. Mirrored into `HistoryRecord.learned_action` (and a new `HistoryRecord.complexity_score` column).
- **`ObservabilityEmitter.emit_complexity_score()`** — per-session complexity breakdown on the operational stream.

#### Synthesis (carried forward from earlier 1.3.0 changes)

- **Synthesis Tier 1 — smart excerpt**: `assemble_context` now builds a keyword-grep excerpt from each file-output task (up to 8 000 chars) instead of blindly truncating to the first 2 000 chars. Keywords are derived from the task label and content summary; `head` is used as a fallback when grep returns nothing.
- **Synthesis Tier 2 — iterative file summarisation**: When completed tasks produce file outputs, up to 3 concurrent LLM `complete()` calls summarise each file in the context of its task instruction before the final synthesis pass. Summaries replace the Tier 1 excerpt for those files. Fires automatically — no developer configuration required.
- **Synthesis file output**: When file-output envelopes are present, the synthesised response is written to `synthesis_{session_id}.md` in the session storage path. A `ResultEvent` with `metadata.output_type="file"` carries the path to the caller. Falls back to text streaming on write failure.
- **Scratchpad in synthesis**: `synthesise()` now accepts a `scratchpad` parameter. The accumulated session reasoning trace (confirmed facts, open questions, strategy) built up during replanning is injected as a `## Session Reasoning` block so the final response is informed by mid-session observations. All three `synthesise()` call sites in `framework.py` pass `primary._scratchpad`.

### Changed

- **`learning` config block** — reshaped around the autonomic gate. New keys: `validation_threshold` (0.75), `complexity_threshold` (0.6), `require_user_identity` (true), `auto_apply_delta` (true), `auto_apply_min_confidence` (`medium`), `max_lesson_chars` (500).
- **`LearningEngine.persist_evolution()`** signature now accepts `complexity_score` and `decomposed_tasks` (both optional). The old `user_consent` parameter is removed from the engine. The framework parameter `run_session(user_consent=...)` is retained as an inert record-shape field (written into `HistoryRecord.user_consent`) for API compatibility.
- **Setup wizard** — the *Learning engine* section now exposes `validation_threshold`, `complexity_threshold`, `require_user_identity`, and `max_lesson_chars`; the old "Ask before persisting scripts" toggle is removed.

### Deprecated

- **`CortexFramework.resolve_evolution_consent()`** — no-op in 1.3.0 (logs a one-time deprecation warning and returns `False`). Autonomic learning no longer emits a consent prompt to resolve.
- **`learning.consent_enabled`** and **`code_sandbox.ask_persist_consent`** — accepted for parsing, ignored at runtime. Remove from `cortex.yaml` when convenient.

#### WorkspaceBash — workspace-aware file and command execution

- **`WorkspaceBash`** (`cortex/modules/workspace_bash.py`) — read, write, and execute files and shell commands scoped to a declared workspace directory. Path traversal is blocked at resolve time.
  - `read_file` and `list_dir` are read-only (no HITL prompt).
  - `write_file` and `execute` fire a mandatory HITL `ClarificationRequestEvent` before acting; write shows a unified diff when the file already exists.
  - `hitl_enabled` is enforced `True` in `framework.py` regardless of config value — cannot be disabled at runtime.
  - Path safety: all paths resolved against the workspace root; absolute references outside the root in shell commands are blocked.
- **`HITLRelayServer`** — lightweight aiohttp server spawned per-session so that ant subprocesses can relay HITL prompts to the parent framework event queue via `CORTEX_HITL_URL`.
- **`workspace_bash` config block** — new `CortexConfig.workspace_bash` field (`WorkspaceBashConfig`) with `enabled` (default `true`) and `hitl_enabled` (enforced `true`).
- **`CortexHITLDeniedError`** — raised when the user denies a WorkspaceBash HITL prompt or the prompt times out. Carries `operation` (`"write"` / `"execute"`) and `path`. Now exported from the top-level `cortex` package.

#### Config Studio — browser-based framework config browser

- **`cortex config-ui`** — new CLI command that launches the Cortex Config Studio on `localhost:7801`. Loads the live `cortex.yaml`, all stored blueprints, staged deltas, and session metadata into a read/edit browser UI.
  - Flags: `--config`, `--port`, `--host`, `--no-browser`, `--storage-base`.
  - Server lives in `cortex/config_ui/` (aiohttp + static bundle).

### Internal

- `_EXCERPT_MAX_CHARS = 8_000`, `_ITERATIVE_MAX_FILES = 3`, `_ITERATIVE_SUMMARY_TOKENS = 400` defined as module-level constants in `primary_agent.py` — not developer-configurable; derived and applied at runtime.
- `_PENDING_EVOLUTION_CONSENTS` module-level dict removed from `framework.py`.

---

## [1.2.0] - 2026-04-19

### Added

- **Intent Gate** — pre-scout classifier that routes each turn as `chat`, `task`, or `hybrid`. Stage 1 uses cheap heuristics (greeting lexicon, task verbs, known task-type names, file attachments); Stage 2 falls through to a small LLM classifier only on ambiguity. Enables conversational UIs to respond directly to small talk without running the full decompose → execute → synthesise pipeline.
  - New module `cortex.modules.intent_gate` with `IntentGate` class and `IntentDecision` dataclass.
  - New config block `agent.intent_gate` (`enabled`, `heuristic_confidence_threshold`, `llm_provider`, `timeout_seconds`).
- **`PrimaryAgent.converse()`** — streaming conversational reply path. Uses history, principal identity, and declared capabilities to answer directly; skips scout, decompose, validation, and evolution.
- **`agent.interaction_mode`** — `"interactive"` (default, chat/CLI/dev) or `"rpc"` (agent-as-callable, e.g. published MCP server). `rpc` forces every turn to the task path and disables interactive clarifications so automated callers never hang on prompts.
- **`CORTEX_INTERACTION_MODE` env var** — runtime override for `agent.interaction_mode` (values `interactive` | `rpc`). `cortex publish mcp` sets this to `rpc` automatically.

### Changed

- `PrimaryAgent.synthesise()` now chooses a conversational system prompt when called with zero envelopes instead of instructing the model to "use task summaries" that don't exist — fixes the "I need the task results summary" meta-reply observed in the published chat UI on simple greetings.
- `cortex publish mcp` auto-injects `CORTEX_INTERACTION_MODE=rpc` and echoes the mode change.

### Fixed

- Empty-greeting or non-actionable input to the published chat UI no longer produces the "I need the task results summary" meta-response. The Intent Gate routes these directly to `converse()`; if the gate is disabled, the hardened `synthesise()` fallback still responds directly.

---

## [1.1.0] - 2026-04-18

### Added

- **Ant Colony** — self-spawning specialist agent mesh. The orchestrator can now hatch independent Cortex agents as MCP servers at runtime to fill capability gaps identified by the Capability Scout.
  - `AntColony` module handles port allocation, per-ant `cortex.yaml` generation, subprocess spawning, health-polling, PID supervision with auto-restart, and `ants.yaml` state persistence across restarts.
  - `AntServer` — lightweight aiohttp MCP server each ant process runs, exposing `/health`, `/capabilities`, `/tools`, and `/tools/{name}/invoke`.
  - `ant_colony` config section in `cortex.yaml` (see Configuration Reference).
  - `trust_tier: ant` — ants are registered with write tools allowed and no output guard, treated as trusted internal agents.
  - `CortexFramework.hatch_ant()`, `stop_ant()`, `list_ants()` public API.
  - `auto_hatch_on_gap` flag on `CapabilityScout` — automatically hatch ants as a last resort when neither configured nor externally discovered servers can fill a capability gap.
  - Two new streaming event types: `ANT_HATCHED`, `ANT_STOPPED`.
  - `ToolServerRegistry.register_ant_server()` — dedicated registration path for ant-tier servers.
  - Learning Engine persists ant-originated `tool_servers` entries to `cortex.yaml` via `DeltaProposal.tool_servers_config`.
- **`cortex ants` CLI** — manage the colony from the terminal: `list`, `hatch`, `stop`, `stop-all`, `status`.
- **Setup Wizard** — new *Ant Colony* section for configuring the subsystem from the browser UI.
- **Replan scratchpad** — `PrimaryAgent` now maintains a session-scoped reasoning trace (`_scratchpad`) across replan calls. The LLM accumulates confirmed facts, open questions, and strategy adjustments (max 300 words) so each replan has full context from prior waves.
- **Clean-wave replan skip** — replanning is skipped when all tasks in a wave passed on the first attempt with no validation feedback, avoiding unnecessary LLM calls when the plan is still valid.

### Fixed

- `stale_task_names` was referenced before its initialisation in `framework.py`, causing a potential `NameError` on the first capability-scout pass.
- Resolved 72 ruff lint errors across the codebase (unused imports, unused local variables, ambiguous variable names, bare f-strings).

---

## [1.0.0] - 2026-04-16

Initial public release of the Cortex Agent Framework.

### Core

- Fan-Out / Fan-In orchestration with a typed task graph compiled from an LLM decomposition pass.
- `CortexFramework.run_session()` entrypoint driving the full lifecycle: sanitisation, session creation, capability discovery, decomposition, wave-based execution, synthesis, validation, and learning.
- Primary Agent with three modes: **decompose**, **replan** (mid-session DAG growth), and **synthesise**.
- Task Graph Compiler with cycle detection, dependency validation, and topological wave scheduling.
- Signal Registry coordinating async fan-in between parallel task workers.
- Generic MCP Agent executing every task through a uniform tool-use loop.

### Tooling & discovery

- Capability Scout with LLM-driven tool-server relevance filtering and graceful timeouts.
- Tool Server Registry managing stdio and SSE MCP server lifecycles.
- External MCP Registry for auto-discovered internet MCP servers, persisted to `cortex_auto_mcps.yaml`.

### Knowledge & memory

- Blueprint Store: persistent markdown templates per task type, auto-updated post-session (originally consent-gated; see 1.3.0 for autonomic replacement), with staleness checks that trigger re-discovery.
- History Store with encryption support and automatic retention cleanup.
- Result Envelope Store with in-process hot path and SQLite/Redis crash-resilience backing.
- Learning Engine with delta proposals gated by confidence levels (medium = 3, high = 5) (originally consent-gated at end of session; replaced by the autonomic gate in 1.3.0).

### Safety & isolation

- Input Sanitiser enforcing token limits, MIME checks, and path-traversal blocks at the boundary.
- Credential Scrubber applying configurable regex patterns to task outputs before persistence.
- Code Sandbox for running LLM-generated Python in an isolated subprocess with a configurable import blocklist.

### Validation

- Validation Agent scoring final responses on intent match, completeness, and coherence, with a configurable threshold (floor 0.60).
- Per-task wave-gate validation against declared `output_schema` / `validation_notes`, with up to three automatic retries carrying feedback.

### LLM & configuration

- Multi-provider LLM client supporting Anthropic, OpenAI, Bedrock, Azure, Mistral, Deepseek, Gemini, Grok, local runtimes, and custom providers.
- `LLMClient.verify_all()` startup credential check.
- YAML config loader with schema validation (`cortex.yaml`).

### Observability & streaming

- Observability Emitter with dual OpenTelemetry / stdout operational streams plus an append-only audit log and per-task rolling baselines.
- Typed streaming events (`StatusEvent`, `ResultEvent`, `ClarificationEvent`, `ClarificationRequestEvent`) delivered through a caller-provided `event_queue`.

### Storage

- Pluggable persistence with Memory, SQLite (WAL), and Redis backends behind a single interface.

### Packaging

- PyPI metadata, classifiers, and project URLs.
- GitHub Actions CI running pytest on Python 3.11 and 3.12 plus a ruff lint job.

[1.4.0]: https://github.com/kritird/Cortex-Agent-Framework/releases/tag/v1.4.0
[1.3.1]: https://github.com/kritird/Cortex-Agent-Framework/releases/tag/v1.3.1
[1.3.0]: https://github.com/kritird/Cortex-Agent-Framework/releases/tag/v1.3.0
[1.2.0]: https://github.com/kritird/Cortex-Agent-Framework/releases/tag/v1.2.0
[1.1.0]: https://github.com/kritird/Cortex-Agent-Framework/releases/tag/v1.1.0
[1.0.0]: https://github.com/kritird/Cortex-Agent-Framework/releases/tag/v1.0.0
