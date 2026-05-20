# FAQ

[← Back to README](../README.md)

## General

### What exactly is Cortex?

A Python library (`cortex-agent-framework`) that gives you a production-grade multi-step AI agent driven entirely by a `cortex.yaml` config file. It handles task decomposition, parallel tool execution, MCP integration, streaming, validation, and session persistence so you can focus on your use case instead of rebuilding agent plumbing.

### How is this different from LangChain / LlamaIndex / CrewAI / AutoGen?

- **Config-first *or* code-first.** Define an agent in `cortex.yaml` *or* build it in Python with `CortexBuilder`. Either way the config replaces boilerplate, not your business logic.
- **Fan-out / fan-in as a core primitive.** Parallel tool execution with a dependency DAG is first-class, not an advanced feature you build yourself.
- **MCP-native.** Cortex speaks MCP end-to-end — tool servers *and* agent-to-agent composition both use MCP. There's no bespoke inter-agent protocol.
- **Opinionated production stack.** Session management, validation scoring, delta learning, replay, hot-reload, and deployment targets all ship in the box.

That said, Cortex plays well next to those tools. It doesn't compete with vector DBs or RAG libraries — it calls them via MCP.

### Can I define agents in Python instead of YAML? How does this compare to LangGraph?

Yes. Use `CortexBuilder` to build the whole agent in code, and the `@agent.node` decorator to wire **plain Python functions in as graph nodes** — the LangGraph-style pattern:

```python
from cortex import CortexBuilder, CortexFramework

agent = CortexBuilder("MyAgent", "does things")
agent.llm("anthropic", model="claude-sonnet-4-5", api_key_env="ANTHROPIC_API_KEY")

@agent.node()
async def fetch(ctx):
    return await ctx.call_tool("brave", "search", query=ctx.request)

@agent.node(depends_on=["fetch"])
async def summarise(ctx):
    return await ctx.llm(f"Summarise: {ctx.deps['fetch']}")

framework = CortexFramework(config=agent.build())
```

The difference from LangGraph: by default Cortex *plans* the graph for you with an LLM (`execution_mode="planned"`). The moment you register a code node it switches to `execution_mode="static"` — your declared DAG runs verbatim, exactly like a hand-authored LangGraph. Either way you keep Cortex's wave engine, validation gate, retries, streaming events, and session persistence. See [Getting Started § Code-First Agents](GETTING_STARTED.md#code-first-agents--cortexbuilder).

### When should I use `cortex.yaml` vs. `CortexBuilder`?

Use **`cortex.yaml`** when the agent definition should be diffable and reviewed separately from app code, non-developers tune it, or you want the setup wizard and CLI. Use **`CortexBuilder`** when you prefer code, want the definition next to the rest of your app, or want code nodes (`.node()`) for deterministic, Python-driven graph steps. They are not mutually exclusive — `.task()` (LLM-routed) and `.node()` (Python) mix freely in one builder.

### Is Cortex production-ready?

Yes. It has concurrency limits, session persistence with WAL replay, quality validation, typed streaming events, OpenTelemetry hooks, and three storage backends. The test suite covers the core modules.

### What's the license?

MIT. Use it commercially, fork it, modify it, ship it.

---

## Installation & setup

### `pip install -e .` fails with "Cannot import 'setuptools.backends.legacy'"

That was a bug in an older `pyproject.toml`. Pull the latest — the `build-backend` is now `setuptools.build_meta` which works correctly.

### The setup wizard won't open

- Check the port isn't in use: `lsof -i :7799`
- Pass `--no-browser` and open the URL manually
- Use a different port: `cortex setup --port 7800`

### Can I run Cortex without the setup wizard?

Yes. The wizard just generates a YAML file. You can write `cortex.yaml` by hand — see [CONFIGURATION.md](CONFIGURATION.md) for every field.

---

## Running multiple agents

### Can I run multiple Cortex agents on one machine?

Absolutely — that's the designed pattern. See [DEPLOYMENT.md § Multi-agent deployment](DEPLOYMENT.md#multi-agent-deployment). Each agent needs its own directory, its own `cortex.yaml`, and its own ports. The defaults (port 7799 for the wizard, 8080 for MCP publish, `./cortex.yaml` for config) are just defaults — all of them are overridable.

### Can two agents share a SQLite database?

**No.** SQLite locks the DB file; two Cortex processes pointing at the same `sqlite.path` will fail intermittently. Give each agent its own `storage.base_path` (and therefore its own DB file), or use Redis with a unique `key_prefix` per agent.

