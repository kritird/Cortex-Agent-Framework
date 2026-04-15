# Use Cases

[← Back to README](../README.md)

Real-world scenarios where Cortex earns its place in your stack. Each example sketches the agent layout, the relevant `cortex.yaml` shape, and why Cortex is a good fit.

---

## 1. Customer support triage agent

**Problem:** Your support team drowns in inbound tickets. You want an agent that reads each ticket, classifies it, attaches relevant docs, drafts a reply, and routes to the right human queue.

**Agent layout:**
```
Inbound ticket → CortexFramework → triage result → ticket system
```

**Task types:**
- `classify` — route to product area (billing / integration / bug / feature-request)
- `retrieve_docs` — search internal docs MCP server for relevant articles
- `draft_reply` — write a customer-facing draft (depends_on: classify, retrieve_docs)
- `severity_score` — assign P0/P1/P2 based on language

**Why Cortex:** The parallel fan-out means classify + retrieve_docs + severity_score all run simultaneously, then draft_reply synthesises them. Validation catches bad drafts before they reach customers. Delta learning surfaces new ticket patterns ("crypto payment failures") that you didn't predict.

**Deployment:** Docker microservice behind your ticket system's webhook.

---

## 2. Competitive research agent

**Problem:** Product managers want a weekly "what did our competitors ship this week" report. Currently someone reads 12 websites and writes it up.

**Agent layout:**
```
Scheduled trigger → CortexFramework → Markdown report → Slack / email
```

**Task types:**
- `fetch_competitor_news` — web search MCP for each competitor (fans out N-ways)
- `extract_features` — parse announcements into feature/version pairs
- `compare_with_ours` — pull our own release notes from an internal MCP
- `write_brief` — synthesise into a 1-page brief (depends on all of the above)

**Why Cortex:** The fan-out over N competitors is a natural fit — each competitor fetch is an independent task running in parallel. Session history lets PMs compare this week's brief to last week's. `cortex replay` gives a reproducible audit trail.

**Deployment:** Background worker behind a cron (GitHub Actions, Celery Beat, AWS EventBridge).

---

## 3. Internal developer tool ("ask the monorepo")

**Problem:** Engineers ask "where does the invoicing logic live?" or "why was the retry policy changed?" and waste hours grepping.

**Agent layout:**
```
Developer in IDE → Cortex CLI / MCP → answer with file:line references
```

**Task types:**
- `search_code` — MCP filesystem / ripgrep tool
- `search_git_history` — MCP git tool
- `search_docs` — internal Confluence MCP
- `synthesise_answer` — combine all three (depends_on: search_code, search_git_history, search_docs)

**Why Cortex:** Published as an **MCP server** (`cortex publish mcp`), it plugs directly into Claude Desktop, VS Code, Cursor, or any MCP-capable IDE. Every engineer gets the monorepo assistant as a built-in tool. One YAML, every developer productive.

**Deployment:** MCP server running on an internal host. IDE config points at `http://monorepo-agent:8080/sse`.

---

## 4. Multi-agent research & writing mesh

**Problem:** You're building a content platform where an orchestrator delegates research, fact-checking, and writing to three specialist agents.

**Agent layout:**
```
                    ┌── ResearchAgent  (MCP server, port 8081)
                    │     └── brave-search, wikipedia, arxiv MCP tools
ContentOrchestrator─┼── FactCheckAgent (MCP server, port 8082)
                    │     └── search MCP + Validation Agent strict threshold
                    └── WriterAgent    (MCP server, port 8083)
                          └── style-guide MCP tool
```

**Why Cortex:** Each specialist is its own Cortex process with its own `cortex.yaml`, its own LLM (cheap for research, flagship for writing), its own storage. The orchestrator just lists them as `tool_servers`. Scale each specialist independently — a single WriterAgent instance for 10 ResearchAgent instances if that's the bottleneck.

