# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

- Blueprint Store: persistent markdown templates per task type, auto-updated post-session with user consent, with staleness checks that trigger re-discovery.
- History Store with encryption support and automatic retention cleanup.
- Result Envelope Store with in-process hot path and SQLite/Redis crash-resilience backing.
- Learning Engine with delta proposals gated by confidence levels (medium = 3, high = 5) and end-of-session evolution consent for promoting ad-hoc scripts to reusable task types.

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

[1.1.0]: https://github.com/kritird/Cortex-Agent-Framework/releases/tag/v1.1.0
[1.0.0]: https://github.com/kritird/Cortex-Agent-Framework/releases/tag/v1.0.0
