# Getting Started

[← Back to README](../README.md)

A step-by-step guide to building, configuring, and deploying AI agents with Cortex.

---

## What is Cortex?

Cortex is a **Python library** that gives your application an AI agent capable of decomposing complex requests into parallel tasks, calling external tools, and synthesising results — all driven by a single `cortex.yaml` configuration file.

You don't run Cortex on its own. You **wrap it** in your application — a web API, a CLI tool, a background worker, or an MCP server — and call its `run_session()` method whenever you need an AI-powered response.

```
Your Application
  └── CortexFramework("cortex.yaml")
        ├── Decomposes request into task graph
        ├── Fans out tasks in parallel to MCP tool servers
        ├── Synthesises results
        ├── Validates response quality
        └── Streams events back to your app via event_queue
```

---

## Quick Start (5 minutes)

### 1. Install

```bash
# From PyPI
pip install cortex-agent-framework

# Or from source
git clone <repo-url>
cd cortex-agent-framework
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

Verify the install:

```bash
cortex --help     # should list: setup, dev, dry-run, publish, spec, replay, delta, migrate
```

### 2. Hello World (no external tools)

Before wiring up MCP tool servers, you can run a fully working agent using only the LLM. Create `cortex.yaml`:

```yaml
agent:
  name: HelloAgent
  description: A minimal Cortex agent that only uses LLM synthesis

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

Then:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
cortex dry-run "Explain gradient descent in two sentences"
cortex dev
```

No MCP servers, no Docker, no extra setup — this is the smallest config that runs. Add `tool_servers` and more `task_types` once this works.

### 3. Run the Setup Wizard

```bash
cortex setup
```

This opens an interactive browser-based wizard at `http://localhost:7799` that walks you through:

| Step | What you configure |
|---|---|
| Agent Identity | Name and description |
| LLM Provider | Model, API key env var (supports Anthropic, OpenAI, Gemini, Grok, Mistral, DeepSeek, Bedrock, Azure) |
| Tool Servers | MCP integrations for external capabilities |
| Task Types | What your agent can do (web search, code execution, document generation, etc.) |
| Storage & Options | Persistence backend, session timeouts, concurrency limits, validation, history, learning |
| Publish Mode | How you want to deploy (Docker, Python package, MCP server) |

The wizard saves a validated `cortex.yaml` in your project root.

> **Re-running the wizard**: If `cortex.yaml` already exists, the wizard loads your current settings. Fields that could break existing data (agent name, storage backend with data) are locked. Everything else is editable.

### 4. Validate

```bash
cortex dry-run "Summarise the latest news on quantum computing"
```

This validates your config and compiles the task graph **without making any LLM calls**. Use it to catch config errors before spending API credits.

Expected output on success:

```
✓ Config loaded: cortex.yaml
✓ LLM provider reachable: anthropic / claude-sonnet-4-5
✓ Task graph compiled: 2 tasks, max depth 2
  ├─ web_research        (capability: web_search)
  └─ analysis            (depends on: web_research)
✓ No cycles detected
✓ Dry run complete — 0 LLM calls made
```

If any tool server is unreachable or a `depends_on` points at a missing task, dry-run fails here instead of mid-session.

### 5. Run in Dev Mode

```bash
cortex dev --watch
```

Starts Cortex with hot-reload — edit `cortex.yaml` and changes apply instantly without restarting. You'll see:

```
[cortex] Initialising framework from cortex.yaml
[cortex] LLM: anthropic claude-sonnet-4-5
[cortex] Tool servers: brave_search (sse), filesystem (stdio)
[cortex] Watching cortex.yaml for changes...
[cortex] Ready. Send requests via framework.run_session() or the HTTP adapter.
```

---

## How to Use Cortex in Your Application

The core integration is always the same three lines:

```python
from cortex.framework import CortexFramework

framework = CortexFramework("cortex.yaml")
await framework.initialize()

result = await framework.run_session(
    user_id="user_123",
    request="Analyse Q3 revenue trends",
    event_queue=asyncio.Queue(),   # receives streaming events
)
print(result.response)
```

