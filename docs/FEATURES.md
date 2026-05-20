# Features

[← Back to README](../README.md)

A complete feature matrix of everything Cortex ships with.

## Core orchestration

| Feature | Description |
|---|---|
| **Fan-out / fan-in execution** | Primary agent decomposes requests into a dependency DAG; independent tasks run in parallel |
| **Three task execution modes** | `adaptive` (LLM free-form), `pinned` (LLM executes, but DAG locked to blueprint topology), `scripted` (Python code node, no LLM) |
| **Planned vs. static graphs** | `agent.execution_mode`: `planned` (LLM generates the DAG per request) or `static` (the configured `task_types` *are* the graph — no decomposition / intent-gate / scout LLM calls, no replan) |
| **Typed task graph** | Every task has a declared type, output format, dependencies, capability hint, and execution mode |
| **Cycle detection** | Task graph compiler rejects cyclic graphs before execution starts |
| **Topological execution** | Tasks run as soon as their dependencies complete — no fixed pipeline stages |
| **ReAct sub-agent execution** | Every non-scripted task runs a reason → act → observe loop — the sub-agent's LLM picks one action, observes its result, and repeats until it emits `finish`. Replaces single-pass task dispatch; a failed action becomes an observation the loop adapts to rather than aborting the task |
| **ReAct loop tunables** | Per-task-type `react` block bounds loop cost — `max_iterations` (safety cap, default 10), `observation_max_tokens` (default 600), `context_char_budget` (default 24000, past which the oldest steps are digested into a summary). The loop is always on for LLM-driven tasks; scripted code nodes skip it |
| **Capability-aware decomposition** | Decomposer sees currently-available MCP tools and plans around them |
| **Intent Gate** | Pre-scout classifier (heuristic → LLM cascade) routes chat-shaped turns directly to a streaming reply; only task-shaped turns run the full decompose pipeline. Emits `IntentClassifiedEvent` before decomposition. |
| **`interaction_mode`** | `interactive` (chat/CLI/dev) or `rpc` (MCP/automation) — `rpc` forces every turn to the task path and suppresses interactive clarifications |
| **Replan with scratchpad** | Mid-session re-entry into the Primary Agent grows the DAG; a session-scoped reasoning trace (confirmed facts, open questions, strategy) is carried forward across replans and into synthesis |
| **Context-rich replan prompt** | Replan receives a trigger-reason label (`mandatory_failure`, `stale_blueprint`, `adaptive_completed`), the full instruction + `depends_on` of each pending task, and head-and-tail-truncated completed summaries — so `modify`/`remove` ops act on task bodies the LLM has actually seen |
| **Clean-wave replan skip** | Replanning is skipped when every task in a wave passed first attempt with no validator feedback — avoids unnecessary LLM calls |
| **Conversational retry-with-feedback** | When a task fails the validation gate, the retry threads the prior attempt's output and the judge's feedback as real conversation turns; attempts accumulate so attempt 3 sees attempts 1 and 2, letting the model fix only what was flagged |
| **Session context for sub-tasks** | With `agent.inject_session_context` (default on), each sub-task LLM call sees the original user request and the live planner scratchpad — workers reason about why their task exists instead of running blind |
| **Synthesis step** | Primary agent stitches task outputs into a coherent final response |
| **Smart synthesis excerpts (Tier 1)** | File-output tasks contribute a keyword-grep excerpt (up to 8 KB) instead of a blind 2 KB head truncation |
| **Iterative file summarisation (Tier 2)** | Up to 3 concurrent LLM summaries of file outputs run before final synthesis — richer context with no developer configuration |
| **File output on large results** | When tasks produce file outputs, synthesis is written to `synthesis_{session_id}.md` and streamed as a `ResultEvent` with `metadata.output_type="file"` |
| **Clarification support** | Agent can pause mid-session and ask follow-up questions via `ClarificationEvent` |

## Code-first agents

