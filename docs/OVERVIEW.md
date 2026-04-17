# Overview

[← Back to README](../README.md)

## What is Cortex?

Cortex is a **production-grade, configuration-driven AI agent framework** for Python. You define an agent — its identity, LLM, tools, task types, quality bar, and deployment target — in a single `cortex.yaml` file. Cortex handles everything else: decomposing user requests into parallel task graphs, calling MCP tool servers, streaming live progress, scoring response quality, persisting sessions, and deploying as Docker, a Python package, an MCP server, or a ready-made chat UI.

**One YAML file. One method call. A production agent.**

```python
framework = CortexFramework("cortex.yaml")
await framework.initialize()
result = await framework.run_session(user_id="u1", request="Analyse Q3 revenue")
```

That's the integration. Everything else — orchestration, parallelism, tool calls, retries, validation, streaming, history — is handled.

---

## Why teams choose Cortex

### You skip months of framework engineering

Every AI team eventually builds the same stack: task decomposition, parallel tool execution, streaming events, retry logic, session management, quality scoring, multi-provider routing, deployment. Most teams rebuild it two or three times before shipping.

Cortex is that stack. Pre-built. Battle-tested. Driven by config, not code.

### You go from idea to running agent in minutes

```bash
pip install cortex-agent-framework
cortex setup            # visual wizard at localhost:7799
cortex publish ui       # chat UI at localhost:8090
```

Three commands. You have a working agent with a professional web interface, file upload support, streaming responses, and persistent chat history. No frontend to build, no backend to wire, no infrastructure to set up.

### You change behavior without changing code

LLM provider? YAML. Task types? YAML. Concurrency limits? YAML. Validation threshold? YAML. Tool servers? YAML. Cortex's design principle is radical: **if it can live in configuration, it must.** Your Python code stays a thin wrapper — the agent's behavior lives in `cortex.yaml`, versioned, diffable, reviewable.

### You get multi-agent composition for free

Any Cortex agent can be published as an MCP server in one command. Another Cortex agent adds it to `tool_servers` and calls it like any tool. That's the entire inter-agent protocol — standard MCP, nothing custom.

```
Orchestrator → Research Agent (MCP :8081) → brave-search, wikipedia
             → Code Review Agent (MCP :8082) → github, filesystem
             → Writing Agent (MCP :8083) → document-gen
```

Each agent scales, deploys, and configures independently. You compose them by adding YAML lines.

### You don't guess whether the agent is good

Every response passes through a **Validation Agent** that scores intent match, completeness, and coherence. You set a floor (default: 0.75); responses below the floor are flagged and optionally remediated. Per-task validation gates run inside the execution loop — bad intermediate outputs are retried with feedback before the pipeline moves on.

This is how you catch regressions before users do, not after.

### Your agent gets smarter over time

The **Learning Engine** observes task patterns across sessions. When the same decomposition pattern recurs, it stages a **delta proposal** — a concrete config change you can review with `cortex delta review` and apply in one command. The agent doesn't silently drift; it surfaces what it learned and asks for approval.

**Blueprints** capture workflow knowledge in versionable markdown: the dos, don'ts, and lessons learned for each task type. They're loaded into the LLM's context on the second run and auto-updated (with consent) after every session.

---

## What makes Cortex different