What changes is **what wraps those lines**. Below are all the ways developers use Cortex, with complete working examples.

---

### Usage 1: Conversational Chat UI

**Best for**: Customer-facing apps, internal tools, support agents, dashboards with AI chat.

```
User ──► Browser ──► Your API (FastAPI) ──► CortexFramework
              ◄── SSE stream ◄─────────────── event_queue
```

**Step 1** — Build the API layer:

```python
# app.py
import asyncio
import json
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from cortex.framework import CortexFramework
from cortex.streaming.status_events import (
    EventType, StatusEvent, ResultEvent, ClarificationEvent,
)

app = FastAPI()
framework = CortexFramework("cortex.yaml")

@app.on_event("startup")
async def startup():
    await framework.initialize()

@app.on_event("shutdown")
async def shutdown():
    await framework.shutdown()

@app.post("/chat")
async def chat(body: dict):
    queue = asyncio.Queue()
    asyncio.create_task(
        framework.run_session(
            user_id=body["user_id"],
            request=body["message"],
            event_queue=queue,
        )
    )

    async def stream():
        while True:
            event = await queue.get()
            payload = {
                "type": event.event_type.value,
                "session_id": event.session_id,
            }
            if isinstance(event, ResultEvent):
                payload["content"] = event.content
                payload["partial"] = event.partial
            elif isinstance(event, StatusEvent):
                payload["message"] = event.message
            elif isinstance(event, ClarificationEvent):
                payload["question"] = event.question
                payload["clarification_id"] = event.clarification_id
                payload["options"] = event.options
            yield f"data: {json.dumps(payload)}\n\n"
            if event.event_type in (EventType.SESSION_END, EventType.ERROR):
                break

    return StreamingResponse(stream(), media_type="text/event-stream")

# Handle clarification answers (agent asked a follow-up question)
@app.post("/clarify")
async def clarify(body: dict):
    resolved = framework.resolve_evolution_consent(
        body["clarification_id"], body["answer"]
    )
    return {"resolved": resolved}

# Resume timed-out sessions
@app.get("/sessions/{user_id}/resumable")
async def resumable(user_id: str):
    return await framework.get_resumable_sessions(user_id)
```

**Step 2** — Build a frontend that:

- Sends messages to `POST /chat`
- Reads the SSE stream for real-time events
- Handles `CLARIFICATION` events (show follow-up questions, post answers to `/clarify`)
- Renders `RESULT` events as the agent's response

**Event lifecycle in the UI**:

| Event | What the UI does |
|---|---|
| `session_start` | Show "thinking..." indicator |
| `task_start` | Show progress ("Searching web...", "Analysing data...") |
| `task_complete` | Update progress bar |
| `status` | Display status messages |
| `clarification` | Render a follow-up question with options |
| `result` (partial) | Stream text into the chat bubble |
| `result` (final) | Display complete response |
| `error` | Show error state |
| `session_end` | Re-enable input |

---

### Usage 2: MCP Server (Agent-to-Agent)

**Best for**: Agent composition, building specialised sub-agents, multi-agent architectures.

```
Parent Agent ──► MCP protocol ──► Your Agent (as tool server)
                                     └── CortexFramework
```

This is the most powerful pattern. Your agent **becomes a tool** that other agents can call.

**Step 1** — Build a specialised agent with its own `cortex.yaml`:

```yaml
# research-agent/cortex.yaml
agent:
  name: ResearchAgent
  description: Searches the web and summarises findings

llm_access:
  default:
    provider: anthropic
    model: claude-sonnet-4-20250514
    api_key_env_var: ANTHROPIC_API_KEY
    max_tokens: 4096

tool_servers:
  brave_search:
    transport: sse
    url: http://localhost:8051/sse

task_types:
  - name: web_research
    description: Search the web for current information
    output_format: md
    capability_hint: web_search

storage:
  base_path: ./research_storage
```