### Can two agents share a Redis instance?

Yes, as long as you set a unique `redis.key_prefix` per agent so their sessions don't collide.

---

## LLM providers

### Can I mix providers in one agent?

Yes. Set `llm_access.default.provider` to one provider, then use `llm_access.task_overrides` to route specific task types through a different provider/model:

```yaml
llm_access:
  default:
    provider: anthropic
    model: claude-sonnet-4-5
  task_overrides:
    cheap_summary:
      provider: deepseek
      model: deepseek-chat
    heavy_reasoning:
      provider: anthropic
      model: claude-opus-4-6
      thinking_budget_tokens: 5000
```

### Can I point Cortex at a proxy or gateway (LiteLLM, OpenRouter, etc.)?

Yes. Use `provider: anthropic_compatible` or `provider: openai` with a `base_url`:

```yaml
llm_access:
  default:
    provider: anthropic_compatible
    base_url: https://my-gateway.internal/v1
    api_key_env_var: GATEWAY_KEY
    model: claude-sonnet-4-5
```

### Can I use a provider Cortex doesn't ship with?

Yes — write a Python function and point to it with `provider: custom` + `function: my_module.my_provider`. See [CONFIGURATION.md](CONFIGURATION.md) for the function signature.

---

## Task graph & execution

### What happens if one task fails?

The task is marked failed, the session continues, and the Primary Agent synthesises what it can from the successful tasks. The `SessionResult.task_completion` field reports which tasks succeeded, failed, or timed out. You decide whether to surface a partial response or retry.

### What's the max parallelism?

Controlled by `agent.concurrency.max_parallel_tasks` (default 5). Dependencies always take precedence — a task waits for its `depends_on` regardless of the parallel cap.

### Do I need to set `max_parallel_llm_calls`?

No — it's auto-derived from your provider+model at startup (e.g. `1` for `local:*`, `8` for `anthropic:*haiku*`, `4` for `anthropic:*opus*`) and then self-tuned at runtime by `AdaptiveLLMGate` based on observed latency and errors. The field was removed from the setup wizard in v1.5.0. Set it explicitly in `cortex.yaml` only when you need a fixed ceiling — benchmarking, or a hard rate-limited API. See [CONFIGURATION.md § "LLM concurrency auto-tuning"](CONFIGURATION.md#llm-concurrency-auto-tuning).

### The LLM sometimes returns a task list with a cyclic dependency. What then?

The Task Graph Compiler detects cycles and fails the session with a clear error before execution starts. This is a safety net specifically because LLMs hallucinate.

### Can I inspect the task graph the LLM generated?

Yes, via `cortex dry-run "your request"` (which decomposes without executing) or `cortex replay SESSION_ID` (which shows a historical decomposition). Both print the compiled graph.

---

## Intent Gate & interaction modes

### Why did my "hi" no longer trigger a full task pipeline?

That's the **Intent Gate** doing its job. It classifies each turn (heuristic first, LLM cascade only when the heuristic is under-confident) and sends chat-shaped turns through `PrimaryAgent.converse()` — which streams a direct reply and skips scout, decomposition, execution, validation, and evolution. Turn it off with `agent.intent_gate.enabled: false` if you want every turn to decompose.

### What's the difference between `interactive` and `rpc`?

- **`interactive`** — for chat UIs, CLIs, dev mode. Conversational turns skip the full pipeline; `ClarificationEvent`s (intent gate, HITL task prompts, timeout-extension offers) are emitted and a human answers. Learning fires automatically at end-of-session via the autonomic gate — no consent prompt is shown.
- **`rpc`** — for agents exposed as callables (e.g. published MCP servers). Every turn is forced to the task path. Interactive clarifications are suppressed so automated callers never hang on a prompt they can't answer. `cortex publish mcp` sets this automatically via `CORTEX_INTERACTION_MODE=rpc`.

### Can I override interaction mode at runtime?

Yes. `CORTEX_INTERACTION_MODE=interactive|rpc` beats the value in `cortex.yaml`.

---

## Chat UI (Cortex Synapse)

### How do I get the built-in chat UI?

```bash
cortex publish ui --port 8090
# Cortex chat UI: http://localhost:8090
```