| Capability | Cortex | Typical agent frameworks |
|---|---|---|
| **Configuration** | Single `cortex.yaml` drives everything | Scattered code, env vars, multiple config files |
| **Task orchestration** | LLM-generated DAG with parallel fan-out/fan-in | Sequential chain or hand-coded state machine |
| **Tool protocol** | Native MCP (SSE, stdio, streamable-HTTP) | Custom tool wrappers per integration |
| **Multi-agent** | Any agent becomes an MCP tool in one command | Bespoke inter-agent protocols |
| **Quality gates** | Built-in validation agent with scoring + remediation | Manual testing or nothing |
| **Learning** | Delta proposals + blueprints with human review | Prompt tweaking by hand |
| **LLM providers** | 8 built-in (Anthropic, OpenAI, Gemini, Grok, Mistral, DeepSeek, Bedrock, Azure) | Usually 1–2, hard-coded |
| **Per-task routing** | Route decomposition to a fast model, synthesis to flagship | One model for everything |
| **Streaming** | Typed event classes (StatusEvent, ResultEvent, ClarificationEvent) | Loose dicts or raw SSE strings |
| **Session management** | Persistence, resume, per-user concurrency, WAL crash recovery | In-memory or DIY |
| **Deployment** | `publish docker`, `publish package`, `publish mcp`, `publish ui` | Write your own Dockerfile |
| **Setup** | Visual wizard (`cortex setup`) + CLI | Read docs, write boilerplate |
| **Chat UI** | Built-in web frontend with file upload + history | Build your own or use a third-party tool |
| **Observability** | OpenTelemetry, audit log, anomaly detection, token accounting | Add your own logging |
| **Security** | Input sanitiser, credential scrubber, code sandbox, MCP output guard | Hope for the best |

---

## Who is Cortex for?

| You are… | Cortex gives you… |
|---|---|
| A **startup founder** shipping an AI product | A production-grade agent runtime in an afternoon — skip 3–6 months of plumbing and focus on your domain |
| A **platform team** at a larger company | A governed, consistent agent runtime with audit trails, quality gates, and per-user isolation for every product team |
| An **enterprise architect** | Multi-agent meshes with independent scaling, configurable security, and compliance-friendly history encryption |
| A **solo developer** | Prototype to production with one YAML file — no framework-of-the-month to learn |
| A **researcher** | Swap LLM providers, models, and tools from config — run experiments without touching code |
| An **MLOps engineer** | Validation scores, session replay, token accounting, delta learning, and OpenTelemetry hooks out of the box |

---

## The 60-second pitch

You write a YAML file describing your agent. Cortex reads it and gives you:

- **Automatic task decomposition** — the LLM breaks requests into a typed dependency graph
- **Parallel execution** — independent tasks run simultaneously, not sequentially
- **MCP tool servers** — connect any tool with three lines of YAML
- **8 LLM providers** — switch models without code changes, route different tasks to different models
- **Response validation** — every output scored; regressions caught automatically
- **Delta learning** — agent proposes its own improvements; you review and approve
- **Blueprints** — reusable workflow knowledge that makes the agent better over time
- **Streaming events** — typed, structured events for any UI (SSE, WebSocket, CLI)
- **Session persistence** — resume timed-out sessions, replay history, encrypt at rest
- **Built-in chat UI** — professional web frontend with file uploads and conversation history
- **4 deployment targets** — Docker, Python package, MCP server, chat UI
- **Visual setup wizard** — configure everything from a browser, no docs required
- **Security built-in** — input sanitisation, credential scrubbing, sandboxed code execution
- **Observability** — OpenTelemetry, audit logs, anomaly detection, token budgets

All of this is in the box. No plugins to install. No boilerplate to write. No infrastructure to manage.

**Define once. Deploy anywhere. Let it learn.**

---

## What Cortex is *not*

- **Not a low-code builder.** It's a Python library you integrate into your app. The config file replaces boilerplate, not code.
- **Not an LLM gateway.** It uses providers; it doesn't replace them. Bring your own API key.
- **Not a vector database or RAG system.** It calls MCP tools that do RAG — it doesn't implement retrieval itself.
- **Not a replacement for your web framework.** Cortex runs *inside* FastAPI/Django/Flask/Click. The built-in chat UI is a standalone publish target, not a framework you build on.

---

## Where to go next

| Goal | Page |
|---|---|
| Build your first agent in 5 minutes | **[Getting Started](GETTING_STARTED.md)** |
| Understand how it works under the hood | **[Architecture](ARCHITECTURE.md)** |
| See the full capability matrix | **[Features](FEATURES.md)** |
| Browse reference architectures | **[Use Cases](USE_CASES.md)** |
| Configure every knob | **[Configuration](CONFIGURATION.md)** |
| Deploy to production | **[Deployment](DEPLOYMENT.md)** |
| CLI commands reference | **[CLI](CLI.md)** |
