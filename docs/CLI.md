# CLI Reference

[← Back to README](../README.md)

The `cortex` CLI is installed automatically when you `pip install -e .`.

```bash
cortex --help
cortex <command> --help
```

---

## Quick reference

| Command | What it does |
|---|---|
| `cortex run` | Run a single request through the agent |
| `cortex chat` | Interactive chat REPL |
| `cortex setup` | Browser-based setup wizard |
| `cortex dev` | Hot-reload development mode |
| `cortex dry-run` | Validate config without LLM calls |
| `cortex replay` | Display a historical session |
| `cortex config validate` | Validate `cortex.yaml` and report errors |
| `cortex config show` | Print a section-by-section config summary |
| `cortex sessions` | Browse, inspect, export, and delete session history |
| `cortex blueprints` | List, view, edit, and delete learning blueprints |
| `cortex mcps` | Manage the external MCP server registry |
| `cortex stats` | Token usage and session statistics |
| `cortex providers` | List and test configured LLM providers |
| `cortex storage` | Inspect and purge framework storage |
| `cortex delta` | Review, apply, reject, and rollback learning deltas |
| `cortex ants` | Manage the self-spawning Ant Colony |
| `cortex migrate` | Migrate `cortex.yaml` between schema versions |
| `cortex publish` | Package and deploy your agent |
| `cortex spec` | Generate a capability manifest |
| `cortex config-ui` | Visual config editor in the browser |

---

## `cortex run`

Run a single natural-language request through the agent and print the response. The most direct way to invoke the agent from the command line without writing Python.

```bash
cortex run "Research the latest vector DB benchmarks" [OPTIONS]
```

| Flag | Default | Description |
|---|---|---|
| `--config` | `cortex.yaml` | Path to config file |
| `--user-id` | `cli-user` | Principal identity for the session |
| `-v / --verbose` | false | Print all streaming events (status, task start/complete, learning) |

Streaming events are printed live as the framework executes. The final response and a summary line (tasks, tokens, time, validation score) are printed when the session completes.

```
Cortex › Research the latest vector DB benchmarks

  ▶  task: web_research
  ✓  web_research complete
  ▶  task: write_report
  ✓  write_report complete

Response
  # Vector Database Benchmarks 2025
  ...

tasks=2/2  tokens=3481  time=12.3s  validation=0.91(pass)
```

---

## `cortex chat`

Start an interactive multi-turn chat REPL. Each message is sent as a new session; the agent processes it and prints the response before prompting for the next message.

```bash
cortex chat [OPTIONS]
```

| Flag | Default | Description |
|---|---|---|
| `--config` | `cortex.yaml` | Path to config file |
| `--user-id` | `cli-user` | Principal identity |
| `-v / --verbose` | false | Print all streaming events |

Type `exit`, `quit`, or press `Ctrl-C` to leave the REPL.

### In-flight interrupts

While the agent is working on a request you can type a message and press Enter at any time to interrupt it. The agent will process your message at the next wave boundary (after the current parallel batch of tasks finishes) and take one of two actions:

**Rethink (replan)** — if your message redirects, corrects, or adds context, the agent updates its remaining task graph accordingly and continues. Examples:

```
  · Type a message and press Enter at any time to interrupt the agent.
  ↩  interrupt queued — will be processed after the current wave
  ↩  agent is rethinking its plan
```

**Terminate** — if your message clearly requests a stop (`stop`, `cancel`, `abort`, `quit`, etc.) the agent winds down immediately after the current wave and synthesises whatever work has been completed so far.

```
  ↩  terminating session per your request

cortex  Here is what I found before stopping: …
```

The same mechanism is available to SDK consumers via `framework.inject_user_message(session_id, message)` — see the API reference for details.

---

## `cortex setup`

Launches a browser-based setup wizard that walks you through generating a `cortex.yaml`.

```bash
cortex setup [--port 7799] [--no-browser]
```

| Flag | Default | Description |
|---|---|---|
| `--port` | `7799` | Port the wizard listens on |
| `--no-browser` | false | Don't auto-open the browser |

The wizard walks you through: agent identity → LLM provider → tool servers → task types → storage → publish mode. On save, it writes a validated `cortex.yaml` in the current directory.