**Deployment:** Three Docker services + one orchestrator service. See [DEPLOYMENT.md](DEPLOYMENT.md#multi-agent-deployment).

---

## 5. Data-pipeline enrichment worker

**Problem:** You ingest 100k rows/day of user-generated product descriptions. You want AI-normalised categories, extracted specs, and a quality score on each.

**Agent layout:**
```
Job queue (SQS/Celery) → Cortex worker → enriched rows → data warehouse
```

**Task types:**
- `extract_specs` — pull structured fields from raw text
- `categorise` — map to internal taxonomy
- `score_quality` — rate listing quality (depends_on: extract_specs)

**Why Cortex:** Per-user concurrency limits prevent one noisy seller from starving the queue. Redis storage backend lets you run 20 workers in parallel. Validation agent catches hallucinated specs before they hit the warehouse. Token usage accounting tells finance exactly what each row costs.

**Deployment:** Python package installed into your existing Celery workers. No new service to operate.

---

## 6. "Explain this incident" post-mortem helper

**Problem:** After an outage, an on-call engineer spends 2 hours correlating logs, metrics, and git changes to write the post-mortem.

**Agent layout:**
```
Engineer input: "Incident at 14:02 UTC" → CortexFramework → draft post-mortem
```

**Task types:**
- `fetch_logs` — MCP to Datadog/Loki
- `fetch_metrics` — MCP to Prometheus
- `fetch_deployments` — MCP to ArgoCD/GitHub Actions
- `correlate_timeline` — depends on all three, builds a minute-by-minute view
- `draft_postmortem` — depends on correlate_timeline, writes structured markdown

**Why Cortex:** Fan-out across three independent telemetry sources cuts wall-clock time from minutes to seconds. The validation agent forces the draft to include all sections (root cause, impact, action items). Session replay means the SRE team can look back at *why* the agent drew a particular conclusion.

**Deployment:** CLI tool (`cortex dev` wrapped in a Click command) that engineers run locally, or a Slack slash command backed by a Docker service.

---

## 7. Embedded AI in a SaaS product

**Problem:** You sell a project management SaaS. You want to add "AI assistant" as a feature without building a separate agent product.

**Agent layout:**
```
SaaS web app → Cortex as a Python dependency → agent responses → UI
```

**Task types:**
- `summarise_project` — read project data from your DB via a custom MCP wrapper
- `suggest_next_actions` — depends on summarise_project
- `draft_status_update` — depends on summarise_project

**Why Cortex:** `pip install cortex-agent-framework` and call `framework.run_session()` from inside your existing Django/FastAPI view. No new service, no new deployment target, no new auth story. Per-user concurrency limits ship with the framework so you don't build a throttle layer. When a customer wants a different LLM provider, flip one line of YAML.

**Deployment:** Embedded library. Cortex is just a dependency.

---

## 8. Compliance-gated enterprise agent

**Problem:** You're at a regulated company (finance, healthcare, legal). Every AI response must be logged, scored, and reviewable.

**Why Cortex fits:**
- Every session is persisted with input, output, token usage, and validation scores (SQLite or Redis backend).
- `cortex replay SESSION_ID --user-id USER_ID` reconstructs any past session exactly.
- Validation thresholds enforce a quality floor; low-scoring responses can be routed to human review.
- Learning Engine is consent-gated — only learns from sessions where users opted in.
- Credential scrubbing in logs. Input sanitisation on user inputs.
- Delta learning is human-in-the-loop — no config changes go live without `cortex delta apply`.

**Deployment:** Docker with Redis storage in your regulated cloud, behind your existing audit logging pipeline (OpenTelemetry OTLP exporter ships in the box).

---

## Pattern matching: which usage mode fits you?

| If you need… | Use this usage mode |
|---|---|
| A chat UI for end users | **Chat UI** (FastAPI + SSE streaming) |
| An agent other agents can call as a tool | **MCP Server** (`cortex publish mcp`) |
| A one-shot CLI tool for devs/ops | **CLI tool** (Click + Cortex) |
| Batch processing of a job queue | **Background worker** |
| AI feature inside an existing web app | **Embedded library** |
| Multi-tenant production service | **Docker + Redis** |
| A pre-configured agent for users to install | **Python package** (`cortex publish package`) |

Each of these maps to a specific example in [GETTING_STARTED.md](GETTING_STARTED.md#how-to-use-cortex-in-your-application).
