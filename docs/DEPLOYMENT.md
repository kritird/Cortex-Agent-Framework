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
docker run -p 8080:8080 --env-file .env my-agent:latest
```

### Production checklist

- **Storage backend**: use **Redis** (not SQLite) for multi-replica deployments.
- **Secrets**: pass API keys via `--env-file` or a secret manager, not baked into the image.
- **Concurrency limits**: set `max_concurrent_sessions` in `cortex.yaml` to match your instance size.
- **Logging**: set `CORTEX_LOG_LEVEL=INFO` (or `DEBUG` for investigation) and forward container stdout to your log aggregator.
- **OpenTelemetry**: Cortex ships an OTLP exporter — point it at your collector with standard OTEL env vars (`OTEL_EXPORTER_OTLP_ENDPOINT`, etc.).
- **Health check**: hit `/health` (if you expose one in your wrapper) to fail fast on broken configs.

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
    model: claude-sonnet-4-5
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
# Distribute the wheel
pip install dist/cortex_agent_framework-*.whl
```

Use this when:
- You want to embed Cortex in an existing Python app (Django, FastAPI, Flask).
- You want to ship a pre-configured agent to internal users.
- You don't want to run a separate service.

Once installed, you just import and call it:

```python
from cortex.framework import CortexFramework

framework = CortexFramework("cortex.yaml")
await framework.initialize()
result = await framework.run_session(user_id="u1", request="Hello")
```

No new deployment target to operate. Cortex is just a dependency.

---

## Option C: MCP server

```bash
cortex publish mcp --port 8080
```

Runs the agent as an MCP server. Any MCP client — another Cortex agent, Claude Desktop, an IDE, or a custom tool — can consume it:

```yaml
# consumer's cortex.yaml
tool_servers:
  my_specialist_agent:
    transport: sse
    url: http://host:8080/sse
```

Use this when:
- You're building a **multi-agent system**.
- You want your agent available to Claude Desktop / Cursor / VS Code without a wrapper.
- Your agent is a "specialist capability" that an orchestrator delegates to.

---

## Option D: Chat UI

```bash
cortex publish ui --config cortex.yaml
# → Cortex chat UI: http://0.0.0.0:8090
```

Serves a clean, single-page web frontend backed by your agent. Users get:

- **Text + file uploads** — files are validated against `file_input` MIME / size limits.
- **SSE-streamed responses** — status pills ("decomposing → running 3 tasks → synthesising") update live.
- **Persistent session history** — threads listed in a sidebar, backed by your existing History Store.
- **Per-user identity** — anonymous cookie (`auth.mode: none`), shared token, or HTTP Basic.

### Configuration

All settings live under the `ui` block in `cortex.yaml`:

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

These can also be configured through the **Chat UI** section in the setup wizard (`cortex setup`).

### Docker with Chat UI

```bash
cortex publish docker --with-ui --tag my-agent:latest
docker build -f Dockerfile.cortex -t my-agent:latest .
docker run -p 8090:8090 --env-file .env my-agent:latest
```

The generated Dockerfile runs `cortex publish ui` as its entrypoint and exposes port 8090.

### Tips

- **Enable history** (`history.enabled: true`) so conversations survive page reloads.
- **Use SQLite or Redis** for the storage backend — in-memory storage loses all chat history on restart.
- **Auth for public access**: if exposing to the internet, switch from `none` to `token` or `basic`.
- Host and port can be overridden on the CLI: `cortex publish ui --host 127.0.0.1 --port 9000`.

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
    url: http://localhost:8081/sse
  code_review:
    transport: sse
    url: http://localhost:8082/sse

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

### Multi-agent pitfalls

1. **Never share a SQLite file between running agents.** SQLite locks the DB; two agents pointing at the same `sqlite.path` will fail intermittently. Give each its own `storage.base_path`.
2. **Redis is safe to share** if you want centralised storage — but use a different `key_prefix` per agent so sessions don't collide.
3. **Don't run two agents from the same directory.** They'd fight over `cortex.yaml`, storage, and ports. `cd` into each agent's own folder or use `--config /abs/path/cortex.yaml`.
4. **Wizard is one-at-a-time per port.** Configure multiple agents with `cortex setup --port 7800`, `--port 7801`, etc.
5. **Pick a port allocation scheme up front.** Conventions like wizard `7799+N` and MCP `8080+N` make a mesh readable.
6. **Avoid circular tool_server references.** Agent A → Agent B → Agent A will deadlock. Keep the call graph a DAG.
7. **Kill orphaned MCP servers before restarting.** `cortex publish mcp` holds the port until the process exits — `lsof -i :8081` to find a lingering PID.
8. **Use `CORTEX_CONFIG` for sticky shells.** `export CORTEX_CONFIG=~/agents/research-agent/cortex.yaml` lets you run `cortex dev` from anywhere targeting that agent.

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
- ☐ Session timeouts set generous enough for worst-case task graphs
- ☐ `cortex dry-run` wired into CI so bad configs fail at build time
