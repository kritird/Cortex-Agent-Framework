"""Chat UI — publishable web frontend for a Cortex agent.

Exposes a clean HTTP+SSE surface over an initialised CortexFramework:

  GET  /                              → single-page chat UI
  GET  /api/config                    → UI-facing config snapshot
  POST /api/session                   → start a new session (multipart: text + files)
  GET  /api/session/{ui_id}/events    → SSE stream of events for that session
  POST /api/session/{ui_id}/clarify   → answer a mid-run clarification request
  GET  /api/history                   → list recent sessions for current user
  GET  /api/history/{sid}             → full history record
  GET  /api/history/{sid}/files/{task}/{name} → download a persisted task output
  DELETE /api/history/{sid}           → delete a session from history

Auth is driven by cortex.yaml's ``ui.auth`` block (none | token | basic).
Everything else is inherited from the already-loaded CortexFramework instance.
"""
from .server import run_ui_server, build_app

__all__ = ["run_ui_server", "build_app"]