It serves **Cortex Synapse** — a single-page web frontend with text + file uploads, live task blueprint display, intent classification badges, workspace event streaming, token usage, full-text history search, and artifact ZIP download. Configure title, host, port, and auth (`none` / `token` / `basic`) under the `ui:` block in `cortex.yaml` or via the wizard. See [CORTEX_SYNAPSE.md](CORTEX_SYNAPSE.md) for the full feature list and REST API.

### The printed URL shows `0.0.0.0` — is that right?

No — the server binds `0.0.0.0` so it accepts connections on all interfaces, but browsers cannot connect to that address. Cortex Synapse now always prints `localhost` as the clickable URL, which is the correct browser address.

### Will chat history survive a restart?

Only if `history.enabled: true` and your storage is SQLite or Redis. The Memory backend loses everything on restart.

### How does mid-session file upload work?

Use `POST /api/session/{ui_id}/upload` with a multipart form — Cortex Synapse's UI shows a drag-and-drop area in the composer. Uploaded files are queued into the running session and passed to the next request.

### Can I download all outputs from a session?

Yes — `GET /api/history/{sid}/artifacts/zip` streams a ZIP archive of all task output files. In Cortex Synapse, there's a download-all button on each session in the history panel.

---

## Ant Colony

### What's an "ant"?

An independent Cortex agent — with its own `cortex.yaml` — that the orchestrator spawns at runtime as an MCP server to fill a capability gap the Capability Scout couldn't fill from configured or discovered servers. Ants are supervised (PID watched, auto-restart on crash) and persisted to `ants.yaml` so they come back across framework restarts.

### When do ants get hatched?

When `ant_colony.enabled: true` **and** (a) `auto_hatch_on_gap: true` and the scout finds an unfillable gap, or (b) you explicitly call `cortex ants hatch <name> --capability <cap>` / `framework.hatch_ant(...)`, or (c) ToolForge generates and registers a new MCP server at a wave boundary.

### Are ants trusted?

They're registered with `trust_tier: ant` — treated like internal servers (write tools allowed, no output guard) but persisted separately from developer-configured servers so you can audit what got spawned.

---

## ToolForge

### What is ToolForge?

ToolForge lets the decomposer assign `forge_mcp` tasks that generate a FastMCP server script from LLM-produced code, write it to disk, and spawn it with Ant Colony at the next wave boundary. Dependent tasks in the same session can use the new server immediately.

Enable it in `cortex.yaml`:

```yaml
ant_colony:
  enabled: true
code_sandbox:
  enabled: true
tool_forge:
  enabled: true
```

All three must be `true` for `forge_mcp` to appear in the decomposition prompt.

### Are forged servers persisted across restarts?

By default no (`persist_by_default: false`). The server entry is written to `ants.yaml` with `auto_restart: false` — it won't be re-hatched on the next framework startup. Set `persist_by_default: true` to change this. You can also change an individual server's `auto_restart` in `ants.yaml` by hand.

### How do I debug a forge failure?

- Check `cortex_storage/ants/<task_name>/server.py` — the generated script is always written there, even if the server failed to start.
- Look at the framework log: `ToolForge: failed to hatch forge ant '<name>': ...`
- Increase `spawn_timeout_seconds` if the server is starting but not passing `/health` fast enough.

---

---

## Streaming & UI integration

### How do I stream to a web UI?