**Re-running:** If `cortex.yaml` already exists the wizard loads your settings. Fields that would break existing data are locked.

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

---

## `cortex dry-run`

Validates your config and compiles the task graph **without making any LLM calls**. Use it in CI to gate config changes.

```bash
cortex dry-run [--config cortex.yaml] "REQUEST"
```

---

## `cortex replay`

Loads and displays a historical session from storage.

```bash
cortex replay SESSION_ID --user-id USER_ID [--config cortex.yaml]
```

Shows the request, task outcomes, token usage, validation score, final response, and duration. Requires `history.enabled: true` in `cortex.yaml`.

---

## `cortex config`

Validate and inspect `cortex.yaml` configuration.

### `cortex config validate`

Check the config file for schema errors, heuristic warnings, and task-graph cycle violations. Safe to run in CI — exits non-zero on any error.

```bash
cortex config validate [--config cortex.yaml] [--strict]
```

| Flag | Default | Description |
|---|---|---|
| `--config` | `cortex.yaml` | Path to config file |
| `--strict` | false | Treat warnings as errors |

Output on success:

```
Validating cortex.yaml …

✓ Config is valid
  Agent       : ResearchAgent
  Provider    : anthropic / claude-sonnet-4-5
  Task types  : 3
  Tool servers: 2
```

Warnings (yellow `⚠`) are printed for common oversights — no task types defined, no tool servers, etc. Errors (red `✗`) cause a non-zero exit.

### `cortex config show`

Print a human-readable summary of each configuration section.

```bash
cortex config show [--config cortex.yaml] [--section agent|llm|tasks|tools|storage|learning|all]
```

| Flag | Default | Description |
|---|---|---|
| `--section` | `all` | Which section to display |

---

## `cortex sessions`

Browse, inspect, export, and delete session history. All subcommands require `--user-id`.

### `cortex sessions list`

```bash
cortex sessions list --user-id USER_ID [--config cortex.yaml] [--limit 20] [--format table|json]
```

Prints a table of recent sessions with timestamp, task completion, token count, validation score, and duration.

### `cortex sessions show`

```bash
cortex sessions show SESSION_ID --user-id USER_ID [--config cortex.yaml] [--format text|json]
```

Prints full detail for a single session: request, response summary, task breakdown, token usage by role, validation score, complexity score, and persisted files.

### `cortex sessions delete`

```bash
cortex sessions delete --user-id USER_ID --all [--yes] [--config cortex.yaml]
```

Deletes all history for the given user. Prompts for confirmation unless `--yes` is passed.

### `cortex sessions export`

```bash
cortex sessions export SESSION_ID --user-id USER_ID [--format json|md] [-o output.json] [--config cortex.yaml]
```

Export a session to JSON (full record) or Markdown (human-readable report). Writes to stdout by default; use `-o` to write to a file.

---

## `cortex blueprints`

Manage per-task learning blueprints — Markdown files the Learning Engine builds up over time and injects into task context.

### `cortex blueprints list`

```bash
cortex blueprints list [--config cortex.yaml] [--format table|names]
```

Lists all blueprint files in the storage directory with size and last-modified date.

### `cortex blueprints show`

```bash
cortex blueprints show <name> [--config cortex.yaml]
```

Prints the full content of a blueprint including metadata (task name, version, updated date).

### `cortex blueprints edit`

```bash
cortex blueprints edit <name> [--config cortex.yaml]
```

Opens the blueprint in `$EDITOR` (falls back to `vi`). Useful for manually correcting lessons or adding known constraints before the Learning Engine has gathered enough confirmations to promote them automatically.

### `cortex blueprints delete`

```bash
cortex blueprints delete <name> [--yes] [--config cortex.yaml]
```

Permanently deletes the blueprint file. The Learning Engine will recreate it from scratch on the next matching session.

---

## `cortex mcps`

Manage the external MCP server registry (`cortex_auto_mcps.yaml`). The framework auto-populates this during CapabilityScout runs; these commands let you inspect and manually curate it.

### `cortex mcps list`

```bash
cortex mcps list [--config cortex.yaml] [--filter all|verified|auth-pending|failed] [--format table|json]
```