**Step 2** — Publish it as an MCP server:

```bash
cortex publish mcp --port 8081
```

**Step 3** — Connect it from a parent agent's config. You need **both** a `tool_servers` entry (so the parent can reach the child) **and** a `task_types` entry that references it (so the decomposer knows it exists):

```yaml
# parent-agent/cortex.yaml
agent:
  name: OrchestratorAgent
  description: Delegates research, code review, and writing to specialised sub-agents

llm_access:
  default:
    provider: anthropic
    model: claude-sonnet-4-5
    api_key_env_var: ANTHROPIC_API_KEY

tool_servers:
  research:
    url: http://localhost:8081/sse
    transport: sse
  code_review:
    url: http://localhost:8082/sse
    transport: sse
  writing:
    url: http://localhost:8083/sse
    transport: sse

task_types:
  - name: research
    description: Delegate web research to the ResearchAgent sub-agent
    output_format: md
    capability_hint: web_search         # routes to the `research` tool_server

  - name: review_code
    description: Delegate code review to the CodeReviewAgent sub-agent
    output_format: md
    capability_hint: auto               # decomposer picks code_review server

  - name: write_report
    description: Generate a final written report from research + review inputs
    output_format: md
    capability_hint: document_generation
    depends_on: [research, review_code] # fan-in: waits for both
```

**Step 4** — Drive it:

```python
result = await framework.run_session(
    user_id="dev_1",
    request="Research competitor pricing for vector DBs, review our benchmark code, and write a report",
    event_queue=asyncio.Queue(),
)
```

The parent decomposes the request into three tasks, fans out `research` and `review_code` in parallel to the two MCP sub-agents, waits for both, then runs `write_report` to synthesise. Each sub-agent is independently deployable and has its own `cortex.yaml`.

> **Common pitfall**: adding a `tool_server` without a matching `task_type` is a silent no-op — the decomposer only generates tasks it has types for. If a sub-agent never gets called, check that a `task_type` references it via `capability_hint`.

**Composing agent hierarchies**:

```
                    ┌── ResearchAgent (port 8081)
                    │     └── brave_search tool
OrchestratorAgent ──┼── CodeReviewAgent (port 8082)
                    │     └── github tools
                    └── WritingAgent (port 8083)
                          └── doc generation tools
```

Each agent is independent, has its own config, and can be deployed/scaled separately.

---

### Usage 3: CLI Tool

**Best for**: Developer tools, ops automation, data pipelines, scripts.

```
Terminal ──► Your CLI (Click/Typer) ──► CortexFramework
```

```python
# cli_agent.py
import asyncio
import click
from cortex.framework import CortexFramework
from cortex.streaming.status_events import EventType, ResultEvent, StatusEvent

@click.command()
@click.argument("request")
@click.option("--config", default="cortex.yaml")
def run(request, config):
    """Run a one-shot agent request from the command line."""
    asyncio.run(_run(request, config))

async def _run(request, config):
    fw = CortexFramework(config)
    await fw.initialize()
    q = asyncio.Queue()

    # Print events as they arrive
    async def print_events():
        while True:
            event = await q.get()
            if isinstance(event, StatusEvent):
                click.echo(f"  [{event.event_type.value}] {event.message}")
            elif isinstance(event, ResultEvent) and not event.partial:
                click.echo(f"\n{event.content}")
            if event.event_type in (EventType.SESSION_END, EventType.ERROR):
                break

    event_task = asyncio.create_task(print_events())
    result = await fw.run_session("cli_user", request, q)
    await event_task
    await fw.shutdown()

if __name__ == "__main__":
    run()
```

```bash
python cli_agent.py "Analyse the error logs from the last 24 hours"
```

---

### Usage 4: Background Worker

**Best for**: Batch processing, scheduled jobs, email triage, automated report generation.

```
Job Queue (Celery / SQS / Redis) ──► Worker ──► CortexFramework
                                   ◄── Result stored to DB
```

