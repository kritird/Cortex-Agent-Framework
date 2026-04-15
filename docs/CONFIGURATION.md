# Configuration Reference

[← Back to README](../README.md)

Every aspect of Cortex is driven by `cortex.yaml`. This page is the authoritative reference for every field.

## Top-level structure

```yaml
agent:          # Agent identity, concurrency, timeouts
llm_access:     # LLM provider routing
task_types:     # Vocabulary of work the agent can do
tool_servers:   # MCP tool server connections
storage:        # Persistence configuration
sqlite:         # (optional) SQLite backend settings
redis:          # (optional) Redis backend settings
history:        # (optional) Session history settings
validation:     # (optional) Quality validation settings
learning:       # (optional) Delta learning settings
```

---

## `agent`

```yaml
agent:
  name: MyAgent                         # Required. Display name, locked after first run.
  description: A helpful AI assistant   # Required.
  system_prompt_extra: |                # Optional. Appended to system prompt.
    Always respond in British English.
  time:
    default_max_wait_seconds: 120       # Session-level timeout
    default_task_timeout_seconds: 40    # Per-task timeout
  concurrency:
    max_concurrent_sessions: 50         # Global session cap
    max_concurrent_sessions_per_user: 3 # Per-user session cap
    max_parallel_tasks: 5               # Tasks running simultaneously per session
    max_tasks_per_session: 20           # Total tasks allowed in a single session
```

---

## `llm_access`

```yaml
llm_access:
  default:
    provider: anthropic                 # See providers table below
    model: claude-sonnet-4-5
    api_key_env_var: ANTHROPIC_API_KEY
    max_tokens: 4096
    temperature: 1.0
    thinking_budget_tokens: 0           # Extended thinking (Anthropic only, 0 = off)
    base_url: null                      # For proxies / gateways

  # Optional per-task overrides
  task_overrides:
    heavy_analysis:
      model: claude-opus-4-5
      max_tokens: 8192
      thinking_budget_tokens: 5000
```

### Supported providers

| Provider | Value | Default env var | Example models |
|---|---|---|---|
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | claude-sonnet-4-5, claude-opus-4-6, claude-haiku-4-5 |
| OpenAI | `openai` | `OPENAI_API_KEY` | gpt-4o, gpt-4o-mini, o3-mini |
| Google Gemini | `gemini` | `GEMINI_API_KEY` | gemini-2.5-pro, gemini-2.5-flash |
| xAI Grok | `grok` | `XAI_API_KEY` | grok-3, grok-3-mini |
| Mistral | `mistral` | `MISTRAL_API_KEY` | mistral-large-latest |
| DeepSeek | `deepseek` | `DEEPSEEK_API_KEY` | deepseek-chat, deepseek-reasoner |
| AWS Bedrock | `bedrock` | AWS credentials | anthropic.claude-sonnet-4-* |
| Azure AI | `azure_ai` | `AZURE_AI_API_KEY` | claude-sonnet-4 via Azure |
| Anthropic proxy | `anthropic_compatible` | `ANTHROPIC_API_KEY` | any — set `base_url` |
| Custom | `custom` | — | Provide `function` dotted path |

---

## `task_types`

The vocabulary of work the agent can perform.

```yaml
task_types:
  - name: web_research                  # Unique ID used in depends_on
    description: Search the web for current info on a topic
    output_format: md                   # text | md | json | html | csv | code | file
    capability_hint: web_search         # See capability hints below
    tool_hint: brave_search             # Optional: prefer a specific tool server
    mandatory: false                    # If true, always included in every session
    max_tokens: 2048                    # Override max_tokens for this task
    timeout_seconds: 60                 # Override per-task timeout
    depends_on: []                      # Task names that must complete first

  - name: write_report
    description: Write a structured report from research findings
    output_format: md
    capability_hint: document_generation
    depends_on: [web_research]
```

### Execution modes (`complexity`)

| Value | Name | How it works | When to use |
|---|---|---|---|
| `adaptive` | **Adaptive** | LLM decomposes and executes freely each run. Soft hints accumulate in the blueprint's *Discovery Hints* section after each run to steer future ones. | Open-ended tasks where the approach may vary: research, writing, classification |
| `pinned` | **Pinned** | LLM still executes each sub-task, but the decomposition DAG is locked to the blueprint's *Topology* section (hard constraint). Reproducible workflow on every run. | Recurring workflows with a known fixed structure — e.g. SDLC: code → test → deploy |
| `scripted` | **Scripted** | Bypasses the LLM entirely. Your Python handler function runs directly and returns the output. Zero token cost, fully auditable. | DB lookups, API calls, validation, math — anything where the logic is fixed |

For `scripted` tasks, set `handler` to the dotted Python path of your function:

