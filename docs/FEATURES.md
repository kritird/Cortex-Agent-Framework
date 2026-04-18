# Features

[← Back to README](../README.md)

A complete feature matrix of everything Cortex ships with.

## Core orchestration

| Feature | Description |
|---|---|
| **Fan-out / fan-in execution** | Primary agent decomposes requests into a dependency DAG; independent tasks run in parallel |
| **Three execution modes** | `adaptive` (LLM free-form), `pinned` (LLM executes, but DAG locked to blueprint topology), `scripted` (Python handler, no LLM) |
| **Typed task graph** | Every task has a declared type, output format, dependencies, capability hint, and execution mode |
| **Cycle detection** | Task graph compiler rejects cyclic graphs before execution starts |
| **Topological execution** | Tasks run as soon as their dependencies complete — no fixed pipeline stages |
| **Capability-aware decomposition** | Decomposer sees currently-available MCP tools and plans around them |
| **Synthesis step** | Primary agent stitches task outputs into a coherent final response |
| **Clarification support** | Agent can pause mid-session and ask follow-up questions via `ClarificationEvent` |

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
| Custom | `custom` | — | Provide a Python dotted path |

**Per-task model routing**: override the default model for specific task types — e.g. run decomposition on a cheap fast model and synthesis on the flagship.

## Model Context Protocol (MCP)

| Feature | Description |
|---|---|
| **SSE transport** | Connect to remote MCP servers over Server-Sent Events |
| **stdio transport** | Spawn MCP servers as subprocesses with stdin/stdout pipes |
| **streamable-HTTP transport** | Full MCP 1.x streamable HTTP support |
| **Capability discovery** | Dynamic tool discovery at session start |
| **Header injection** | Per-server HTTP headers (auth tokens, API keys) |
| **Lifecycle management** | Auto-start, auto-restart, graceful shutdown of tool servers |
| **Publish as MCP server** | Export your Cortex agent *as* an MCP server for other agents to call |

## Multi-agent composition

| Feature | Description |
|---|---|
| **Agent-as-MCP-tool** | Any Cortex agent can be published as an MCP server |
| **Orchestrator pattern** | Parent agents list sub-agents in `tool_servers` and decompose across them |
| **Independent lifecycles** | Each agent has its own config, storage, concurrency, LLM routing |
| **Port conventions** | Standard port allocation (wizard `7799+N`, MCP `8080+N`) for multi-agent hosts |
| **No custom protocol** | Uses MCP end-to-end — no bespoke inter-agent RPC |
| **Ant Colony** | Orchestrator self-spawns specialist Cortex agents as MCP servers at runtime; supervised, health-checked, auto-restarted |

## Streaming

| Event class | Fields | Use |
|---|---|---|
| `StatusEvent` | `message`, `session_id`, `event_type`, `metadata` | Progress updates for the UI |
| `ResultEvent` | `content`, `partial`, `validation_score`, `metadata` | Final or streaming response content |
| `ClarificationEvent` | `question`, `options`, `clarification_id` | Agent is asking a follow-up question |

Event types: `SESSION_START`, `TASK_START`, `TASK_COMPLETE`, `STATUS`, `RESULT`, `ERROR`, `SESSION_END`, `CLARIFICATION`.

Wires into FastAPI SSE, WebSockets, or any async consumer pattern.

## Quality & validation

| Feature | Description |
|---|---|
| **Composite scoring** | Every response scored on intent match, completeness, coherence |
| **Configurable threshold** | Set a minimum acceptable score (hard floor: 0.60) |
| **Per-session validation report** | Returned on `SessionResult.validation_report` |
| **Model override** | Run validation with a different model than task execution |

## Delta learning

| Feature | Description |
|---|---|
| **Pattern observation** | Tracks task decomposition patterns across sessions |
| **Consent gating** | Only learns from sessions where the user granted consent |
| **Confidence levels** | Medium (3 confirmations) / High (5 confirmations) from distinct users |
| **Human-in-the-loop review** | `cortex delta review` shows staged proposals |
| **Apply with rollback** | `cortex delta apply` writes to `cortex.yaml`, `cortex delta rollback` restores the prior version |
| **Auto-apply mode** | Optional: auto-apply high-confidence proposals |

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
| **API key via env vars** | Keys are never stored in config files |
| **Session ownership checks** | Resume is gated by the original `user_id` |

## Developer tooling

| Tool | What it does |
|---|---|
| **Setup wizard** | Browser-based `cortex.yaml` generator at `localhost:7799` |
| **Dry-run validation** | `cortex dry-run` validates config and compiles task graph without LLM calls |
| **Hot-reload dev mode** | `cortex dev --watch` applies config changes live |
| **Session replay** | `cortex replay` shows request, response, task outcomes, validation report |
| **Config migration** | `cortex migrate` checks `cortex.yaml` against the target schema version |
| **Capability manifest** | `cortex spec` emits a JSON/YAML description of the agent's capabilities |
| **Mock LLM client** | `cortex.testing.MockLLMClient` for unit tests without API calls |
| **Test config factory** | `cortex.testing.make_test_config()` for in-memory test configs |

## Deployment targets

| Target | Command | Use for |
|---|---|---|
| **Docker image** | `cortex publish docker` | Containerised service deployment |
| **Python wheel** | `cortex publish package` | Library distribution via pip/internal PyPI |
| **MCP server** | `cortex publish mcp` | Expose agent as a tool for other agents |

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