Pass an `asyncio.Queue()` to `run_session()`. The queue receives typed event objects as work progresses. Key events: `StatusEvent`, `ResultEvent`, `ClarificationEvent`, `IntentClassifiedEvent`, `TaskBlueprintEvent`, `TaskToolCallEvent`, `WorkspaceEvent`, `FileOutputEvent`, `SessionTokenUsageEvent`. Wire them into FastAPI SSE or your websocket layer — see the full example in [GETTING_STARTED.md § Usage 1](GETTING_STARTED.md#usage-1-conversational-chat-ui).

### How do clarifications work?

If the agent decides it needs more information, it emits a `ClarificationEvent` with a question and a `clarification_id`. Your UI shows the question, collects the user's answer, and calls the appropriate resolver — `framework.resolve_task_clarification(clarification_id, answer)` for mid-task HITL prompts, or `framework.resolve_timeout_extension(clarification_id, "yes"|"no")` for session-timeout-extension offers. The agent picks up the answer and continues.

### Can I interrupt a running session?

Cancel the `asyncio.Task` running `run_session()`. The session manager will mark the session as cancelled and clean up.

---

## Quality & validation

### Can I turn off validation?

Yes — `validation.enabled: false`. You'll lose the quality score, but sessions run slightly faster.

### What does "composite score 0.75" actually mean?

It's a weighted combination of three sub-scores (intent match, completeness, coherence), each on 0–1. The Validation Agent produces them. There's a hard floor of 0.60 — scores below that always flag.

### Can I validate with a cheaper model?

Yes — `validation.model` lets you override the model used for validation calls independently of the default.

---

## Autonomic learning

### How does learning decide to fire?

At the end of every session the framework runs a **signal-driven gate** (not a consent prompt):

1. **Skip guards** — chat turns, RPC calls with no principal, or `learning.enabled: false` exit immediately.
2. **Scoring gates** — the `TaskComplexityScorer` produces a deterministic 0.0–1.0 score from code synthesis / tool trace / fan-out / deps / tokens / duration; the composite validation score must clear `validation_threshold`. When both gates clear, learning fires.

The outcome is emitted as a `LearningEvent` (`staged`, `applied`, `blueprint_updated`, `skipped_*`, …) and written to `HistoryRecord.learned_action`.

### What actually gets saved?

Two paths, decided by whether the task was in `cortex.yaml` at runtime:

- **Ad-hoc tasks** (new capabilities discovered this session) are staged into `cortex_delta/pending.yaml` with a seeded draft blueprint under `drafts/{task_name}__{hash}`. Any generated Python script is persisted to the code store immediately.
- **Known tasks** (already in `cortex.yaml`) have their existing blueprints refined via an LLM-driven blueprint auto-update.

### Does it modify my config automatically?

By default yes — `auto_apply_delta: true`. Promotion still requires distinct-principal accumulation (default: 3 users with `auto_apply_min_confidence: medium`), so a single session can never rewrite your config on its own. Set `auto_apply_delta: false` to keep deltas in `pending.yaml` until you run `cortex delta review` / `cortex delta apply`.

### Can I opt out?

Yes — `learning.enabled: false`. Or keep learning on but tighten the gates (raise `validation_threshold` / `complexity_threshold`, or set `auto_apply_delta: false` to require manual promotion).

---

## Security & compliance

### Where are API keys stored?

In environment variables. `cortex.yaml` references them by name (`api_key_env_var: MY_KEY`). Keys are never written to config files or logs.

### Are user inputs sanitised?

Yes. Cortex scrubs common prompt-injection patterns on user inputs and redacts credential-like strings from logs and event streams. See [`cortex/security/`](../cortex/security/).

### Is session data encrypted at rest?

Cortex doesn't encrypt the storage backend itself — that's your storage layer's job (SQLite with SQLCipher, Redis with TLS + at-rest encryption, etc.). The framework handles the data; you control where it lives.

### Can I run Cortex fully offline?

Yes, if your LLM and tool servers are local. Use an Ollama or vLLM-backed `anthropic_compatible`/`openai` provider for the LLM, and stdio MCP servers for tools. No Cortex code talks to the internet unless your config tells it to.

---

## Operations

### How do I upgrade `cortex.yaml` when the schema changes?

`cortex migrate [--to-version X.Y]`. It validates your current config and applies any needed migrations.

### How do I debug a failing session?

1. `cortex replay SESSION_ID --user-id USER_ID` — see exactly what happened
2. Set `CORTEX_LOG_LEVEL=DEBUG`
3. Use `cortex dry-run` to validate the task graph before running it
4. Inspect `SessionResult.task_completion` for per-task failure info

### How do I monitor Cortex in production?

- **Metrics & traces**: OpenTelemetry OTLP exporter is built in. Set standard `OTEL_*` env vars.
- **Structured logs**: every event has a stable `event_type`; pipe stdout to your log aggregator.
- **Token usage**: `SessionResult.token_usage` breaks tokens down by role (decomposition, execution, synthesis, validation) — surface it to your billing/budget system.

### Can I run Cortex in Kubernetes?

Yes. `cortex publish docker` produces a Dockerfile you can deploy to any K8s cluster. Use Redis for storage (shared across replicas), configure HPAs on CPU or on a custom metric from the OTEL exporter, and set concurrency limits per pod.

---

## Still stuck?

- File an issue on the GitHub repo with your `cortex.yaml` (redacted) and the exact error
- Use `cortex dry-run` first — it catches most config mistakes
- Check the [Configuration Reference](CONFIGURATION.md) — every field is documented
- Read [Getting Started](GETTING_STARTED.md) for end-to-end working examples