| Feature | Description |
|---|---|
| **`CortexBuilder`** | Fluent Python API that assembles a `CortexConfig` — `.llm()`, `.provider()`, `.tool_server()`, `.task()`, `.node()`, `.storage()`, `.validation()`, `.configure()`, `.build()`. No `cortex.yaml` required |
| **`CortexFramework(config=...)`** | The framework constructor accepts a pre-built `CortexConfig` object, not just a YAML path |
| **`@node` code nodes** | The `.node()` decorator registers a plain Python function as a graph node — LangGraph-style. Works bare or parameterised (`@agent.node(depends_on=[...])`); sync or async |
| **Static-DAG execution** | Registering a code node flips the agent to `execution_mode="static"` — the declared graph runs verbatim, skipping the planner. The wave engine, validation gate, retries, streaming, and persistence still apply |
| **`TaskContext` runtime wiring** | Each node receives `ctx.request`, `ctx.deps` (upstream node outputs), `await ctx.llm(prompt)`, and `await ctx.call_tool(server, tool, ...)` — the same providers and MCP servers the rest of the agent uses |
| **Flexible node returns** | A node may return a `str`, a `(str, format)` tuple, a `dict`/`list` (auto-JSON), or `None` |
| **`SessionResult.node_outputs`** | Per-node raw outputs keyed by node name — read individual results without parsing the synthesised response |
| **Optional `event_queue`** | `run_session()` no longer requires an `event_queue` — omit it when you only want the returned `SessionResult` |
| **Handler registry** | In-process registry (`cortex:node:<id>` scheme) backs code-node handlers; dotted-path handlers (`"module.function"`) in `cortex.yaml` continue to work |

## LLM providers (8 built-in)

| Provider | Config value | Default env var | Notes |
|---|---|---|---|
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | Native SDK, extended thinking supported |
| OpenAI | `openai` | `OPENAI_API_KEY` | GPT-4o, o-series, etc. |
| Google Gemini | `gemini` | `GEMINI_API_KEY` | Gemini 2.5 / 2.0 / 1.5 |
| xAI Grok | `grok` | `XAI_API_KEY` | Grok-3, Grok-2 |
| Mistral AI | `mistral` | `MISTRAL_API_KEY` | Mistral Large, Medium |
| DeepSeek | `deepseek` | `DEEPSEEK_API_KEY` | DeepSeek V3, R1 |
| AWS Bedrock | `bedrock` | AWS credentials | Claude via Bedrock |
| Azure AI | `azure_ai` | `AZURE_AI_API_KEY` | Claude via Azure |
| Anthropic-compatible proxy | `anthropic_compatible` | `ANTHROPIC_API_KEY` | Set `base_url` for gateways |
| Local runtime | `local` | `LOCAL_LLM_API_KEY` (optional) | Ollama / LM Studio / vLLM; defaults `base_url` to `http://localhost:11434/v1`. Gemma 4 quickstart in the wizard |
| Custom | `custom` | — | Provide a Python dotted path |

**Per-task model routing**: override the default model for specific task types via `task_types[n].llm_provider`, or enable **Adaptive Model Routing (AMR)** to let the decomposer select the LLM automatically based on task complexity.

**Adaptive Model Routing (AMR)**: when `adaptive_model_routing.enabled: true`, the decomposition LLM grades each task as `low`, `medium`, or `high` complexity. AMR maps those tiers to named providers in `llm_access.providers`. Assessment is objective — the grading criteria are purely task-structural; no provider preference is baked in. Explicit `llm_provider` on a task type always overrides AMR. Ant sub-tasks inherit the parent's AMR config. The validation provider auto-selects the first non-default named provider when left blank.