```yaml
task_types:
  - name: fetch_user
    description: Look up a user record from the database
    complexity: scripted
    handler: my_pkg.handlers.fetch_user
    output_format: json
```

For `pinned` tasks, pair with a `blueprint` that has a `## Topology` section. After the first successful run the framework populates it automatically, or you can author it by hand:

```yaml
task_types:
  - name: sdlc
    description: End-to-end software development lifecycle
    complexity: pinned
    blueprint: sdlc.md    # must contain a ## Topology section
    output_format: md
```

---

### Capability hints

Tells the router which kind of tool server this task needs:

| Hint | Purpose |
|---|---|
| `auto` | Let the agent pick |
| `llm_synthesis` | No external tools — pure LLM reasoning |
| `web_search` | Needs a web search tool server |
| `bash` | Needs a shell execution sandbox |
| `code_exec` | Needs a code interpreter |
| `document_generation` | Needs a document writer tool |
| `image_generation` | Needs an image generator tool |

---

## `tool_servers`

MCP tool server connections. Three transports supported.

```yaml
tool_servers:
  # SSE transport — connects to a running HTTP server
  brave_search:
    transport: sse
    url: http://localhost:8051/sse
    headers:
      Authorization: "Bearer ${BRAVE_API_KEY}"
    capabilities:
      - web_search

  # stdio transport — spawns a subprocess
  filesystem:
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp/workspace"]
    capabilities:
      - file_read
      - file_write

  # streamable_http transport — MCP 1.x HTTP streaming
  custom_api:
    transport: streamable_http
    url: http://localhost:9000/mcp
    headers:
      Authorization: "Bearer ${MY_API_TOKEN}"
    capabilities:
      - custom_action
```

Environment variable substitution with `${VAR}` works in any string value.

---

## `storage`

```yaml
storage:
  base_path: ./cortex_storage           # Root directory for persistent data
  result_ttl_seconds: 3600              # How long task results are kept in memory
```

### SQLite backend (single-host)

```yaml
sqlite:
  enabled: true
  path: ./cortex_storage/cortex.db
  wal_mode: true                        # Recommended for concurrent reads
```

### Redis backend (distributed)

```yaml
redis:
  enabled: true
  url: redis://localhost:6379/0
  key_prefix: "cortex:myagent:"         # Isolate agents sharing one Redis
```

> **Never share a SQLite file across running agents.** Use Redis for multi-process deployments.

---

## `history`

```yaml
history:
  enabled: true
  max_records_per_user: 1000
  retention_days: 90
```

When enabled, every completed session is stored and queryable via `cortex replay SESSION_ID`.

---

## `validation`

```yaml
validation:
  enabled: true
  threshold: 0.75                       # Min composite score (hard floor: 0.60)
  model: null                           # Override model for validation (null = default)
```

Every response is scored on intent match, completeness, and coherence. Responses below `threshold` are flagged on `SessionResult.validation_report`.

---

## `learning`

```yaml
learning:
  enabled: true
  consent_enabled: true                 # Only learn from sessions with user consent
  min_confirmations_medium: 3           # Distinct users for medium confidence
  min_confirmations_high: 5             # Distinct users for high confidence
  auto_apply_confidence: null           # null | medium | high
```

When enabled, the agent observes task patterns and stages delta proposals. Review with `cortex delta review` and apply with `cortex delta apply`.

---

## Environment variable substitution

Any string field in `cortex.yaml` can use `${VAR}` syntax:

```yaml
tool_servers:
  github:
    transport: sse
    url: ${GITHUB_MCP_URL}
    headers:
      Authorization: "Bearer ${GITHUB_TOKEN}"
```

Substitution happens at load time. Missing variables produce a clear error.

---

## Environment variables Cortex reads directly

| Variable | Description |
|---|---|
| `CORTEX_CONFIG` | Override default config path (defaults to `./cortex.yaml`) |
| `CORTEX_LOG_LEVEL` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |
| `ANTHROPIC_API_KEY` | Default Anthropic provider key |
| `OPENAI_API_KEY` | Default OpenAI provider key |
| `GEMINI_API_KEY` | Default Gemini provider key |
| `XAI_API_KEY` | Default Grok provider key |
| `MISTRAL_API_KEY` | Default Mistral provider key |
| `DEEPSEEK_API_KEY` | Default DeepSeek provider key |
| `AWS_DEFAULT_REGION` | Bedrock region |
| `AZURE_AI_API_KEY` | Azure AI provider key |

---

## Minimal working example

```yaml
agent:
  name: HelloAgent
  description: A minimal Cortex agent

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

That's the entire file. No tool servers, no MCP setup — just an LLM-driven Q&A agent.

---

## Validating your config

```bash
cortex dry-run "test request"
```

Loads the config, compiles the task graph, and reports any errors **without making any LLM calls**. Use this in CI to gate config changes.
