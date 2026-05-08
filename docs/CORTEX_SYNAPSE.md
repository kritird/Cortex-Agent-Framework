# Cortex Synapse — Chat UI

[← Back to README](../README.md)

Cortex Synapse is the built-in web frontend served by `cortex publish ui`. It connects directly to your Cortex agent over SSE and provides a full-featured chat experience with live task observability, file handling, and session management — no separate frontend to build or host.

```bash
cortex publish ui --config cortex.yaml
# Cortex chat UI: http://localhost:8090
```

---

## Features

### Conversation & streaming

- **Live SSE streaming** — responses appear word-by-word as the LLM generates them.
- **Intent classification badge** — shows whether the turn was routed as `chat` or `task` and with what confidence, displayed before any work begins.
- **Task blueprint panel** — after decomposition, the full task DAG (task names, dependencies, waves) renders before execution so you can see the plan before the agent acts on it.
- **Task progress chips** — each running task emits status updates; chips show "decomposing → running N tasks → synthesising" live.
- **Tool call indicators** — when a sub-agent invokes an MCP or built-in tool, the tool name is surfaced inline.
- **Markdown rendering** — responses render as formatted markdown with syntax-highlighted code blocks (powered by `marked.js` and `highlight.js`).

### File handling

- **File uploads on send** — attach files to a message; they are validated against the `file_input` MIME / size limits in `cortex.yaml`.
- **Mid-session uploads** — additional files can be dropped into a running session without starting a new one.
- **Output file download** — when a task produces a file output, a download chip appears in-thread.
- **Session artifact ZIP** — download all output files for a completed session as a single ZIP from the session detail panel.
- **Inline file preview** — images and text files can be previewed in-browser (`?inline=1`) rather than downloaded.

### Workspace observability

When WorkspaceBash is active, file-system operations emit `WorkspaceEvent` SSE events that appear in the thread:

| Action | Shown as |
|---|---|
| `read` | "read path/to/file" |
| `modified` | "modified path/to/file" |
| `executed` | "executed /workspace/…" |
| `listed` | "listed directory/" |

### HITL (Human-in-the-Loop)

WorkspaceBash requires human approval before writing files or executing commands. Clarification prompts appear inline in the chat thread — type your answer and press Enter without leaving the conversation. The framework waits for the answer before proceeding.

### Token usage

A footer indicator shows cumulative input / output / cache tokens for the session, updated at session end via `SessionTokenUsageEvent`.

### Session history & search

- **Sidebar threads** — past sessions are listed; click to resume reading or re-open.
- **Full-text search** — the search bar queries across session titles and response summaries via `GET /api/history/search?q=...`.
- **Session delete** — delete a thread from the sidebar to remove it from history.

### Service launcher

Open Config Studio (`http://localhost:7801`) or Setup Wizard (`http://localhost:7799`) from inside the UI with one click. The UI server launches them as background subprocesses, waits up to 5 seconds for the port to open, then navigates to the URL — no separate terminal needed.

---

## Configuration

All Synapse settings live under the `ui` block in `cortex.yaml`:

```yaml
ui:
  enabled: true
  host: "0.0.0.0"      # Bind address; Synapse always prints localhost regardless
  port: 8090
  title: "My Agent"    # Shown in the browser tab and page header
  auth:
    mode: none         # none | token | basic
    # token: "s3cret"
    # username: admin
    # password: changeme
```

Configure via the wizard (`cortex setup` → Chat UI step) or by hand.

---

## REST API

Synapse also exposes a REST API for headless / programmatic access. The same auth mode applies to all endpoints.

### Sessions

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/session` | Start a new session. Body: `{"request": "...", "user_id": "..."}`. Returns `{"ui_id": "..."}`. |
| `GET` | `/api/session/{ui_id}/events` | SSE stream of all events for this session. |
| `POST` | `/api/session/{ui_id}/clarify` | Answer a HITL clarification. Body: `{"clarification_id": "...", "answer": "..."}`. |
| `POST` | `/api/session/{ui_id}/upload` | Upload additional files mid-session. Multipart form with `files` field(s). |

### History

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/history` | Paginated list of sessions for the current user. |
| `GET` | `/api/history/search?q=...` | Full-text search across session titles and response summaries. |
| `GET` | `/api/history/{sid}` | Detailed session record including task outcomes and file list. |
| `GET` | `/api/history/{sid}/files/{task}/{name}` | Download a task output file. Add `?inline=1` to serve inline. |
| `GET` | `/api/history/{sid}/artifacts/zip` | Download all output files for a session as a ZIP archive. |
| `DELETE` | `/api/history/{sid}` | Delete a session and its files. |

### Runtime management

| Method | Endpoint | Description |
|---|---|---|
| `DELETE` | `/api/ants/{ant_id}` | Request cancellation of a running ant task. |
| `POST` | `/api/runtime/delta/action` | Promote or discard a learning delta. Body: `{"delta_id": "...", "action": "promote"|"discard"}`. |
| `POST` | `/api/services/{service}/launch` | Ensure a companion service is running. `service` is `config` (Config Studio) or `wizard` (Setup Wizard). Returns `{"url": "...", "started": bool}`. |

### Configuration

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/config` | Returns agent name, description, auth mode, and UI title. |

---

## Headless usage (curl)

```bash
# Start a session
SESSION=$(curl -s -X POST http://localhost:8090/api/session \
  -H 'Content-Type: application/json' \
  -d '{"request": "Summarise the latest AI news", "user_id": "demo"}' | jq -r .ui_id)

# Stream events
curl -N "http://localhost:8090/api/session/$SESSION/events"

# Answer a HITL clarification (if one arrives)
curl -X POST "http://localhost:8090/api/session/$SESSION/clarify" \
  -H 'Content-Type: application/json' \
  -d '{"clarification_id": "abc123", "answer": "yes"}'

# Download session artifacts as ZIP
curl -o artifacts.zip "http://localhost:8090/api/history/$SESSION/artifacts/zip"
```

---

## Authentication

| Mode | How to configure | How clients authenticate |
|---|---|---|
| `none` | `auth.mode: none` | Anonymous; each browser gets a cookie-based user ID |
| `token` | `auth.mode: token` + `auth.token: s3cret` | `Authorization: Bearer s3cret` header, or `?token=s3cret` in URL |
| `basic` | `auth.mode: basic` + `auth.username` + `auth.password` | Standard HTTP Basic auth |

For production deployments exposed beyond localhost, use `token` or `basic` auth and put Synapse behind an HTTPS reverse proxy (nginx, Caddy, Cloudflare Tunnel).

---

## Technology

Cortex Synapse is a self-contained single HTML file (`cortex/ui/static/index.html`) — no build step, no npm, no bundler.

| Library | Purpose |
|---|---|
| [Tailwind CSS](https://tailwindcss.com) (CDN) | Utility-first styling |
| [Alpine.js](https://alpinejs.dev) (CDN) | Reactive UI state |
| [marked.js](https://marked.js.org) (CDN) | Markdown rendering |
| [highlight.js](https://highlightjs.org) (CDN) | Syntax highlighting |
| Inter + JetBrains Mono (Google Fonts) | Typography |

The entire UI is served as a static file by the aiohttp backend — no separate static server needed.

---

## Companion tools

From inside Synapse you can open:

- **Config Studio** (`cortex config-ui`, port 7801) — inspect and edit `cortex.yaml`, blueprints, learning deltas, and session metadata in a browser UI.
- **Setup Wizard** (`cortex setup`, port 7799) — re-run the guided wizard to change agent configuration.

Click the grid icon in the Synapse header to launch either tool. The UI server starts them as background processes if they're not already running.
