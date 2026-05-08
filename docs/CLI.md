# CLI Reference

[← Back to README](../README.md)

The `cortex` CLI is installed automatically when you `pip install -e .`.

```bash
cortex --help
cortex <command> --help
```

---

## `cortex setup`

Launches a browser-based setup wizard that walks you through generating a `cortex.yaml`.

```bash
cortex setup [--port 7799] [--no-browser]
```

| Flag | Default | Description |
|---|---|---|
| `--port` | `7799` | Port the wizard listens on (use a different port to run multiple wizards) |
| `--no-browser` | false | Don't auto-open the browser |

The wizard walks you through: agent identity → LLM provider → tool servers → task types → storage → publish mode. On save, it writes a validated `cortex.yaml` in the current directory.

**Re-running:** If `cortex.yaml` already exists, the wizard loads your settings. Fields that would break existing data (agent name after storage has data, storage backend after data is written) are locked.

---

## `cortex dev`

Runs Cortex in development mode with optional hot-reload.

```bash
cortex dev [--config cortex.yaml] [--watch]
```

| Flag | Default | Description |
|---|---|---|
| `--config` | `cortex.yaml` | Path to config file |
| `--watch` | false | Reload config on file changes |

Expected output:

```
[cortex] Initialising framework from cortex.yaml
[cortex] LLM: anthropic claude-sonnet-4-5
[cortex] Tool servers: brave_search (sse), filesystem (stdio)
[cortex] Watching cortex.yaml for changes...
[cortex] Ready.
```

---

## `cortex dry-run`

Validates your config and compiles the task graph **without making any LLM calls**. Use it in CI to gate config changes.

```bash
cortex dry-run [--config cortex.yaml] "REQUEST"
```

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

If a tool server is unreachable or a `depends_on` points at a missing task, dry-run fails here instead of mid-session.

---

## `cortex replay`

Loads and displays a historical session from storage.

```bash
cortex replay SESSION_ID --user-id USER_ID [--config cortex.yaml]
```

Shows the request, task decomposition, task outcomes, token usage, validation score, final response, and duration. Invaluable for debugging and audit.

Requires `history.enabled: true` in `cortex.yaml`.

---

## `cortex delta`

Manages the autonomic learning system.

```bash
cortex delta review                               # Show staged proposals
cortex delta apply [--min-confidence high|medium|low] [--yes]
cortex delta reject TASK_NAME                     # Reject a specific proposal
cortex delta history                              # Show apply history
cortex delta rollback [--yes]                     # Restore previous cortex.yaml
```

By default (`learning.auto_apply_delta: true`) proposals promote themselves into `cortex.yaml` as soon as the distinct-principal confirmation threshold is met — these commands are useful for auditing what was applied, or for overriding the default with `auto_apply_delta: false` and manually curating. `apply` writes changes to `cortex.yaml` and takes a backup first; `rollback` restores that backup.

---

## `cortex migrate`

Validates that your `cortex.yaml` is compatible with a target schema version, and migrates it if needed.

```bash
cortex migrate [--config cortex.yaml] [--from-version 0.9] [--to-version 1.0]
```

---

## `cortex publish`

Publishes your agent as a Docker image, Python package, MCP server, or Chat UI.

### `cortex publish docker`

```bash
cortex publish docker [--tag cortex-agent:latest] [--with-ui] [--config cortex.yaml]
```

Generates `Dockerfile.cortex` next to your config. Build and run it yourself:

```bash
docker build -f Dockerfile.cortex -t my-agent:latest .
docker run --rm -p 8090:8090 -e ANTHROPIC_API_KEY=your_key my-agent:latest
```

Pass `--with-ui` to generate a Dockerfile that starts Cortex Synapse (`cortex publish ui`) on port 8090. Without `--with-ui`, the bare framework runs on the MCP/REST port instead.

### `cortex publish package`

```bash
cortex publish package [--output-dir dist]
```

Runs `python -m build` (using the current Python interpreter) and produces a wheel + sdist in `dist/`. Install with `pip install dist/*.whl`.

### `cortex publish mcp`

```bash
cortex publish mcp [--config cortex.yaml] [--port 8080]
# MCP server running at http://localhost:8080/mcp
```

Starts a live aiohttp HTTP server. The agent is callable at two endpoints:

- `POST /mcp` — primary MCP endpoint; body: `{"input": "your request"}`, returns `{"output": "..."}`
- `POST /run` — convenience alias for `/mcp`

Other Cortex agents consume it by pointing a `tool_servers` entry at `/mcp`:

```yaml
tool_servers:
  my_agent:
    transport: sse
    url: http://host:8080/mcp
```

Or call it directly via curl:

```bash
curl -X POST http://localhost:8080/run \
  -H 'Content-Type: application/json' \
  -d '{"input": "Summarise the latest AI news"}'
```