```python
# worker.py
import asyncio
from cortex.framework import CortexFramework

framework = CortexFramework("cortex.yaml")

async def init():
    await framework.initialize()

async def process_job(job: dict) -> str:
    """Called by your job queue for each incoming job."""
    q = asyncio.Queue()
    result = await framework.run_session(
        user_id=job["user_id"],
        request=job["prompt"],
        event_queue=q,
    )
    return result.response

# Example: process a batch of documents
async def batch_process(documents: list):
    await init()
    for doc in documents:
        summary = await process_job({
            "user_id": "batch_worker",
            "prompt": f"Summarise this document:\n\n{doc['content']}",
        })
        save_to_database(doc["id"], summary)
    await framework.shutdown()
```

---

### Usage 5: Embedded in an Existing Application

**Best for**: Adding AI capabilities to an app that already exists (Django, Flask, FastAPI).

```python
# Inside your existing Django view or FastAPI route
from cortex.framework import CortexFramework

framework = CortexFramework("cortex.yaml")

# Call this once at app startup
# await framework.initialize()

async def handle_support_ticket(ticket):
    q = asyncio.Queue()
    result = await framework.run_session(
        user_id=ticket.author_id,
        request=f"Triage and categorise this support ticket:\n\n{ticket.body}",
        event_queue=q,
    )
    ticket.ai_triage = result.response
    ticket.ai_score = result.validation_report.composite_score
    ticket.save()
```

No new app to build. Cortex is just another dependency.

---

## Running Multiple Cortex Agents on One Machine

Cortex is designed for multi-agent composition — you can run any number of agents side-by-side on one machine. There's no "one Cortex per host" limit; the defaults (filename `cortex.yaml`, wizard port `7799`, MCP port `8080`, storage `./cortex_storage`) just need to be overridden per agent.

### What's shared vs. per-agent

| Thing | Default | How to override per agent |
|---|---|---|
| Config file | `./cortex.yaml` in CWD | Every CLI command takes `--config PATH`, or set `CORTEX_CONFIG` env var |
| Wizard port | `7799` | `cortex setup --port 7800` |
| MCP publish port | `8080` | `cortex publish mcp --port 8081` |
| Storage base_path | `./cortex_storage` | Set `storage.base_path` in each `cortex.yaml` |
| SQLite DB path | `./cortex_storage/cortex.db` | Set `sqlite.path` in each `cortex.yaml` |

### Recommended directory layout

Give each agent its own directory with its own config and storage. Never run two agents from the same directory.

```
~/agents/
├── research-agent/
│   ├── cortex.yaml           # MCP port 8081, storage ./storage
│   └── storage/
├── code-review-agent/
│   ├── cortex.yaml           # MCP port 8082, storage ./storage
│   └── storage/
└── orchestrator/
    ├── cortex.yaml           # references 8081 + 8082 as tool_servers
    └── storage/
```

### Step-by-step: build a 3-agent mesh

**Step 1** — Create the research sub-agent:

```bash
mkdir -p ~/agents/research-agent && cd ~/agents/research-agent
cortex setup --port 7799        # wizard configures this one
```

In the wizard, set:
- Agent name: `ResearchAgent`
- Storage base_path: `./storage`
- SQLite path: `./storage/cortex.db`
- Add your web-search MCP tool server + a `web_research` task type

**Step 2** — Create the code-review sub-agent (use a different wizard port so you can run both wizards in parallel if needed):

```bash
mkdir -p ~/agents/code-review-agent && cd ~/agents/code-review-agent
cortex setup --port 7800
```

- Agent name: `CodeReviewAgent`
- Storage base_path: `./storage`
- Add a GitHub/filesystem MCP tool server + a `review_code` task type

**Step 3** — Create the orchestrator that fans out to both:

```bash
mkdir -p ~/agents/orchestrator && cd ~/agents/orchestrator
cortex setup --port 7801
```