Lists all known external MCP servers with name, URL, status, and capabilities.

### `cortex mcps test`

```bash
cortex mcps test <URL> [--timeout 10]
```

Makes a GET request to the URL and reports whether it is reachable. Quick connectivity check before adding to `cortex.yaml`.

### `cortex mcps add`

```bash
cortex mcps add <URL> [--name NAME] [--capability TAG] [--config cortex.yaml]
```

Registers an external MCP server manually. `--capability` may be repeated to attach multiple tags.

```bash
cortex mcps add https://mcp.example.com/sse \
  --name "Example MCP" \
  --capability web_search \
  --capability summarisation
```

### `cortex mcps remove`

```bash
cortex mcps remove <URL> [--yes] [--config cortex.yaml]
```

Removes an MCP server from the registry. Prompts for confirmation unless `--yes` is passed.

---

## `cortex stats`

Show token usage and session statistics — either aggregated across all users or scoped to a single user.

```bash
cortex stats [--config cortex.yaml] [--user-id USER_ID] [--limit 100] [--format text|json]
```

| Flag | Default | Description |
|---|---|---|
| `--user-id` | all users | Restrict stats to one user |
| `--limit` | `100` | Max sessions to scan per user |
| `--format` | `text` | `text` for a human summary, `json` for machine-readable output |

Output (text mode):

```
Session Statistics
  Users            : 4
  Sessions         : 312
  Avg duration     : 9.2s

Token Usage
  Total tokens     : 1,842,301
  Avg / session    : 5,904
  Primary agent    : 921,043
  MCP agents       : 731,204
  Validation agent : 190,054

Task Completion
  Tasks            : 894/921 completed  (12 failed)
  Avg val. score   : 0.883

By User
  alice             142 sessions    821,304 tokens
  bob                98 sessions    604,217 tokens
```

---

## `cortex providers`

List and test the LLM providers configured in `cortex.yaml`.

### `cortex providers list`

```bash
cortex providers list [--config cortex.yaml]
```

Prints a table of all configured providers (key, provider name, model, API key env var, and whether the env var is currently set).

### `cortex providers test`

```bash
cortex providers test [--config cortex.yaml] [--provider KEY] [--prompt TEXT]
```

Sends a minimal test prompt to each provider (or a specific one) and reports success or failure. Useful before deploying to confirm all API keys are wired correctly.

| Flag | Default | Description |
|---|---|---|
| `--provider` | all | Test only this provider key |
| `--prompt` | `Reply with the single word: OK` | Test prompt |

---

## `cortex storage`

Inspect and maintain framework storage.

### `cortex storage status`

```bash
cortex storage status [--config cortex.yaml]
```

Shows the storage base path, existence status, per-subdirectory sizes (history, blueprints, ants, delta, snapshots), SQLite DB size, user count, and session count.

### `cortex storage purge`

```bash
cortex storage purge [--config cortex.yaml] [--older-than 30] [--user-id USER_ID] [--yes]
```

Deletes session history records older than N days. Scoped to a single user with `--user-id`, or runs across all users when omitted. Prompts for confirmation unless `--yes` is passed.

| Flag | Default | Description |
|---|---|---|
| `--older-than` | `30` | Retention threshold in days |
| `--user-id` | all users | Purge only this user's history |
| `--yes` | false | Skip confirmation |

---

## `cortex delta`

Manages the autonomic learning system.

```bash
cortex delta review                                    # Show staged proposals
cortex delta apply [--min-confidence high|medium|low] [--yes]
cortex delta reject TASK_NAME                          # Reject a specific proposal
cortex delta history                                   # Show apply history
cortex delta rollback [--yes]                          # Restore previous cortex.yaml
```

By default (`learning.auto_apply_delta: true`) proposals promote themselves into `cortex.yaml` as soon as the distinct-principal confirmation threshold is met. These commands are useful for auditing what was applied, or for manually curating when `auto_apply_delta: false`. `apply` writes changes to `cortex.yaml` and takes a backup first; `rollback` restores that backup.

---

## `cortex ants`

Manage the Ant Colony — self-spawning specialist Cortex agents that run as MCP servers.

### `cortex ants list`

