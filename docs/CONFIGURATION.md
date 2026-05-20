# Configuration Reference

[← Back to README](../README.md)

Every aspect of Cortex is driven by `cortex.yaml`. This page is the authoritative reference for every field.

## Top-level structure

```yaml
agent:           # Agent identity, concurrency, timeouts, intent gate, interaction mode
llm_access:      # LLM provider routing
task_types:      # Vocabulary of work the agent can do
tool_servers:    # MCP tool server connections
storage:         # Persistence configuration
sqlite:          # (optional) SQLite backend settings
redis:           # (optional) Redis backend settings
history:         # (optional) Session history settings
validation:      # (optional) Quality validation settings
learning:        # (optional) Delta learning settings
ant_colony:      # (optional) Self-spawning specialist agent mesh
tool_forge:      # (optional) Runtime MCP server generation from LLM-generated code
workspace_bash:  # (optional) Workspace-aware file/command execution with HITL
code_sandbox:    # (optional) Sandboxed Python code execution
ui:              # (optional) Built-in chat UI served by `cortex publish ui`
```

---

## `agent`

```yaml
agent:
  name: MyAgent                         # Required. Display name, locked after first run.
  description: A helpful AI assistant   # Required.
  system_prompt_extra: |                # Optional. Appended to system prompt.
    Always respond in British English.
  synthesis_guidance: |                 # Optional. Extra instruction injected into the
    Always cite sources with [n] markers.  # synthesis LLM call — useful for citation
                                           # style, tone, or output structure guidance.
  interaction_mode: interactive         # "interactive" | "rpc" — see below.
  execution_mode: planned               # "planned" | "static" — see below.
  inject_session_context: true          # Give sub-tasks the goal + scratchpad — see below.
  time:
    default_max_wait_seconds: 120       # Session-level timeout
    default_task_timeout_seconds: 40    # Per-task timeout
  concurrency:
    max_concurrent_sessions: 50         # Global session cap
    max_concurrent_sessions_per_user: 3 # Per-user session cap
    max_parallel_tasks: 5               # Tasks running simultaneously per session
    max_tasks_per_session: 20           # Total tasks allowed in a single session
    # max_parallel_llm_calls: <int>     # Optional. Omit to auto-derive from
                                        # provider+model (see "LLM concurrency
                                        # auto-tuning" below).
    # adaptive_llm_concurrency: true    # Default true. Set false to pin the
                                        # gate at max_parallel_llm_calls instead
                                        # of self-tuning it.
  intent_gate:                          # Pre-scout turn classifier (see below)
    enabled: true
    heuristic_confidence_threshold: 0.7
    llm_provider: default
    timeout_seconds: 5.0
  capability_scout:                     # Controls tool server discovery at session start
    timeout_seconds: 10
    external_discovery:
      search_timeout_s: 10
```

### `interaction_mode`

- `interactive` (default) — chat UIs, CLI, dev mode. The Intent Gate routes conversational turns (greetings, acknowledgements, "what can you do?") directly to a streaming reply via `PrimaryAgent.converse()`, skipping scout + decomposition. Task-shaped turns run the full pipeline. Interactive clarifications are allowed.
- `rpc` — agent is exposed as a callable (e.g. `cortex publish mcp`). Every turn is forced to the task path and no interactive clarifications are emitted, because an automated caller cannot answer them. If the decomposer returns no tasks for an rpc turn, the framework returns a structured empty response instead of hanging.

Override at runtime with the `CORTEX_INTERACTION_MODE` env var (`interactive` | `rpc`). `cortex publish mcp` sets this to `rpc` automatically.

### `execution_mode`

- `planned` (default) — the decomposition LLM generates the task graph at runtime from your `task_types`. Intent gate, capability scout, and decomposition all run.
- `static` — the `task_types` *are* the graph. They run as a fixed DAG in dependency order with **no decomposition, intent-gate, or capability-scout LLM calls**, and no mid-session replanning. The fan-out/fan-in waves, validation gate, retries, synthesis, and learning still run.

