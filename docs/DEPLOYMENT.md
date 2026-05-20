# Deployment

[← Back to README](../README.md)

Cortex ships four deployment targets out of the box: **Docker**, **Python package**, **MCP server**, and **Chat UI**. Pick based on who's calling your agent.

| Mode | Consumer | Transport | When to use |
|---|---|---|---|
| Docker | End users / services | HTTP to a running container | Production microservice, multi-tenant backend |
| Package | Python developers | `import` in-process | Embed in an existing Django/FastAPI app |
| MCP server | Other agents | MCP protocol tool call | Multi-agent composition, IDE integrations |
| Chat UI | End users (browser) | HTTP + SSE | Quick demo, internal tool, user-facing chat |

---

## Option A: Docker

```bash
cortex publish docker --tag my-agent:latest
docker build -f Dockerfile.cortex -t my-agent:latest .
docker run --rm -p 8090:8090 -e ANTHROPIC_API_KEY=your_key my-agent:latest
```

Pass `--with-ui` to generate a Dockerfile that runs the built-in Cortex Synapse chat UI on port 8090:

```bash
cortex publish docker --with-ui --tag my-agent:latest
docker build -f Dockerfile.cortex -t my-agent:latest .
docker run --rm -p 8090:8090 -e ANTHROPIC_API_KEY=your_key my-agent:latest
# open http://localhost:8090
```

### Production checklist

- **Storage backend**: use **Redis** (not SQLite) for multi-replica deployments.
- **Secrets**: pass API keys via `-e KEY=val` or a secret manager — never bake them into the image.
- **Concurrency limits**: set `max_concurrent_sessions` in `cortex.yaml` to match your instance size.
- **Logging**: set `CORTEX_LOG_LEVEL=INFO` (or `DEBUG` for investigation) and forward container stdout to your log aggregator.
- **OpenTelemetry**: Cortex ships an OTLP exporter — point it at your collector with standard OTEL env vars (`OTEL_EXPORTER_OTLP_ENDPOINT`, etc.).
- **Health check**: the UI server exposes `/health` — use it in your container orchestrator's readiness probe.

### Example: FastAPI + Docker + Redis

```yaml
# cortex.yaml
agent:
  name: ProductionAgent
  concurrency:
    max_concurrent_sessions: 100
    max_concurrent_sessions_per_user: 5

llm_access:
  default:
    provider: anthropic
    model: claude-sonnet-4-6
    api_key_env_var: ANTHROPIC_API_KEY

redis:
  enabled: true
  url: ${REDIS_URL}
  key_prefix: "cortex:prod:"
```

---

## Option B: Python package

```bash
cortex publish package --output-dir dist
pip install dist/cortex_agent_framework-*.whl
```

Use this when:
- You want to embed Cortex in an existing Python app (Django, FastAPI, Flask).
- You want to ship a pre-configured agent to internal users.
- You don't want to run a separate service.

Once installed, import and call it directly:

```python
from cortex.framework import CortexFramework

framework = CortexFramework("cortex.yaml")
await framework.initialize()
result = await framework.run_session(user_id="u1", request="Hello")
```

Then start it with:

```bash
export ANTHROPIC_API_KEY=your_key
cortex publish ui --config cortex.yaml
# open http://localhost:8090
```

No new deployment target to operate. Cortex is just a dependency.

---

## Option C: MCP server

```bash
cortex publish mcp --config cortex.yaml --port 8080
# MCP server running at http://localhost:8080/mcp
```

Runs the agent as a live aiohttp HTTP server. Any MCP client — another Cortex agent, Claude Desktop, an IDE, or a custom tool — can call it:

```yaml
# consumer's cortex.yaml
tool_servers:
  my_specialist_agent:
    transport: sse
    url: http://host:8080/mcp
```

Or call it directly via REST (convenience alias `/run`):

```bash
curl -X POST http://localhost:8080/run \
  -H 'Content-Type: application/json' \
  -d '{"input": "Summarise the latest AI news"}'
# → {"output": "..."}
```

**Interaction mode:** `cortex publish mcp` automatically sets `CORTEX_INTERACTION_MODE=rpc` so the agent never blocks on interactive clarifications — MCP clients cannot answer them.

Use this when:
- You're building a **multi-agent system**.
- You want your agent available to Claude Desktop / Cursor / VS Code without a wrapper.
- Your agent is a "specialist capability" that an orchestrator delegates to.

---

## Option D: Chat UI (Cortex Synapse)

```bash
cortex publish ui --config cortex.yaml
# Cortex chat UI: http://localhost:8090
```

Serves **Cortex Synapse** — a fully-featured single-page web frontend backed by your agent. Open `http://localhost:8090` in your browser.

### What users get