Edit `~/agents/orchestrator/cortex.yaml` so `tool_servers` references the two sub-agents (both running as MCP servers) and `task_types` has entries that route to them — see the MCP example in **Usage 2** above for the exact shape.

```yaml
tool_servers:
  research:
    url: http://localhost:8081/sse
    transport: sse
  code_review:
    url: http://localhost:8082/sse
    transport: sse
```

**Step 4** — Run all three in separate terminals (or as systemd/supervisor/pm2 units):

```bash
# Terminal 1
cd ~/agents/research-agent    && cortex publish mcp --port 8081

# Terminal 2
cd ~/agents/code-review-agent && cortex publish mcp --port 8082

# Terminal 3
cd ~/agents/orchestrator      && cortex dev
```

**Step 5** — Drive the orchestrator from your app (or another `cortex dev` REPL):

```python
result = await framework.run_session(
    user_id="dev_1",
    request="Research the latest vector DB benchmarks and review our benchmark script at ./bench.py",
    event_queue=asyncio.Queue(),
)
```

The orchestrator decomposes the request, fans out to both sub-agents in parallel over MCP, and synthesises the combined result.

### Things to watch out for

1. **Never share a SQLite file between running agents.** SQLite locks the DB file, so two agents pointing at the same `sqlite.path` will intermittently fail writes. Give each agent its own `sqlite.path` under its own `storage.base_path`.
2. **Redis is safe to share** across agents if you want centralised storage — but use a different key prefix per agent in the `redis` config block so sessions don't collide.
3. **Don't run two agents from the same directory.** Both would load the same `cortex.yaml`, write to the same storage, and fight over the same ports. Always `cd` into the agent's own folder (or pass `--config /abs/path/cortex.yaml` explicitly).
4. **Wizard is one-at-a-time per port.** If you're configuring multiple agents, use `cortex setup --port 7800`, `--port 7801`, etc., so wizards don't collide.
5. **Pick a port allocation scheme up front.** A simple convention like wizard `7799 + N` and MCP `8080 + N` keeps the mesh readable. Write the mapping into each agent's `cortex.yaml` comments so it's discoverable.
6. **Avoid circular tool_server references.** Agent A referencing Agent B as a tool server which references A back will deadlock decomposition. Keep the call graph a DAG.
7. **Kill orphaned MCP servers before restarting.** `cortex publish mcp` binds the port until the process exits — if a previous run is still up, the next one will fail with `address already in use`. `lsof -i :8081` to find the PID.
8. **Set `CORTEX_CONFIG` in long-lived shells** if you work on one specific agent a lot: `export CORTEX_CONFIG=~/agents/research-agent/cortex.yaml`. Then `cortex dev` / `cortex dry-run` from anywhere will target it without `--config`.

---

## Deployment

### Option A: Docker

```bash
cortex publish docker --tag my-agent:latest
docker build -f Dockerfile.cortex -t my-agent:latest .
docker run -p 8080:8080 --env-file .env my-agent:latest
```

### Option B: Python Package

```bash
cortex publish package --output-dir dist
# Distribute the .whl file
pip install dist/*.whl
cortex dev --config cortex.yaml
```

### Option C: MCP Server

```bash
cortex publish mcp --port 8080
# Other agents connect via:
#   tool_servers:
#     my_agent:
#       url: http://host:8080/sse
#       transport: sse
```

---

## Configuration at a Glance

Everything lives in `cortex.yaml`. Here is a fully annotated example:

```yaml
# ── Agent identity ──
agent:
  name: MyAgent
  description: A helpful AI assistant
  time:
    default_max_wait_seconds: 120     # Session timeout
    default_task_timeout_seconds: 40  # Per-task timeout
  concurrency:
    max_concurrent_sessions: 50       # Global session cap
    max_concurrent_sessions_per_user: 3
    max_parallel_tasks: 5             # Parallel tasks per session
    max_tasks_per_session: 20

# ── LLM provider ──
llm_access:
  default:
    provider: anthropic               # anthropic | openai | gemini | grok | mistral | deepseek | bedrock | azure_ai
    model: claude-sonnet-4-20250514
    api_key_env_var: ANTHROPIC_API_KEY
    max_tokens: 4096
    temperature: 1.0

# ── External tools via MCP ──
tool_servers:
  brave_search:
    transport: sse
    url: http://localhost:8051/sse
  filesystem:
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp/workspace"]

# ── Task types ──
task_types:
  - name: web_research
    description: Search the web for current information
    output_format: md                  # text | md | json | file | html | csv | code
    capability_hint: web_search        # auto | llm_synthesis | web_search | bash | code_exec | document_generation | image_generation
    timeout_seconds: 60

  - name: analysis
    description: Analyse data and produce structured insights
    output_format: json
    capability_hint: llm_synthesis
    depends_on: [web_research]         # Runs after web_research completes

# ── Storage ──
storage:
  base_path: ./cortex_storage

sqlite:                                # Or redis for distributed deployments
  enabled: true
  path: ./cortex_storage/cortex.db
  wal_mode: true

# ── Optional features ──
validation:
  threshold: 0.75                      # Min quality score (floor: 0.60)

history:
  enabled: true
  retention_days: 90

learning:
  consent_enabled: true                # Auto-discover new task types from usage
```

---

## CLI Reference

| Command | What it does |
|---|---|
| `cortex setup` | Interactive browser wizard to generate `cortex.yaml` |
| `cortex dev --watch` | Dev mode with hot-reload on config changes |
| `cortex dry-run "query"` | Validate config and task graph without LLM calls |
| `cortex publish docker` | Generate `Dockerfile.cortex` |
| `cortex publish package` | Build a distributable `.whl` |
| `cortex publish mcp --port 8080` | Expose agent as an MCP tool server |
| `cortex spec --format json` | Generate capability manifest |
| `cortex replay SESSION_ID --user-id USER_ID` | Replay a historical session |
| `cortex delta review` | Review auto-discovered task type proposals |
| `cortex delta apply --min-confidence high` | Apply confirmed proposals to config |
| `cortex delta rollback` | Restore previous config from backup |
| `cortex migrate` | Validate config schema compatibility |

---

## Session Result

Every call to `run_session()` returns a `SessionResult`:

```python
result = await framework.run_session(user_id, request, event_queue)

result.session_id          # Unique session identifier
result.response            # Final synthesised response (string)
result.validation_report   # Quality scores (intent_match, completeness, coherence)
result.task_completion     # Which tasks succeeded/failed/timed out
result.token_usage         # Token counts by role (decomposition, execution, synthesis, validation)
result.duration_seconds    # Wall-clock time
result.error               # Error message if session failed (None on success)
```

---

## Streaming Events

Cortex streams events through the `event_queue` as work progresses. Three event types:

```python
from cortex.streaming.status_events import StatusEvent, ResultEvent, ClarificationEvent

# StatusEvent — progress updates
event.message       # "Executing task: web_research"
event.session_id
event.event_type    # EventType.STATUS | TASK_START | TASK_COMPLETE | SESSION_START | SESSION_END | ERROR

# ResultEvent — agent response (partial or final)
event.content       # The response text
event.partial       # True if streaming, False when complete
event.validation_score

# ClarificationEvent — agent needs more information
event.question          # "Which time period should I analyse?"
event.clarification_id  # Pass back to resolve_evolution_consent()
event.options           # ["Last 7 days", "Last 30 days", "Last quarter"]
```

---

## Supported LLM Providers