**Interaction mode:** this command automatically sets `CORTEX_INTERACTION_MODE=rpc` so the agent never blocks on interactive clarifications — MCP clients cannot answer them. Override with an explicit `export CORTEX_INTERACTION_MODE=interactive` only if you genuinely need chat-mode behind MCP.

See [DEPLOYMENT.md](DEPLOYMENT.md) for multi-agent mesh setups.

### `cortex publish ui`

```bash
cortex publish ui [--config cortex.yaml] [--host 0.0.0.0] [--port 8090]
# Cortex chat UI: http://localhost:8090
```

Serves **Cortex Synapse** — the built-in web frontend. The printed URL always resolves to `localhost` even when the server binds `0.0.0.0`. Features:

- Text + file uploads (validated against `file_input` MIME / size limits); mid-session uploads also supported
- SSE-streamed status events, task blueprint display, intent classification, workspace events, and token usage
- Full-text history search, per-session artifact ZIP download, inline HITL clarification answers
- Service launcher: open Config Studio or Setup Wizard from inside the chat without a separate terminal
- Auth modes: `none` (anonymous cookie), `token`, `basic` — configured under the `ui.auth` block in `cortex.yaml`

CLI flags override `ui.host` and `ui.port` from config. Enable `history.enabled: true` so threads survive restarts. See [Deployment → Chat UI](DEPLOYMENT.md#option-d-chat-ui-cortex-synapse) for the full REST API and production tips.

---

## `cortex spec`

Generates a capability manifest for the agent — useful for documentation, discovery, or introspecting what an unknown `cortex.yaml` does.

```bash
cortex spec [--config cortex.yaml] [--format json|yaml] [-o output.json]
```

Outputs a structured description of task types, tool servers, LLM configuration, and capability hints.

---

## `cortex ants`

Manage the Ant Colony — self-spawning specialist Cortex agents that run as MCP servers.

```bash
cortex ants --help
```

### `cortex ants list`

List all ants in the colony with their current status.

```bash
cortex ants list [--config cortex.yaml]
```

Displays a table of name, capability, port, status (running / stopped / crashed), PID, and restart count.

### `cortex ants status <name>`

Show detailed status of a specific ant.

```bash
cortex ants status <ant-name> [--config cortex.yaml]
```

### `cortex ants hatch <name>`

Manually hatch a new specialist ant agent.

```bash
cortex ants hatch <name> --capability <cap> [--description <desc>] [--config cortex.yaml]
```

| Flag | Required | Description |
|---|---|---|
| `--capability` | Yes | Capability hint the ant will serve (e.g. `web_search`) |
| `--description` | No | Human-readable description of what this ant does |
| `--config` | No | Path to cortex.yaml (default: `cortex.yaml`) |

The colony must have `ant_colony.enabled: true` in `cortex.yaml`. A port is allocated automatically starting from `ant_colony.base_port`.

### `cortex ants stop <name>`

Stop a running ant by name.

```bash
cortex ants stop <ant-name> [--config cortex.yaml]
```

The ant process is terminated. The ant's state is saved as `stopped` in `ants.yaml`. The supervisor will not restart a manually stopped ant.

### `cortex ants stop-all`

Stop all running ants in the colony (prompts for confirmation).

```bash
cortex ants stop-all [--config cortex.yaml]
```

---

## `cortex config-ui`

Launches the Cortex Config Studio — a browser-based UI for inspecting and editing all framework configuration: `cortex.yaml`, stored blueprints, staged learning deltas, and session metadata.

```bash
cortex config-ui [--config cortex.yaml] [--port 7801] [--host 127.0.0.1] [--no-browser] [--storage-base PATH]
```

| Flag | Default | Description |
|---|---|---|
| `--config` | `cortex.yaml` | Path to the `cortex.yaml` to load and edit |
| `--port` | `7801` | Port the Config Studio server listens on |
| `--host` | `127.0.0.1` | Host to bind the server to |
| `--no-browser` | false | Start the server without auto-opening the browser |
| `--storage-base` | auto from config | Override `storage.base_path` for locating runtime files (blueprints, deltas, sessions) |

The studio reads `storage.base_path` from the config file automatically — use `--storage-base` only when the storage path was moved or the config file is in a different directory than the data.

---

## Global environment variables

| Variable | Effect |
|---|---|
| `CORTEX_CONFIG` | Override default config path (`cortex.yaml`) for all commands |
| `CORTEX_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `CORTEX_INTERACTION_MODE` | Override `agent.interaction_mode` (`interactive` / `rpc`). Set automatically to `rpc` by `cortex publish mcp`. |

Setting `CORTEX_CONFIG` in your shell is handy when you work on a specific agent for a while — `cortex dev` / `cortex dry-run` will target it without needing `--config` every time.