```bash
cortex ants list [--config cortex.yaml]
```

Displays a table of name, capability, port, status (running / stopped / crashed), PID, and restart count.

### `cortex ants status <name>`

```bash
cortex ants status <ant-name> [--config cortex.yaml]
```

Show detailed status of a specific ant: capability, description, URL, port, PID, restart count, created time, and config path.

### `cortex ants hatch <name>`

```bash
cortex ants hatch <name> --capability <cap> [--description <desc>] [--config cortex.yaml]
```

Manually hatch a new specialist ant agent. Requires `ant_colony.enabled: true` in `cortex.yaml`. A port is allocated automatically starting from `ant_colony.base_port`.

| Flag | Required | Description |
|---|---|---|
| `--capability` | Yes | Capability hint the ant will serve (e.g. `web_search`) |
| `--description` | No | Human-readable description |

### `cortex ants forge <script>`

```bash
cortex ants forge <script.py> --name <name> --capability <cap> [OPTIONS]
```

Spawn a **pre-written** MCP server Python script as a forged ant. Unlike `hatch`, no LLM template generation happens — the script already exists and must bind to the port passed via the `CORTEX_ANT_PORT` environment variable.

| Flag | Default | Description |
|---|---|---|
| `--name` | required | Unique ant name |
| `--capability` | required | Capability string for registry routing |
| `--persist` | false | Re-hatch this ant automatically on next framework startup |
| `--timeout` | `30.0` | Startup health-check timeout in seconds |

```bash
cortex ants forge ./tools/pdf_extractor.py \
  --name pdf-ant \
  --capability pdf_extraction \
  --persist
```

### `cortex ants stop <name>`

```bash
cortex ants stop <ant-name> [--config cortex.yaml]
```

Terminates the ant process. State is saved as `stopped` in `ants.yaml`; the supervisor will not auto-restart a manually stopped ant.

### `cortex ants stop-all`

```bash
cortex ants stop-all [--config cortex.yaml]
```

Stop all running ants (prompts for confirmation).

---

## `cortex migrate`

Validates that your `cortex.yaml` is compatible with a target schema version and migrates it if needed.

```bash
cortex migrate [--config cortex.yaml] [--from-version 0.9] [--to-version 1.0]
```

---

## `cortex publish`

Publish your agent as a Docker image, Python package, MCP server, or Chat UI.

### `cortex publish docker`

```bash
cortex publish docker [--tag cortex-agent:latest] [--with-ui] [--config cortex.yaml]
```

Generates `Dockerfile.cortex`. Pass `--with-ui` to bundle Cortex Synapse on port 8090.

### `cortex publish package`

```bash
cortex publish package [--output-dir dist]
```

Runs `python -m build` and produces a wheel + sdist in `dist/`.

### `cortex publish mcp`

```bash
cortex publish mcp [--config cortex.yaml] [--port 8080]
```

Starts an aiohttp MCP server. Endpoints: `POST /mcp` and `POST /run`.

### `cortex publish ui`

```bash
cortex publish ui [--config cortex.yaml] [--host 0.0.0.0] [--port 8090]
```

Serves **Cortex Synapse** — the built-in web frontend with file uploads, streaming, history search, and HITL clarification.

---

## `cortex spec`

Generates a capability manifest for the agent — useful for documentation or discovery.

```bash
cortex spec [--config cortex.yaml] [--format json|yaml] [-o output.json]
```

---

## `cortex config-ui`

Launches the Cortex Config Studio — a browser-based UI for editing `cortex.yaml`, blueprints, learning deltas, and session metadata.

```bash
cortex config-ui [--config cortex.yaml] [--port 7801] [--host 127.0.0.1] [--no-browser] [--storage-base PATH]
```

---

## Global environment variables

| Variable | Effect |
|---|---|
| `CORTEX_CONFIG` | Override default config path (`cortex.yaml`) for all commands |
| `CORTEX_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `CORTEX_INTERACTION_MODE` | Override `agent.interaction_mode` (`interactive` / `rpc`). Set automatically to `rpc` by `cortex publish mcp`. |

Setting `CORTEX_CONFIG` in your shell is handy when you work on a specific agent for a while — all commands will target it without needing `--config` every time.