- **Text + file uploads** — files validated against `file_input` MIME / size limits; mid-session uploads also supported.
- **Live streaming** — SSE-pushed status chips ("decomposing → running 3 tasks → synthesising") update in real time.
- **Task blueprint view** — after decomposition, the full task DAG (waves, dependencies) is shown before execution begins.
- **Intent classification indicator** — shows whether the turn was routed as chat or task, with confidence.
- **Token usage display** — cumulative input/output/cache token counts per session.
- **Workspace events** — file reads, writes, and executions in WorkspaceBash are streamed as live events.
- **Persistent session history** — threads listed in a sidebar; full-text search across all sessions.
- **Artifact download** — download all output files for a session as a single ZIP.
- **HITL inline answers** — clarification questions from the agent appear inline; answer without leaving the chat.
- **Per-user identity** — anonymous cookie (`auth.mode: none`), shared token, or HTTP Basic.
- **Service launcher** — open Config Studio or Setup Wizard from inside the chat UI without a separate terminal.

### Configuration

```yaml
ui:
  enabled: true
  host: "0.0.0.0"
  port: 8090
  title: "My Agent"
  auth:
    mode: none      # none | token | basic
    # token: "s3cret"            # for mode: token
    # username: admin             # for mode: basic
    # password: changeme          # for mode: basic
```

Configure through the **Chat UI** section in the setup wizard (`cortex setup`) or by hand.

### REST API

The UI server also exposes a REST API for headless / programmatic access:

| Endpoint | Method | Description |
|---|---|---|
| `/api/session` | POST | Start a new session |
| `/api/session/{id}/events` | GET | SSE stream of events |
| `/api/session/{id}/clarify` | POST | Answer a HITL clarification |
| `/api/session/{id}/upload` | POST | Upload additional files mid-session |
| `/api/history` | GET | List session history |
| `/api/history/search?q=...` | GET | Full-text search over sessions |
| `/api/history/{sid}` | GET | Session detail |
| `/api/history/{sid}/files/{task}/{name}` | GET | Download a task output file |
| `/api/history/{sid}/artifacts/zip` | GET | Download all outputs as ZIP |
| `/api/history/{sid}` | DELETE | Delete a session |
| `/api/ants/{ant_id}` | DELETE | Cancel a running ant task |
| `/api/runtime/delta/action` | POST | Promote or discard a learning delta |
| `/api/services/{service}/launch` | POST | Ensure config-ui or wizard is running |

### Docker with Chat UI

```bash
cortex publish docker --with-ui --tag my-agent:latest
docker build -f Dockerfile.cortex -t my-agent:latest .
docker run --rm -p 8090:8090 -e ANTHROPIC_API_KEY=your_key my-agent:latest
# open http://localhost:8090
```

The generated Dockerfile runs `cortex publish ui` as its entrypoint and exposes port 8090.

### Tips

- **Enable history** (`history.enabled: true`) so conversations survive page reloads.
- **Use SQLite or Redis** — in-memory storage loses all chat history on restart.
- **Auth for public access**: switch from `none` to `token` or `basic` before exposing to the internet.
- Host and port can be overridden on the CLI: `cortex publish ui --host 127.0.0.1 --port 9000`.
- The printed URL always shows `localhost` even when the server binds to `0.0.0.0`.

---

## Multi-agent deployment

Cortex is designed for multi-agent composition. Any number of Cortex agents can run on one host or across a cluster — each just needs its own directory, its own `cortex.yaml`, and its own ports.

### What's shared vs. per-agent

| Thing | Default | Per-agent override |
|---|---|---|
| Config file | `./cortex.yaml` | `--config PATH` or `CORTEX_CONFIG` env var |
| Wizard port | `7799` | `cortex setup --port 7800` |
| MCP publish port | `8080` | `cortex publish mcp --port 8081` |
| Chat UI port | `8090` | Set `ui.port` or `cortex publish ui --port 9000` |
| Storage base_path | `./cortex_storage` | Set `storage.base_path` in each `cortex.yaml` |
| SQLite DB path | `./cortex_storage/cortex.db` | Set `sqlite.path` — **never share across running agents** |

### Recommended layout

```
~/agents/
├── research-agent/
│   ├── cortex.yaml          # MCP port 8081, storage ./storage
│   └── storage/
├── code-review-agent/
│   ├── cortex.yaml          # MCP port 8082, storage ./storage
│   └── storage/
└── orchestrator/
    ├── cortex.yaml          # references 8081 + 8082 as tool_servers
    └── storage/
```

### Step-by-step: 3-agent mesh

**1. Create each sub-agent** (each in its own directory, with its own wizard port):

```bash
mkdir -p ~/agents/research-agent && cd ~/agents/research-agent
cortex setup --port 7799

mkdir -p ~/agents/code-review-agent && cd ~/agents/code-review-agent
cortex setup --port 7800

mkdir -p ~/agents/orchestrator && cd ~/agents/orchestrator
cortex setup --port 7801
```

