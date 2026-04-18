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

Manages the delta learning system.

```bash
cortex delta review                               # Show staged proposals
cortex delta apply [--min-confidence high|medium|low] [--yes]
cortex delta reject TASK_NAME                     # Reject a specific proposal
cortex delta history                              # Show apply history
cortex delta rollback [--yes]                     # Restore previous cortex.yaml
```

`apply` writes changes to `cortex.yaml` and takes a backup first. `rollback` restores that backup.

---

## `cortex migrate`

Validates that your `cortex.yaml` is compatible with a target schema version, and migrates it if needed.

```bash
cortex migrate [--config cortex.yaml] [--from-version 0.9] [--to-version 1.0]
```

---

## `cortex publish`

Publishes your agent as a Docker image, Python package, or MCP server.

### `cortex publish docker`

```bash
cortex publish docker [--tag cortex-agent:latest] [--config cortex.yaml]
```

Generates a `Dockerfile.cortex` next to your config. You then build it yourself:

```bash
docker build -f Dockerfile.cortex -t my-agent:latest .
docker run -p 8080:8080 --env-file .env my-agent:latest
```

### `cortex publish package`

```bash
cortex publish package [--output-dir dist]
```

Runs `python -m build` under the hood and produces a wheel + sdist in `dist/`. Install with `pip install dist/*.whl`.

### `cortex publish mcp`

```bash
cortex publish mcp [--config cortex.yaml] [--port 8080]
```

Runs the agent as an MCP server on the given port. Other Cortex agents (or any MCP client) can now consume this agent as a tool:

```yaml
tool_servers:
  my_agent:
    transport: sse
    url: http://host:8080/sse
```

See [DEPLOYMENT.md](DEPLOYMENT.md) for multi-agent mesh setups.

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

## Global environment variables

| Variable | Effect |
|---|---|
| `CORTEX_CONFIG` | Override default config path (`cortex.yaml`) for all commands |
| `CORTEX_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

Setting `CORTEX_CONFIG` in your shell is handy when you work on a specific agent for a while — `cortex dev` / `cortex dry-run` will target it without needing `--config` every time.
