# Overview

[← Back to README](../README.md)

## What is Cortex?

Cortex is a **production-grade, configuration-driven AI agent framework** for Python. It gives you a fully-featured multi-step agent — task decomposition, parallel tool execution, streaming, validation, and session management — defined entirely in a single `cortex.yaml` file.

You don't write an agent loop. You don't wire up tool-calling by hand. You don't build a retry/timeout/session system. You write YAML, call `framework.run_session()`, and ship.

## The problem Cortex solves

Building a production AI agent is a lot more than calling an LLM in a loop. Every team that ships one eventually builds the same plumbing:

- Decomposing a user request into sub-tasks
- Running those sub-tasks in parallel
- Handling tool calls via MCP or custom wrappers
- Streaming progress to the UI
- Scoring output quality and catching regressions
- Persisting sessions and resuming on failure
- Managing per-user concurrency limits
- Swapping LLM providers without rewriting everything
- Deploying as a service, a package, or an agent-to-agent tool

Most teams rebuild this stack two or three times before they ship. **Cortex is that stack, pre-built, battle-tested, and driven by a single config file.**

## Who is Cortex for?

| You are… | Cortex helps you… |
|---|---|
| A **startup founder** shipping an AI product | Skip 3–6 months of framework engineering and focus on your vertical |
| A **platform team** at a larger company | Give every product team a consistent, governed agent runtime |
| An **enterprise architect** | Deploy multi-agent meshes with per-agent isolation, quality gates, and audit trails |
| A **solo developer / indie hacker** | Prototype an agent in an afternoon without learning a framework-of-the-month |
| A **researcher** | Swap LLM providers and tool configs from YAML without touching code |
| An **MLOps engineer** | Get validation scores, session replay, and delta learning out of the box |

## Core design principles

### 1. Configuration over code
Everything that can live in YAML does. Agent identity, LLM routing, task types, tool servers, concurrency limits, storage, validation thresholds, learning policy. You change behavior by editing `cortex.yaml`, not by rewriting Python.

### 2. Fan-out / fan-in by default
Real user requests rarely map to one LLM call. "Research X and write a report" is a research task *and* a writing task with a dependency between them. Cortex's primary agent decomposes the request into a typed task graph, runs independent tasks in parallel, and synthesises the results automatically.

### 3. MCP as the tool protocol
Cortex speaks the [Model Context Protocol](https://modelcontextprotocol.io) natively — SSE, stdio, and streamable-HTTP transports all supported. Any MCP-compatible tool server (filesystem, brave-search, GitHub, your own) plugs in with three lines of YAML.

### 4. Agents compose
Any Cortex agent can be published as an MCP server and consumed by another Cortex agent as a tool. That's how you build multi-agent systems: specialist agents become capabilities of an orchestrator. No custom inter-agent protocol to design.

### 5. Quality is measured, not assumed
Every response runs through a validation agent that scores it on intent match, completeness, and coherence. You set a floor; responses below the floor are flagged. This is how you catch regressions before users do.

### 6. Observability is first-class
Streaming events are typed dataclasses, not loose dicts. Sessions are replayable. Token usage is bucketed by role (decomposition, execution, synthesis, validation). OpenTelemetry hooks are built in.

### 7. Deployment is a command, not a project
`cortex publish docker` generates a Dockerfile. `cortex publish package` builds a wheel. `cortex publish mcp` runs the agent as an MCP server for other agents to consume. Pick the target; Cortex handles the rest.

## What Cortex is *not*

- **Not a low-code builder.** It's a library you integrate into a Python app. The config file is Python-adjacent, not a visual flow editor.
- **Not an LLM gateway.** It uses providers; it doesn't replace them. Bring your own API key.
- **Not a vector database or RAG system.** It calls MCP tools that do RAG — it doesn't implement retrieval itself.
- **Not a replacement for your web framework.** Cortex runs *inside* FastAPI/Django/Flask/Click — it doesn't ship its own HTTP layer for your app to use.

## Where to go next

- **[Getting Started](GETTING_STARTED.md)** — build a working agent in 5 minutes
- **[Architecture](ARCHITECTURE.md)** — see how it works under the hood
- **[Features](FEATURES.md)** — the full capability matrix
- **[Use Cases](USE_CASES.md)** — reference architectures for common scenarios
