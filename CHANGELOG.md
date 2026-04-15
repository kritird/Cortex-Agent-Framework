# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[1.0.0]: https://github.com/kritird/cortex-agent-framework/releases/tag/v1.0.0