Static mode powers **code-node agents** — agents whose nodes are Python functions (`complexity: scripted` + a `handler`). It is set automatically when you build the agent with [`CortexBuilder`](GETTING_STARTED.md#code-first-agents--cortexbuilder) and register a code node via `.node()`. You can also hand-write a static DAG of capability-routed `task_types` in `cortex.yaml` by setting `execution_mode: static`.

### `inject_session_context`

When `true` (default), each LLM-synthesis sub-task receives two extra pieces of context in its system prompt:

- the **original user request**, so the worker knows the overall goal its task serves; and
- the planner's current **reasoning scratchpad** (confirmed facts, open questions, strategy), refreshed at every wave dispatch so a worker in a later wave sees what earlier waves established.

Without this, a sub-task only sees its own instruction and runs blind to the session. The worker is still told to produce output for *its task only* — the context is for consistency, not scope expansion.

It adds modest tokens per sub-task call (request truncated to ~800 chars, scratchpad to ~1500). Set to `false` on latency- or budget-sensitive deployments to send the leaner legacy prompt.

### LLM concurrency auto-tuning

`max_parallel_llm_calls` is the ceiling on concurrent in-flight LLM HTTP requests. The right value depends entirely on the backend — a single local Ollama serializes inference (1 is correct), Anthropic Haiku and GPT-4o-mini happily serve eight or more parallel calls, Opus and GPT-4 sit somewhere in between — so the framework picks it for you.

**Initial value — model-power registry.** When `max_parallel_llm_calls` is unset in `cortex.yaml`, the framework looks up the configured default `provider` + `model` against a small table in [`cortex/llm/model_power.py`](../cortex/llm/model_power.py). Representative picks:

| Provider:model pattern | Initial ceiling |
|---|---|
| `local:*` (Ollama, vLLM, llama.cpp) | 1 |
| `anthropic:*haiku*`, `openai:*mini*`, `gemini:*flash*` | 8 |
| `anthropic:*sonnet*`, `openai:*gpt-4o*`, `mistral:*` | 6 |
| `anthropic:*opus*`, `openai:*gpt-4*`, `grok:*` | 4 |
| Unknown / unmatched | 2 |

The pick is logged at startup so you can see what the framework chose (`max_parallel_llm_calls auto-derived: 6 (anthropic:claude-sonnet-4-7)`).

**Runtime adaptation — AdaptiveLLMGate.** With `adaptive_llm_concurrency: true` (the default), the gate self-tunes between `1` and the initial ceiling using AIMD: it halves multiplicatively on errors, empty responses, or sharp latency spikes vs the best observed baseline, and grows additively by 1 after a streak of clean calls under saturation. Backoff and probe-up steps are logged (`AdaptiveLLMGate: 6 -> 3 (backoff: latency spike ...)`).

**When to pin a value.** Set `max_parallel_llm_calls: <int>` explicitly only when you need determinism (benchmarking) or when an API enforces a hard rate limit you must not exceed. Pinning still lets the gate adapt downward — to disable self-tuning entirely and pin the gate exactly at your value, also set `adaptive_llm_concurrency: false`.

### `intent_gate`

Cheap pre-scout classifier that decides whether a turn needs the full task pipeline. Stage 1 is a pure heuristic (greeting lexicon, task verbs, known task-type names, file attachments) — most turns resolve here for zero LLM cost. Stage 2 is a small LLM call that only fires when the heuristic is under-confident.

| Key | Meaning |
|---|---|
| `enabled` | Master switch. `false` treats every turn as a task (legacy behaviour). |
| `heuristic_confidence_threshold` | Stage 1 confidence at/above which Stage 2 is skipped. Raise to force more LLM classifications; lower to trust heuristics more. |
| `llm_provider` | LLM provider key used for Stage 2. Default reuses the framework's `default` provider. Point this at a cheap/fast model to minimise per-turn latency. |
| `timeout_seconds` | Upper bound on Stage 2 latency before falling back to task routing. |

---

## `llm_access`

```yaml
llm_access:
  default:
    provider: anthropic                 # See providers table below
    model: claude-sonnet-4-5
    api_key_env_var: ANTHROPIC_API_KEY
    max_tokens: 4096
    temperature: 1.0
    thinking_budget_tokens: 0           # Extended thinking (Anthropic only, 0 = off)
    base_url: null                      # For proxies / gateways

  # Optional per-task overrides
  task_overrides:
    heavy_analysis:
      model: claude-opus-4-5
      max_tokens: 8192
      thinking_budget_tokens: 5000
```

### Supported providers

| Provider | Value | Default env var | Example models |
|---|---|---|---|
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | claude-sonnet-4-5, claude-opus-4-6, claude-haiku-4-5 |
| OpenAI | `openai` | `OPENAI_API_KEY` | gpt-4o, gpt-4o-mini, o3-mini |
| Google Gemini | `gemini` | `GEMINI_API_KEY` | gemini-2.5-pro, gemini-2.5-flash |
| xAI Grok | `grok` | `XAI_API_KEY` | grok-3, grok-3-mini |
| Mistral | `mistral` | `MISTRAL_API_KEY` | mistral-large-latest |
| DeepSeek | `deepseek` | `DEEPSEEK_API_KEY` | deepseek-chat, deepseek-reasoner |
| AWS Bedrock | `bedrock` | AWS credentials | anthropic.claude-sonnet-4-* |
| Azure AI | `azure_ai` | `AZURE_AI_API_KEY` | claude-sonnet-4 via Azure |
| Anthropic proxy | `anthropic_compatible` | `ANTHROPIC_API_KEY` | any — set `base_url` |
| Local runtime | `local` | `LOCAL_LLM_API_KEY` (optional) | Ollama / LM Studio / vLLM — e.g. `gemma4:e4b`. Default `base_url` is `http://localhost:11434/v1` |
| Custom | `custom` | — | Provide `function` dotted path |

---

## `task_types`

The vocabulary of work the agent can perform.

```yaml
task_types:
  - name: web_research                  # Unique ID used in depends_on
    description: Search the web for current info on a topic
    output_format: md                   # text | md | json | html | csv | code | file
    capability_hint: web_search         # See capability hints below
    tool_hint: brave_search             # Optional: prefer a specific tool server
    mandatory: false                    # If true, always included in every session
    max_tokens: 2048                    # Override max_tokens for this task
    timeout_seconds: 60                 # Override per-task timeout
    depends_on: []                      # Task names that must complete first

  - name: write_report
    description: Write a structured report from research findings
    output_format: md
    capability_hint: document_generation
    depends_on: [web_research]
```

### Execution modes (`complexity`)

| Value | Name | How it works | When to use |
|---|---|---|---|
| `adaptive` | **Adaptive** | LLM decomposes and executes freely each run. Soft hints accumulate in the blueprint's *Discovery Hints* section after each run to steer future ones. | Open-ended tasks where the approach may vary: research, writing, classification |
| `pinned` | **Pinned** | LLM still executes each sub-task, but the decomposition DAG is locked to the blueprint's *Topology* section (hard constraint). Reproducible workflow on every run. | Recurring workflows with a known fixed structure — e.g. SDLC: code → test → deploy |
| `scripted` | **Scripted** | Bypasses the LLM entirely. Your Python handler function runs directly and returns the output. Zero token cost, fully auditable. This is a **code node**. | DB lookups, API calls, validation, math — anything where the logic is fixed |

For `scripted` tasks, set `handler` to the dotted Python path of your function:

```yaml
task_types:
  - name: fetch_user
    description: Look up a user record from the database
    complexity: scripted
    handler: my_pkg.handlers.fetch_user
    output_format: json
```

The handler is `async def fn(ctx)` (sync also works) and receives a [`TaskContext`](GETTING_STARTED.md#the-taskcontext) — `ctx.request`, `ctx.deps` (upstream outputs), `await ctx.llm(...)`, `await ctx.call_tool(...)`. It returns a string, a `(string, format)` tuple, or a dict/list (JSON).

> **Code-first:** instead of a dotted path, define handlers inline with the [`CortexBuilder.node()`](GETTING_STARTED.md#code-nodes--agentnode) decorator — no importable module needed, and `execution_mode` flips to `static` automatically.

For `pinned` tasks, pair with a `blueprint` that has a `## Topology` section. After the first successful run the framework populates it automatically, or you can author it by hand:

```yaml
task_types:
  - name: sdlc
    description: End-to-end software development lifecycle
    complexity: pinned
    blueprint: sdlc.md    # must contain a ## Topology section
    output_format: md
```

---

### Capability hints

`capability_hint` is a **planning hint, not an execution router**. It is optional — it defaults to `auto`. For non-scripted tasks the [ReAct loop](#react-loop-react) chooses the actual action(s) at runtime regardless of what you set here; the hint instead helps the decomposer understand each task type and guides which MCP servers the Capability Scout probes before decomposition. Setting it explicitly is most useful on **scripted** tasks, where a non-`auto` hint lets the framework skip MCP probing for that handler.

| Hint | Meaning |
|---|---|
| `auto` *(default)* | No hint — the planner and ReAct loop decide |
| `llm_synthesis` | No external tools — pure LLM reasoning, writing, summarisation |
| `web_search` | Search the web for live/current information. Tries configured tool servers first; falls back to built-in DuckDuckGo (no API key needed) |
| `workspace_bash` | Read, write, or execute files in the user's workspace directory (requires HITL approval for mutating ops) |
| `bash` | Run shell commands in a sandboxed environment |
| `code_exec` | Generate and run Python code in a sandbox |
| `document_generation` | Create structured documents (PDF, DOCX, reports) |
| `image_generation` | Generate or manipulate images |
| `forge_mcp` | Generate a new MCP server from code and register it with Ant Colony at the wave boundary (requires `tool_forge.enabled`, `code_sandbox.enabled`, and `ant_colony.enabled`) |

---

### ReAct loop (`react`)

Every non-scripted task runs through a **ReAct (reason → act → observe) loop**: the sub-agent's LLM picks one action, observes its result, and repeats until it decides the task is done. The loop is always on — there is no enable/disable flag — but three per-task-type knobs bound its cost:

```yaml
task_types:
  - name: web_research
    description: Search the web for current info on a topic
    capability_hint: web_search
    react:
      max_iterations: 10            # safety cap on reason→act→observe cycles
      observation_max_tokens: 600   # each tool observation is truncated to ~this
      context_char_budget: 24000    # older steps are summarised past this size
```

| Field | Default | Purpose |
|---|---|---|
| `max_iterations` | `10` | Hard safety cap. On reaching it the loop stops calling actions and forces a best-effort final answer. Normal tasks finish well before this. |
| `observation_max_tokens` | `600` | Each action's observation is truncated to roughly this many tokens before being fed back, so the running context can't explode. |
| `context_char_budget` | `24000` | Once the running conversation exceeds this many characters, the oldest reason/act/observe steps are digested into a compact summary. |

Scripted tasks (`complexity: scripted`) skip the loop entirely — their handler runs directly — so `react` has no effect on them. See [Task execution: the ReAct loop](ARCHITECTURE.md#task-execution-the-react-loop) for the full mechanics.

---

## `tool_servers`

MCP tool server connections. Three transports supported.

```yaml
tool_servers:
  # SSE transport — connects to a running HTTP server
  brave_search:
    transport: sse
    url: http://localhost:8051/sse
    headers:
      Authorization: "Bearer ${BRAVE_API_KEY}"
    capabilities:
      - web_search

  # stdio transport — spawns a subprocess; tools discovered via JSON-RPC tools/list
  brave_search:
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-brave-search"]
    startup_timeout_seconds: 100
    connection:
      timeout_seconds: 100
      read_timeout_seconds: 600
    env:
      BRAVE_API_KEY: ${BRAVE_API_KEY}   # env vars merged with system env at spawn time

  filesystem:
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp/workspace"]
    capabilities:
      - file_read
      - file_write

  # streamable_http transport — MCP 1.x HTTP streaming
  custom_api:
    transport: streamable_http
    url: http://localhost:9000/mcp
    headers:
      Authorization: "Bearer ${MY_API_TOKEN}"
    capabilities:
      - custom_action
```

Environment variable substitution with `${VAR}` works in any string value.

---

## `storage`

```yaml
storage:
  base_path: ./cortex_storage           # Root directory for persistent data
  result_ttl_seconds: 3600              # How long task results are kept in memory
```

### SQLite backend (single-host)

```yaml
sqlite:
  enabled: true
  path: ./cortex_storage/cortex.db
  wal_mode: true                        # Recommended for concurrent reads
```

### Redis backend (distributed)

```yaml
redis:
  enabled: true
  url: redis://localhost:6379/0
  key_prefix: "cortex:myagent:"         # Isolate agents sharing one Redis
```

> **Never share a SQLite file across running agents.** Use Redis for multi-process deployments.

---

## `history`

```yaml
history:
  enabled: true
  max_records_per_user: 1000
  retention_days: 90
```

When enabled, every completed session is stored and queryable via `cortex replay SESSION_ID`.

---

## `validation`

```yaml
validation:
  enabled: true
  threshold: 0.75                       # Min composite score (hard floor: 0.60)
  critical_threshold: 0.40              # Below this, the response is not delivered
  model: null                           # Override model for validation (null = default)
  max_remediation_attempts: 2           # Iterative remediation passes (1 = single-shot)
```

Every response is scored on intent match, completeness, and coherence. Responses below `threshold` are flagged on `SessionResult.validation_report`. Set `enabled: false` to skip the post-synthesis Validation Agent entirely (the per-task wave gate still runs for tasks that declare an `output_schema` or `validation_notes`).

When a response scores between `critical_threshold` and `threshold`, the framework remediates it. `max_remediation_attempts` controls how many corrective passes run: each pass sees the prior attempt's response and the findings it still failed on, so it corrects without repeating mistakes. If no pass clears `threshold`, the best-scoring candidate across the original and all attempts is delivered. Set to `1` for the legacy single-shot behaviour.

---

## `learning`

Autonomic learning — signal-gated, no consent prompt.

```yaml
learning:
  enabled: true                         # Master switch
  validation_threshold: 0.75            # Min composite validation score to learn
  complexity_threshold: 0.6             # Min TaskComplexityScorer score to stage ad-hoc task
  require_user_identity: true           # In rpc mode, skip learning when no principal attached
  auto_apply_delta: true                # Auto-promote to cortex.yaml once confidence met
  auto_apply_min_confidence: medium     # low | medium | high
  auto_apply_min_confirmations: 3       # Distinct principals required before auto-apply
  notify_on_apply: true                 # Emit a LearningEvent when a delta is applied
  max_lesson_chars: 500                 # Per-entry cap when writing into a blueprint
```

At end of session the framework runs a two-stage gate:

1. **Skip guards** — chat turns, RPC calls without an attached principal, and sessions with `learning.enabled: false` exit immediately with a `LearningEvent(action=…skipped)`.
2. **Scoring gates** — if the composite validation score clears `validation_threshold` *and* the [TaskComplexityScorer](../cortex/modules/task_complexity_scorer.py) score clears `complexity_threshold`, the session is eligible. Ad-hoc tasks are staged into `cortex_delta/pending.yaml` with a seeded draft blueprint; known tasks have their blueprints refined via auto-update.

Staged ad-hoc proposals still need **distinct-principal confirmations** (default 3) before they are promoted into `cortex.yaml`. When `auto_apply_delta: true` (the default) that promotion happens automatically as soon as the threshold is met; otherwise run `cortex delta review` / `cortex delta apply` manually.

---

## `ant_colony`

Enables the self-spawning specialist agent mesh. When active, the Capability Scout can automatically hatch independent Cortex agents as MCP servers to fill capability gaps at runtime.

```yaml
ant_colony:
  enabled: false                        # Set true to activate the colony
  base_port: 8100                       # First port tried when allocating a new ant
  max_ants: 20                          # Maximum simultaneously running ants
  auto_restart: true                    # Supervisor restarts crashed ants automatically
  auto_hatch_on_gap: false              # Hatch ants automatically when CapabilityScout
                                        # finds a gap no configured server can fill
  llm_provider: default                 # Provider alias ants use (must match llm_access key)
  llm_model: claude-haiku-4-5-20251001  # Model for ant agents (Haiku recommended)
  api_key_env_var: ANTHROPIC_API_KEY    # Env var holding the API key for ant agents
```

### How it works

1. A capability gap is detected by the Capability Scout (or you call `cortex ants hatch`).
2. The colony allocates a port starting from `base_port`, writes a `cortex.yaml` for the ant, spawns a subprocess running `AntServer`, and polls `/health` until ready (30 s timeout).
3. The ant is registered in the Tool Server Registry with `trust_tier: ant` — write tools allowed, no output guard.
4. The supervisor monitors PIDs and restarts crashed ants when `auto_restart: true`.
5. On framework shutdown, all ant subprocesses are terminated.

Ant state (name, capability, port, PID, restart count) is persisted to `ants.yaml` in `storage.base_path` and reloaded on the next startup.

### Managing ants via CLI

```bash
cortex ants list                                  # Show all ants and status
cortex ants hatch my-ant --capability web_search  # Manually spawn a specialist ant
cortex ants stop my-ant                           # Stop a specific ant
cortex ants stop-all                              # Stop all running ants
cortex ants status my-ant                         # Detailed status for one ant
```

---

## `tool_forge`

Enables runtime MCP server generation. When active and both `code_sandbox` and `ant_colony` are enabled, the decomposer gains access to the `forge_mcp` capability — it can assign tasks that generate FastMCP server scripts, write them to disk, and register them with Ant Colony at wave boundaries. Dependent tasks in the same session can use the new server immediately.

```yaml
tool_forge:
  enabled: false                        # Master switch. Requires code_sandbox.enabled
                                        # AND ant_colony.enabled to be effective.
  persist_by_default: false             # When true, forged servers survive framework
                                        # restart (auto_restart=true in ants.yaml).
                                        # When false, the entry is written but not
                                        # re-hatched on next startup (session-scoped).
  spawn_timeout_seconds: 30             # Seconds to wait for the generated server
                                        # subprocess to pass /health check.
  codegen_llm_provider: default         # Provider alias for MCP server code generation.
                                        # May warrant a stronger model than the default.
```

### How ToolForge works

1. A `forge_mcp` task is decomposed by the Primary Agent and dispatched to Generic MCP Agent.
2. The agent sends a FastMCP-specific code generation prompt to the configured LLM, then validates and executes the output in the code sandbox.
3. The generated script is written to `{storage_base}/ants/{task_name}/server.py`.
4. At the **wave boundary** (after all tasks in the wave complete), the framework calls `AntColony.hatch_from_script()` with the script path.
5. The new server is spawned, health-checked (HTTP 200 on `/health`), and registered in the Tool Server Registry.
6. Tasks in subsequent waves can use the new capability like any other tool server.

Forged servers are tracked in `ants.yaml` with `source: forged`. They are supervised and auto-restarted by Ant Colony like any hand-hatched ant.

### Guards

All three of the following must be true for `forge_mcp` to appear in the decomposition prompt:

- `tool_forge.enabled: true`
- `code_sandbox.enabled: true`
- `ant_colony.enabled: true`

If only `tool_forge` is enabled but the other two are not, the framework logs a warning and the capability is not registered.

---

## `adaptive_model_routing`

**Adaptive Model Routing (AMR)** — decomposer-driven per-task LLM selection. When enabled, the decomposition LLM emits a `<model_tier>` tag (low / medium / high) for each task it creates. AMR maps that tier to the named provider configured in `tiers`. Explicit `llm_provider` on a `task_type` entry always wins over AMR.

```yaml
adaptive_model_routing:
  enabled: true

  tiers:
    low: fast        # simple retrieval, formatting, short text generation
    medium: default  # multi-step reasoning, moderate code, single-doc analysis
    high: powerful   # complex architecture, deep synthesis, multi-file codegen

  validation_provider: ""  # "" = auto-select first non-default provider
```

| Key | Default | Description |
|---|---|---|
| `enabled` | `false` | Master switch for AMR |
| `tiers.low` | `"default"` | Provider key for low-complexity tasks |
| `tiers.medium` | `"default"` | Provider key for medium-complexity tasks |
| `tiers.high` | `"default"` | Provider key for high-complexity tasks |
| `validation_provider` | `""` | Provider for wave-level task validation. Empty string → auto-select first non-default provider from `llm_access.providers`; falls back to `"default"` when none are configured |

**Complexity criteria emitted by the decomposition LLM:**
- `low` — direct retrieval, format conversion, short text generation, single-fact lookup, simple translation
- `medium` — multi-step reasoning, moderate code generation (< ~100 lines), single-document analysis, structured writing
- `high` — complex architecture design, multi-file code generation, deep research synthesis, long-form content, advanced algorithms

The assessment is objective — the LLM grades based solely on task characteristics. The tier→provider mapping lives entirely in your config; no training-time bias can influence routing.

**Precedence:**
1. `task_types[n].llm_provider` (explicit in cortex.yaml) — always wins
2. AMR tier-resolved provider — applies when the task's static config uses `"default"`
3. `"default"` — fallback when AMR is disabled or the tier is unrecognised

**Ant Colony interaction:**
Ant agents themselves always decompose using their configured `llm_provider` (default). Sub-tasks spawned inside an ant's decomposition inherit the parent's full AMR config and provider pool, so they are also adaptively routed.

---

## `workspace_bash`

Workspace-scoped file and command execution with mandatory Human-in-the-Loop (HITL) gating. When enabled, the Generic MCP Agent gains `read_file`, `list_dir`, `write_file`, and `execute` capabilities scoped to a workspace directory extracted from the task instruction.

```yaml
workspace_bash:
  enabled: true          # Master switch (default: true)
  hitl_enabled: true     # Enforced true at runtime — cannot be disabled
```

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Activates workspace-aware file/command tools in the Generic MCP Agent |
| `hitl_enabled` | `true` | Hardcoded guard — the framework logs a warning and overrides this to `true` even if set to `false` in config |

**HITL behaviour:**
- `read_file` and `list_dir` never prompt — they are read-only.
- `write_file` fires a `ClarificationRequestEvent` before writing; if the file exists, a unified diff is shown.
- `execute` fires a `ClarificationRequestEvent` before running; obviously dangerous patterns (`rm -rf /`, `sudo`, etc.) are blocked before the prompt fires.
- If the HITL prompt times out or the user denies it, a `CortexHITLDeniedError` is raised and the task fails cleanly.

All paths are resolved relative to the workspace root and checked for traversal — any `rel_path` that resolves outside the workspace raises `CortexSecurityError`.

---

## `app_control`

Launch and drive native desktop applications. Primary path discovers each app's scripting interface (macOS sdef, Windows UI Automation / COM, Linux AT-SPI / xdotool) and injects it into the LLM prompt so the agent generates precise actions. Fallback is a screenshot → vision-LLM → action loop. Once enabled, `app_control` is available to the ReAct loop as an action on any non-scripted task — no `capability_hint` wiring needed.

```yaml
app_control:
  enabled: false           # Master switch
  hitl_enabled: true       # Prompt before each mutating action (launch / script / screenshot)
  timeout_seconds: 30      # Per-action subprocess timeout
  sdef_max_chars: 8000     # Trim scripting-dict summary before injecting into LLM context
  max_vision_steps: 10     # Cap on screenshot → action loop iterations
  vision_provider: default # LLM provider for vision steps ("default" = primary)
```

| Key | Default | Description |
|---|---|---|
| `enabled` | `false` | Activates the App Control capability |
| `hitl_enabled` | `true` | Require user approval per action. Vision loops ask once up-front for batch approval covering the whole task. |
| `timeout_seconds` | `30` | Per-action timeout for osascript / PowerShell / shell subprocesses |
| `sdef_max_chars` | `8000` | Max chars of scripting-dictionary summary; longer summaries are truncated before injection |
| `max_vision_steps` | `10` | When no scripting dictionary exists for an app, this caps how many screenshot → action iterations the vision loop runs |
| `vision_provider` | `default` | LLM provider used for vision steps. `default` inherits the primary provider |

**Action types** (emitted by the LLM as `ACTION: <name>` blocks): `launch_app`, `run_applescript`, `run_powershell`, `run_shell_command`, `screenshot`, `get_running_apps`, `get_window_text`, `copy_to_clipboard`, `paste_from_clipboard`. Multiple blocks can be chained with `---`.

**Platform support:** AppleScript and `sdef` discovery are macOS-only. PowerShell + UIA discovery work on Windows. Linux uses AT-SPI / xdotool plus `run_shell_command`.

**Accessibility (macOS):** Before any AppleScript that uses keystrokes, the framework probes whether the host process has Accessibility permission. If denied, a clear instruction message is surfaced (instead of a cryptic `-1743` error). The result is cached per-session.

---

## `playwright_mcp`

Built-in Playwright MCP server — browser automation as a first-class capability. The framework starts `@playwright/mcp` internally as a stdio MCP server at boot. It is NOT exposed in `tool_servers`; users get a `browser` capability automatically.

```yaml
playwright_mcp:
  enabled: false
  browser: chromium                  # chromium | firefox | webkit
  headless: false                    # false = visible browser window
  startup_timeout_seconds: 60
  # Leave both null to auto-default storage_state_path to
  # {storage.local_path}/playwright_session.json (cookies + localStorage)
  storage_state_path: null
  user_data_dir: null
  viewport_width: 1280
  viewport_height: 720
```

| Key | Default | Description |
|---|---|---|
| `enabled` | `false` | Master switch — when on, the Playwright MCP server starts at framework boot |
| `browser` | `chromium` | Browser engine to drive. One of `chromium`, `firefox`, `webkit` |
| `headless` | `false` | `true` hides the browser window (CI / server mode) |
| `startup_timeout_seconds` | `60` | How long to wait for the Playwright MCP server to come up |
| `storage_state_path` | (auto) | JSON file that persists cookies + localStorage so logins survive across runs. When left `null`, defaults to `{storage.local_path}/playwright_session.json` |
| `user_data_dir` | `null` | Full persistent browser profile dir (extensions, IndexedDB, service workers). Takes precedence over `storage_state_path` when set |
| `viewport_width` | `1280` | Browser viewport width in pixels |
| `viewport_height` | `720` | Browser viewport height |

**Prerequisites:** Node.js + `npx` must be on PATH. The first invocation downloads the Playwright MCP package via `npx -y @playwright/mcp@latest`.

**Capability surface:** The agent receives a `browser` capability that the ReAct loop can use as an action on any non-scripted task. All Playwright MCP tools (navigate, click, type, screenshot, evaluate, fill, upload, etc.) are surfaced through the standard MCP tool-discovery flow.

---

## `ui`

Configures the built-in chat UI that `cortex publish ui` serves. Enable via the wizard's *Chat UI* step or by hand.

```yaml
ui:
  enabled: true                  # Master switch
  host: "0.0.0.0"                # Bind address
  port: 8090                     # HTTP port
  title: "Cortex Agent"          # Title shown in the UI header
  auth:
    mode: none                   # none | token | basic
    # token: "s3cret"            # required when mode: token
    # username: admin            # required when mode: basic
    # password: changeme         # required when mode: basic
```

| Auth mode | What it does |
|---|---|
| `none` | Anonymous cookie identifies each browser session |
| `token` | Client must send `Authorization: Bearer <token>` |
| `basic` | Standard HTTP Basic auth |

The UI streams `StatusEvent` / `ResultEvent` / `ClarificationEvent` over SSE and persists chats through the existing History Store (enable `history.enabled: true` to survive restarts).

---

## Environment variable substitution

Any string field in `cortex.yaml` can use `${VAR}` syntax:

```yaml
tool_servers:
  github:
    transport: sse
    url: ${GITHUB_MCP_URL}
    headers:
      Authorization: "Bearer ${GITHUB_TOKEN}"
```

Substitution happens at load time. Missing variables produce a clear error.

---

## Environment variables Cortex reads directly

| Variable | Description |
|---|---|
| `CORTEX_CONFIG` | Override default config path (defaults to `./cortex.yaml`) |
| `CORTEX_LOG_LEVEL` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |
| `CORTEX_INTERACTION_MODE` | Runtime override for `agent.interaction_mode` — `interactive` \| `rpc`. `cortex publish mcp` sets this to `rpc` automatically. |
| `CORTEX_HITL_URL` | Base URL of the HITL relay server (e.g. `http://127.0.0.1:PORT`). Set automatically on ant subprocess environments so WorkspaceBash HITL prompts are relayed to the parent framework session instead of failing silently. Not set manually in normal use. |
| `ANTHROPIC_API_KEY` | Default Anthropic provider key |
| `OPENAI_API_KEY` | Default OpenAI provider key |
| `GEMINI_API_KEY` | Default Gemini provider key |
| `XAI_API_KEY` | Default Grok provider key |
| `MISTRAL_API_KEY` | Default Mistral provider key |
| `DEEPSEEK_API_KEY` | Default DeepSeek provider key |
| `AWS_DEFAULT_REGION` | Bedrock region |
| `AZURE_AI_API_KEY` | Azure AI provider key |
| `LOCAL_LLM_API_KEY` | Optional auth for the local provider (Ollama / LM Studio / vLLM) |

---

## Minimal working example

```yaml
agent:
  name: HelloAgent
  description: A minimal Cortex agent

llm_access:
  default:
    provider: anthropic
    model: claude-sonnet-4-5
    api_key_env_var: ANTHROPIC_API_KEY
    max_tokens: 2048

task_types:
  - name: answer
    description: Answer a user question directly
    output_format: md
    capability_hint: llm_synthesis

storage:
  base_path: ./cortex_storage
```

That's the entire file. No tool servers, no MCP setup — just an LLM-driven Q&A agent.

---

## Validating your config

```bash
cortex dry-run "test request"
```

Loads the config, compiles the task graph, and reports any errors **without making any LLM calls**. Use this in CI to gate config changes.