**Auto-tuned LLM concurrency**: the framework picks an initial `max_parallel_llm_calls` ceiling from your configured provider+model via a small lookup table — `1` for local Ollama, `8` for Anthropic Haiku / GPT-4o-mini, `4` for Opus / GPT-4, etc. — then `AdaptiveLLMGate` self-tunes it at runtime using AIMD (halve on errors or latency spikes, grow additively under sustained saturation). No wizard knob to tune. Pin an explicit value in `cortex.yaml` only for benchmarking or hard-rate-limited APIs. See [LLM concurrency auto-tuning](CONFIGURATION.md#llm-concurrency-auto-tuning).

## Model Context Protocol (MCP)

| Feature | Description |
|---|---|
| **SSE transport** | Connect to remote MCP servers over Server-Sent Events |
| **stdio transport** | Spawn MCP servers as subprocesses; full JSON-RPC tool discovery (`tools/list`) and invocation (`tools/call`) at runtime |
| **streamable-HTTP transport** | Full MCP 1.x streamable HTTP support |
| **Capability discovery** | Dynamic tool discovery at session start; stdio servers auto-map instruction → MCP argument schema |
| **Header injection** | Per-server HTTP headers (auth tokens, API keys) |
| **Lifecycle management** | Auto-start, auto-restart, graceful shutdown of tool servers |
| **Publish as MCP server** | Export your Cortex agent *as* a live MCP server (`/mcp` endpoint + `/run` REST alias) for other agents to call |

## Multi-agent composition

| Feature | Description |
|---|---|
| **Agent-as-MCP-tool** | Any Cortex agent can be published as a live MCP/HTTP server |
| **Orchestrator pattern** | Parent agents list sub-agents in `tool_servers` and decompose across them |
| **Independent lifecycles** | Each agent has its own config, storage, concurrency, LLM routing |
| **Port conventions** | Standard port allocation (wizard `7799+N`, MCP `8080+N`) for multi-agent hosts |
| **No custom protocol** | Uses MCP end-to-end — no bespoke inter-agent RPC |
| **Ant Colony** | Orchestrator self-spawns specialist Cortex agents as MCP servers at runtime; supervised, health-checked, auto-restarted |
| **ToolForge** | Decomposer assigns `forge_mcp` tasks that generate FastMCP server code, write it to disk, and register it with Ant Colony at wave boundaries — dependent tasks in the same session see the new capability immediately |

## Streaming

| Event class | Fields | Use |
|---|---|---|
| `StatusEvent` | `message`, `session_id`, `event_type`, `metadata` | Progress updates for the UI |
| `ResultEvent` | `content`, `partial`, `validation_score`, `metadata` | Final or streaming response content — `metadata.output_type="file"` when synthesis is written to disk |
| `ClarificationEvent` | `question`, `options`, `clarification_id` | Agent is asking a follow-up question |
| `IntentClassifiedEvent` | `intent_mode`, `confidence`, `reasoning` | Intent Gate result (chat / task / hybrid) emitted before decomposition |
| `TaskBlueprintEvent` | `tasks`, `waves` | Full DAG emitted after decomposition — UI can render the plan before execution starts |
| `TaskToolCallEvent` | `task_id`, `task_name`, `tool_name`, `tool_input` | Emitted when a sub-agent invokes an MCP or built-in tool |
| `WorkspaceEvent` | `action`, `path`, `is_dir` | File-system change in workspace (read / modified / executed / listed) |
| `FileOutputEvent` | `filename`, `mime_type`, `size_bytes` | Agent produced a downloadable output file |
| `SessionTokenUsageEvent` | `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` | Cumulative token counters emitted at session end |
| `SynthesisTierEvent` | `tier`, `reason` | Which excerpt tier (short / medium / full / structured) was selected for synthesis |
| `LearningEvent` | `action`, `complexity_score`, `validation_score` | Gate decision and staged/applied task lists |
| `UserInterruptEvent` | `message`, `action` | User injected a mid-run message; `action` is `queued` → `replan` or `terminate` |

Event types: `SESSION_START`, `TASK_START`, `TASK_COMPLETE`, `STATUS`, `RESULT`, `ERROR`, `SESSION_END`, `CLARIFICATION`, `ANT_HATCHED`, `ANT_STOPPED`, `LEARNING`, `INTENT_CLASSIFIED`, `TASK_BLUEPRINT`, `TASK_TOOL_CALL`, `WORKSPACE_EVENT`, `FILE_OUTPUT`, `SESSION_TOKEN_USAGE`, `SYNTHESIS_TIER`, `USER_INTERRUPT`.

Wires into FastAPI SSE, WebSockets, or any async consumer pattern.

## Quality & validation

| Feature | Description |
|---|---|
| **Composite scoring** | Every response scored on intent match, completeness, coherence |
| **Configurable threshold** | Set a minimum acceptable score (hard floor: 0.60) |
| **Per-session validation report** | Returned on `SessionResult.validation_report` |
| **Model override** | Run validation with a different model than task execution |
| **Iterative remediation** | A sub-threshold response is corrected over up to `validation.max_remediation_attempts` passes; each pass sees the prior attempt and the findings it still failed, so it doesn't repeat mistakes. Best-scoring candidate is delivered if none clears the threshold. |

## Autonomic learning

| Feature | Description |
|---|---|
| **Signal-driven gate** | At end-of-session, learning fires automatically when the `TaskComplexityScorer` and validation score both clear their thresholds. No consent prompt is issued — the gate is deterministic and auditable. |
| **Chat-turn skip** | Sessions classified as chat by the Intent Gate never trigger learning. |
| **RPC identity check** | Sessions running in `interaction_mode: rpc` without an attached principal are skipped (configurable via `learning.require_user_identity`). |
| **TaskComplexityScorer** | Pure weighted sum of code synthesis, tool-trace length, fan-out, dependencies, tokens, duration → 0.0–1.0 score. Fixed weights = reproducible learning decisions across deployments. |
| **Draft blueprints** | On first stage, a draft blueprint is seeded under `drafts/{task_name}__{hash}` so guidance accumulates before a task is promoted into `cortex.yaml`. |
| **Distinct-principal accumulation** | Promotion still requires 3 distinct principals (configurable). One user can never promote a delta alone. |
| **Auto-apply mode** | Default on — deltas promote themselves once confidence accumulates. Flip `auto_apply_delta: false` to keep `pending.yaml` as a manual review queue. |
| **Human-in-the-loop review** | `cortex delta review` shows staged proposals; `cortex delta apply` writes to `cortex.yaml`; `cortex delta rollback` restores the prior version. |
| **LearningEvent telemetry** | Every session emits a single `LearningEvent` with the gate decision, complexity score, validation score, and staged/applied task lists. |

## Session management

| Feature | Description |
|---|---|
| **Concurrency limits** | Global and per-user session caps |
| **Parallel task caps** | Limit tasks-per-session and total tasks-per-session |
| **Per-session timeout** | Configurable session-level timeout with partial-result return |
| **Per-task timeout** | Configurable per-task timeout, failing tasks don't take down the session |
| **Write-ahead log** | Session state persisted during execution for crash recovery |
| **Resumable sessions** | Sessions that timed out can be resumed by the original user |
| **Session history** | Optional persistent history with retention policy |
| **Session replay** | `cortex replay SESSION_ID` shows any historical session |

## Storage backends

| Backend | Use for | Notes |
|---|---|---|
| **Memory** | Tests, single-process dev | Volatile, zero config |
| **SQLite** | Single-host deployments | WAL mode, file-based, safe for one process |
| **Redis** | Multi-worker production | Distributed, horizontally scalable |

All three implement the same interface — swap via `storage` config, no code change.

## Security

| Feature | Description |
|---|---|
| **Input sanitisation** | Prompt injection mitigation on user inputs |
| **Credential scrubbing** | Redacts secrets from logs and event streams |
| **Bash sandbox** | Code execution task runs in a sandboxed subprocess |
| **WorkspaceBash** | Workspace-scoped file read/write and command execution with mandatory HITL before any mutating operation; path traversal blocked at resolve time |
| **HITL relay** | Ant subprocesses relay HITL prompts to the parent framework session so the user always controls workspace mutations, even from spawned agents |
| **API key via env vars** | Keys are never stored in config files |
| **Session ownership checks** | Resume is gated by the original `user_id` |

## Developer tooling

| Tool | What it does |
|---|---|
| **Setup wizard** | Browser-based `cortex.yaml` generator at `localhost:7799` — multi-step flow including ToolForge, LLM, storage, and publish mode |
| **Config Studio** | `cortex config-ui` launches a browser UI at `localhost:7801` to inspect and edit `cortex.yaml`, blueprints, staged deltas, and session metadata |
| **Service launcher** | From inside Cortex Synapse, open Config Studio or Setup Wizard with one click — the UI server launches them as background processes and waits for the port to open |
| **Dry-run validation** | `cortex dry-run` validates config and compiles task graph without LLM calls |
| **Hot-reload dev mode** | `cortex dev --watch` applies config changes live |
| **Session replay** | `cortex replay` shows request, response, task outcomes, validation report |
| **History search** | `GET /api/history/search?q=...` full-text search over session titles and responses |
| **Artifact ZIP** | `GET /api/history/{sid}/artifacts/zip` downloads all output files for a session |
| **Config migration** | `cortex migrate` checks `cortex.yaml` against the target schema version |
| **Capability manifest** | `cortex spec` emits a JSON/YAML description of the agent's capabilities |
| **Ant Colony CLI** | `cortex ants list / hatch / stop / stop-all / status` — inspect and manage the self-spawning specialist mesh |
| **Delta CLI** | `cortex delta review / apply / reject / history / rollback` — human-gated application of learned config changes |
| **Mock LLM client** | `cortex.testing.MockLLMClient` for unit tests without API calls |
| **Test config factory** | `cortex.testing.make_test_config()` for in-memory test configs |

## Identity & delegation

| Feature | Description |
|---|---|
| **`Principal` model** | First-class identity on every session and task — `user`, `system`, or `agent` |
| **Delegation chain** | `agent` principals carry a full chain recording every hop back to the originating user |
| **Origin-keyed storage** | `storage_key` always resolves to the originating user, so history / blueprints / learning stay attributed to the human across agent hops |
| **Audit provenance** | Operational stream and audit log record `principal_type` and full chain on every event |

## Built-in web search

When no `web_search` tool server is configured (or one fails), Cortex falls back to a built-in DuckDuckGo search client — no API key required.

| Behaviour | Detail |
|---|---|
| **Configured server first** | If a tool server has `web_search` capability, it is tried first |
| **Automatic fallback** | On failure or absence, the built-in DuckDuckGo client runs instead |
| **No config needed** | The `web_search` capability is always available as a built-in — just add task types that use it |

---

## App Control (native applications)

`app_control` lets the agent launch and drive desktop applications on the host machine. Enable it via `app_control.enabled: true` (or the Setup Wizard / Config Studio).

| Feature | Detail |
|---|---|
| **Two-path execution** | Primary: discover the app's scripting interface (macOS sdef, Windows UI Automation / COM, Linux AT-SPI / xdotool) and inject it into the LLM prompt so it generates precise actions. Fallback: screenshot → vision LLM → action → repeat. |
| **Cross-platform actions** | `launch_app`, `run_applescript` (macOS), `run_powershell` (Windows), `run_shell_command`, `screenshot`, `get_window_text`, `get_running_apps`, `copy_to_clipboard`, `paste_from_clipboard` |
| **HITL gate** | Every mutating action (launch / script / screenshot) requires user approval. Read-only queries (`get_running_apps`, `get_window_text`, `paste_from_clipboard`) never prompt. Batch approval covers a whole vision-loop task. |
| **Accessibility preflight (macOS)** | Detects missing Accessibility permission before AppleScript runs and surfaces a clear instruction message (instead of a cryptic `-1743` error). Result is cached per-session. |
| **Already-running check** | `launch_app` skips the launch and just activates the window if the app is already running — avoids duplicate instances. |
| **Auto-activate** | AppleScript that targets a specific app is auto-prefixed with `tell application "X" to activate` + a 0.3s delay so the window is frontmost before any keystroke. |
| **File pipeline** | When an upstream `code_exec` task produces files, they're surfaced as `UPSTREAM_FILES:` in the next task's instruction — letting `app_control` open / use whatever was just generated. |

## Built-in Browser Automation (Playwright)

`playwright_mcp.enabled: true` starts `@playwright/mcp` as an internal stdio MCP server at framework boot. It is NOT exposed as a configurable `tool_server` entry — users get a `browser` capability automatically. Requires Node.js + `npx` on PATH.

| Feature | Detail |
|---|---|
| **Three browser engines** | `chromium`, `firefox`, or `webkit` |
| **Headless or visible** | `headless: false` (default) for development; `headless: true` for CI / servers |
| **Configurable viewport** | `viewport_width` / `viewport_height` (default 1280×720) |
| **Session persistence** | Cookies + localStorage persisted to `storage_state_path` (auto-defaults to `{storage}/playwright_session.json`) so logins survive across runs |
| **Full profile mode** | Set `user_data_dir` for a complete persistent browser profile (extensions, IndexedDB, service workers). Takes precedence over `storage_state_path` when set. |
| **All Playwright MCP tools** | Navigate, click, type, screenshot, form fill, file upload — surfaced under the `browser` capability for the agent to use without any tool-server wiring |

## Polyglot code execution

`code_sandbox` is not Python-only. The first comment line of any generated script can declare its language with `# LANGUAGE: <lang>`.

| Language | Header | Runner | Notes |
|---|---|---|---|
| Python | (default — no header) | dedicated venv | Sentinel-wrapped result extraction; subprocess permitted |
| Node.js | `# LANGUAGE: node` | `node` | `# NPM_PACKAGES:` header triggers `npm install --prefix output_dir` |
| TypeScript | `# LANGUAGE: typescript` | `npx --yes ts-node --transpile-only` | Same NPM package mechanism |
| Deno | `# LANGUAGE: deno` | `deno run --allow-all` | TypeScript-native; no install needed |
| Shell / Bash | `# LANGUAGE: shell` | `bash` | Source written with exec bit set |
| Ruby | `# LANGUAGE: ruby` | `ruby` | `# GEM_PACKAGES:` header triggers `gem install` |
| Go | `# LANGUAGE: go` | `go run` | `# GO_PACKAGES:` header triggers `go get` |
| Rust | `# LANGUAGE: rust` | `rustc` → run | Compile-then-run; binary cleaned up after |
| C | `# LANGUAGE: c` | `cc` → run | Compile-then-run |
| Java | `# LANGUAGE: java` | `java` (single-file mode) | Java 11+ single-file source execution |
| Kotlin | `# LANGUAGE: kotlin` | `kotlinc -script` | Kotlin scripts (`.kts`) |

Two extra execution modes are available beyond the standard `execute()` path:
- **`execute_background()`** — start long-running processes (servers, daemons) and return immediately with a PID file (`.cortex_pid`) and log file (`.cortex_bg.log`) downstream tasks can read.
- **`execute_streaming()`** — line-by-line stdout streaming via an `on_line` async callback so the UI can show progress from long pipelines.

---

## Deployment targets

| Target | Command | Use for |
|---|---|---|
| **Docker image** | `cortex publish docker` | Containerised service deployment (`--with-ui` to bundle Cortex Synapse UI) |
| **Python wheel** | `cortex publish package` | Library distribution via pip/internal PyPI |
| **MCP server** | `cortex publish mcp` | Live aiohttp server at `/mcp`; auto-sets `CORTEX_INTERACTION_MODE=rpc` |
| **Chat UI** | `cortex publish ui` | Cortex Synapse web frontend: file uploads, SSE streaming, history search, artifact ZIP download |

## Observability

| Feature | Description |
|---|---|
| **OpenTelemetry hooks** | OTLP exporter built-in for traces and metrics |
| **Token usage accounting** | Per-role token counts (decomposition, execution, synthesis, validation) |
| **Typed event stream** | Structured events, not loose log strings |
| **Duration tracking** | Wall-clock time on every `SessionResult` |
| **Configurable log levels** | Via `CORTEX_LOG_LEVEL` env var |

## Configuration ergonomics

| Feature | Description |
|---|---|
| **Single YAML file** | All agent behavior in `cortex.yaml` |
| **Environment variable substitution** | `${VAR}` expansion inside YAML values |
| **Schema validation** | Invalid configs fail fast with clear error messages |
| **Browser wizard** | Full GUI config builder for non-YAML people |
| **Wizard field locking** | Re-running the wizard locks fields that would break existing data |
| **`CORTEX_CONFIG` env var** | Override default config path globally |