**2. In the orchestrator's `cortex.yaml`**, reference the sub-agents as tool servers and add matching task types:

```yaml
tool_servers:
  research:
    transport: sse
    url: http://localhost:8081/mcp
  code_review:
    transport: sse
    url: http://localhost:8082/mcp

task_types:
  - name: research
    description: Delegate web research to ResearchAgent
    capability_hint: web_search
    output_format: md
  - name: review_code
    description: Delegate code review to CodeReviewAgent
    capability_hint: auto
    output_format: md
  - name: write_report
    description: Synthesise findings into a final report
    capability_hint: document_generation
    depends_on: [research, review_code]
```

**3. Run all three** (separate terminals, or systemd / supervisor / pm2 units):

```bash
# Terminal 1
cd ~/agents/research-agent    && cortex publish mcp --port 8081

# Terminal 2
cd ~/agents/code-review-agent && cortex publish mcp --port 8082

# Terminal 3
cd ~/agents/orchestrator      && cortex dev
```

**4. Drive the orchestrator** from your application code:

```python
result = await framework.run_session(
    user_id="dev_1",
    request="Research vector DB benchmarks and review our benchmark script",
)
```

The orchestrator fans out `research` and `review_code` in parallel to the two sub-agents over MCP, waits for both, then runs `write_report`.

### ToolForge: dynamic capability creation at runtime

When `tool_forge`, `ant_colony`, and `code_sandbox` are all enabled, an orchestrator can instruct the decomposer to generate a **new MCP server from code** during a session:

```yaml
ant_colony:
  enabled: true
tool_forge:
  enabled: true
  persist_by_default: false    # session-scoped by default; set true to survive restarts
  spawn_timeout_seconds: 30
code_sandbox:
  enabled: true
```

A `forge_mcp` task generates a FastMCP server script, writes it to `cortex_storage/ants/<task_name>/server.py`, and registers it at the wave boundary — dependent tasks in later waves can use the new capability immediately. Forged servers are supervised by Ant Colony like any hand-hatched ant.

### Multi-agent pitfalls

1. **Never share a SQLite file between running agents.** SQLite locks the DB; two agents pointing at the same `sqlite.path` will fail intermittently. Give each its own `storage.base_path`.
2. **Redis is safe to share** — use a different `key_prefix` per agent so sessions don't collide.
3. **Don't run two agents from the same directory.** They'd fight over `cortex.yaml`, storage, and ports.
4. **Wizard is one-at-a-time per port.** Configure multiple agents with `cortex setup --port 7800`, `--port 7801`, etc.
5. **Pick a port allocation scheme up front.** Conventions like wizard `7799+N` and MCP `8080+N` make a mesh readable.
6. **Avoid circular tool_server references.** Agent A → Agent B → Agent A will deadlock. Keep the call graph a DAG.
7. **Kill orphaned MCP servers before restarting.** `cortex publish mcp` holds the port until the process exits — `lsof -i :8081` to find a lingering PID.
8. **Use `CORTEX_CONFIG` for sticky shells.** `export CORTEX_CONFIG=~/agents/research-agent/cortex.yaml` lets you run `cortex dev` from anywhere targeting that agent.
9. **MCP endpoint is `/mcp`, not `/sse`.** Consumer `tool_servers` entries should point at `http://host:PORT/mcp`.

### Scaling a multi-agent mesh

| Need | How |
|---|---|
| One specialist is the bottleneck | Run multiple replicas of that sub-agent behind a load balancer |
| Agents span hosts | Point `tool_servers.*.url` at the remote hostname instead of `localhost` |
| Shared session store across replicas | Use Redis with a consistent `key_prefix` |
| Zero-downtime deploys | Publish each agent as a Docker image and roll them independently |

---

## Production checklist (any target)

- ☐ Storage backend set to **Redis** if you run more than one process
- ☐ API keys injected via env vars, never in `cortex.yaml`
- ☐ `validation.threshold` set appropriately for your use case
- ☐ `learning.auto_apply_confidence: null` (human-gated) unless you've measured the confidence model
- ☐ `CORTEX_LOG_LEVEL=INFO` in production, `DEBUG` only for investigation
- ☐ OpenTelemetry OTLP endpoint configured if you want traces/metrics
- ☐ Per-user concurrency caps set to prevent one user from starving others
- ☐ `max_parallel_llm_calls` left unset (auto-derives from provider+model and self-tunes via `AdaptiveLLMGate`) unless you need to pin it for a hard-rate-limited API
- ☐ Session timeouts set generous enough for worst-case task graphs
- ☐ `cortex dry-run` wired into CI so bad configs fail at build time
- ☐ Chat UI auth mode set to `token` or `basic` if exposed beyond localhost