| Provider | Config value | Default env var | Example models |
|---|---|---|---|
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | claude-sonnet-4, claude-opus-4, claude-haiku-4.5 |
| OpenAI | `openai` | `OPENAI_API_KEY` | gpt-4o, gpt-4o-mini, o3-mini |
| Google Gemini | `gemini` | `GEMINI_API_KEY` | gemini-2.5-pro, gemini-2.5-flash |
| xAI Grok | `grok` | `XAI_API_KEY` | grok-3, grok-3-mini |
| Mistral | `mistral` | `MISTRAL_API_KEY` | mistral-large-latest |
| DeepSeek | `deepseek` | `DEEPSEEK_API_KEY` | deepseek-chat, deepseek-reasoner |
| AWS Bedrock | `bedrock` | `AWS_ACCESS_KEY_ID` | anthropic.claude-sonnet-4-* |
| Azure AI | `azure_ai` | `AZURE_API_KEY` | claude-sonnet-4 (via Azure) |
| Anthropic Proxy | `anthropic_compatible` | `ANTHROPIC_API_KEY` | Any (set `base_url`) |
| Custom | `custom` | — | Provide `function` dotted path |

---

## Architecture

```
User Request
     │
     ▼
[Primary Agent]  ──── decomposes into task graph ────►  [Task A]  [Task B]
                                                             │         │
                                                        [MCP Agent] [MCP Agent]
                                                             │         │
                                                        (tool calls) (tool calls)
                                                             │         │
                                                        [Task C depends on A + B]
                                                             │
                                                        [MCP Agent]
                                                             │
                                                    [Primary Agent synthesises]
                                                             │
                                                    [Validation Agent scores]
                                                             │
                                                    [Learning Engine observes]
                                                             │
                                                       Final Response
```

| Component | Role |
|---|---|
| **Primary Agent** | Decomposes requests into a task graph, synthesises final response |
| **Generic MCP Agent** | Executes individual tasks with access to MCP tool servers |
| **Task Graph Compiler** | Validates dependencies, detects cycles, computes execution order |
| **Capability Scout** | Pre-decomposition tool discovery so the agent knows what's available |
| **Validation Agent** | Scores responses on intent match, completeness, and coherence |
| **Learning Engine** | Observes patterns and proposes new task types (human-in-the-loop review) |
| **Session Manager** | Concurrency limits, per-user caps, session resume after timeout |
| **Signal Registry** | Coordinates async completion across parallel tasks |

---

## Usage Mode Summary

| Mode | Who calls it | Best for |
|---|---|---|
| **Chat UI** | Human via browser | Customer-facing apps, dashboards, internal tools |
| **MCP Server** | Another agent via MCP | Agent composition, specialised sub-agents |
| **CLI Tool** | Developer in terminal | Dev tools, ops automation, scripts |
| **Background Worker** | Job queue (Celery/SQS) | Batch processing, scheduled reports |
| **Embedded Library** | Your existing app | Adding AI to Django/Flask/FastAPI apps |
| **Docker Microservice** | Other services via HTTP | Production, cloud, CI/CD |
| **Python Package** | End users via pip | Distributing pre-configured agents |

---

## Testing

```bash
# Unit tests (no API key required)
pytest tests/ -v -k "not integration"

# Integration tests (requires API key)
ANTHROPIC_API_KEY=sk-... pytest tests/ -v

# With coverage
pytest tests/ --cov=cortex --cov-report=html
```

Cortex ships with test utilities:

```python
from cortex.testing import MockLLMClient, make_test_config

cfg = make_test_config(agent_name="TestAgent", task_types=["web_search", "summarise"])
mock_llm = MockLLMClient(responses={"default": "Mock response"})
```

---

## Environment Variables

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic provider API key |
| `OPENAI_API_KEY` | OpenAI provider API key |
| `GEMINI_API_KEY` | Google Gemini provider API key |
| `XAI_API_KEY` | xAI Grok provider API key |
| `MISTRAL_API_KEY` | Mistral AI provider API key |
| `DEEPSEEK_API_KEY` | DeepSeek provider API key |
| `AWS_DEFAULT_REGION` | AWS region for Bedrock |
| `AZURE_AI_API_KEY` | Azure AI provider API key |
| `CORTEX_CONFIG` | Override default config path |
| `CORTEX_LOG_LEVEL` | Logging level (DEBUG, INFO, WARNING, ERROR) |

---

## License

MIT License. See [LICENSE](LICENSE) for details.
