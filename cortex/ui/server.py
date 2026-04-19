"""Chat UI HTTP server — aiohttp + SSE over a CortexFramework instance."""
import asyncio
import base64
import json
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from aiohttp import web

from cortex.framework import CortexFramework


logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


@dataclass
class _PendingSession:
    ui_id: str
    user_id: str
    queue: asyncio.Queue
    task: asyncio.Task
    real_session_id: Optional[str] = None
    clarification_answers: Dict[str, asyncio.Future] = field(default_factory=dict)


def _uploads_dir(framework: CortexFramework) -> Path:
    base = Path(framework._config.storage.base_path) / "ui_uploads"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _safe_name(name: str) -> str:
    # Reuse the framework's sanitiser for consistency with the rest of the stack.
    return framework_sanitiser_filename(name)


def framework_sanitiser_filename(name: str) -> str:
    from cortex.security.sanitiser import InputSanitiser
    return InputSanitiser().sanitise_filename(name)


# ── Auth ──────────────────────────────────────────────────────────────────────

def _resolve_user(request: web.Request) -> Optional[str]:
    """Return user_id for this request, or None if unauthenticated.

    For auth.mode=none, an anonymous user_id is generated and stored in a cookie.
    For token/basic modes, the request must present valid credentials.
    """
    framework: CortexFramework = request.app["framework"]
    auth = framework._config.ui.auth
    mode = auth.mode

    if mode == "token":
        header = request.headers.get("Authorization", "")
        token = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
        if not token:
            token = request.headers.get("X-Cortex-Token", "").strip()
        if not auth.token or not secrets.compare_digest(token, auth.token):
            return None
        return "token_user"

    if mode == "basic":
        header = request.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return None
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8", errors="replace")
            user, _, pw = decoded.partition(":")
        except Exception:
            return None
        if (
            not auth.username
            or not auth.password
            or not secrets.compare_digest(user, auth.username)
            or not secrets.compare_digest(pw, auth.password)
        ):
            return None
        return user

    # mode == "none"
    uid = request.cookies.get("cortex_uid")
    if not uid:
        uid = "anon_" + uuid.uuid4().hex[:16]
    return uid


def _attach_anon_cookie(response: web.StreamResponse, user_id: str) -> None:
    if user_id.startswith("anon_"):
        response.set_cookie(
            "cortex_uid",
            user_id,
            max_age=60 * 60 * 24 * 365,
            httponly=True,
            samesite="Lax",
        )


def _unauthorised(framework: CortexFramework) -> web.Response:
    mode = framework._config.ui.auth.mode
    headers = {}
    if mode == "basic":
        headers["WWW-Authenticate"] = 'Basic realm="Cortex"'
    return web.Response(status=401, text="Unauthorised", headers=headers)


# ── Routes ────────────────────────────────────────────────────────────────────

async def handle_index(request: web.Request) -> web.StreamResponse:
    framework: CortexFramework = request.app["framework"]
    user_id = _resolve_user(request)
    if user_id is None:
        return _unauthorised(framework)
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        return web.Response(text="<h1>UI assets missing.</h1>", content_type="text/html")
    response = web.FileResponse(index_path)
    _attach_anon_cookie(response, user_id)
    return response


async def handle_config(request: web.Request) -> web.Response:
    framework: CortexFramework = request.app["framework"]
    user_id = _resolve_user(request)
    if user_id is None:
        return _unauthorised(framework)
    cfg = framework._config
    payload = {
        "title": cfg.ui.title,
        "auth_mode": cfg.ui.auth.mode,
        "file_input": {
            "max_size_mb": cfg.file_input.max_size_mb,
            "allowed_mime_types": cfg.file_input.allowed_mime_types,
        },
        "user_id": user_id,
    }
    response = web.json_response(payload)
    _attach_anon_cookie(response, user_id)
    return response


async def handle_new_session(request: web.Request) -> web.Response:
    framework: CortexFramework = request.app["framework"]
    user_id = _resolve_user(request)
    if user_id is None:
        return _unauthorised(framework)

    reader = await request.multipart()
    text: str = ""
    saved_files: list[str] = []
    ui_id = uuid.uuid4().hex
    upload_dir = _uploads_dir(framework) / user_id / ui_id
    upload_dir.mkdir(parents=True, exist_ok=True)

    max_bytes = framework._config.file_input.max_size_mb * 1024 * 1024

    async for part in reader:
        if part.name == "text":
            text = (await part.text()).strip()
        elif part.name == "files":
            filename = framework_sanitiser_filename(part.filename or "upload")
            dest = upload_dir / filename
            size = 0
            with dest.open("wb") as f:
                while True:
                    chunk = await part.read_chunk()
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        f.close()
                        dest.unlink(missing_ok=True)
                        return web.json_response(
                            {"error": f"file '{filename}' exceeds max size"},
                            status=413,
                        )
                    f.write(chunk)
            saved_files.append(str(dest))

    if not text and not saved_files:
        return web.json_response({"error": "empty request"}, status=400)

    queue: asyncio.Queue = asyncio.Queue()

    async def _driver():
        try:
            await framework.run_session(
                user_id=user_id,
                request=text or "(file upload)",
                event_queue=queue,
                file_refs=saved_files or None,
            )
        except Exception as exc:
            logger.exception("UI session failed: %s", exc)
            await queue.put({"type": "error", "message": str(exc)})
        finally:
            await queue.put(None)

    task = asyncio.create_task(_driver())
    pending = _PendingSession(ui_id=ui_id, user_id=user_id, queue=queue, task=task)
    request.app["pending"][ui_id] = pending

    response = web.json_response({"ui_session_id": ui_id})
    _attach_anon_cookie(response, user_id)
    return response


async def handle_events(request: web.Request) -> web.StreamResponse:
    framework: CortexFramework = request.app["framework"]
    user_id = _resolve_user(request)
    if user_id is None:
        return _unauthorised(framework)

    ui_id = request.match_info["ui_id"]
    pending: Optional[_PendingSession] = request.app["pending"].get(ui_id)
    if not pending or pending.user_id != user_id:
        return web.Response(status=404, text="unknown session")

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
    await response.prepare(request)

    try:
        while True:
            event = await pending.queue.get()
            if event is None:
                # Session finished. Emit a synthetic 'done' event with the real session_id.
                done = {
                    "type": "done",
                    "session_id": pending.real_session_id,
                    "ui_session_id": pending.ui_id,
                }
                await response.write(
                    f"event: done\ndata: {json.dumps(done)}\n\n".encode("utf-8")
                )
                break

            if hasattr(event, "to_sse"):
                # Capture the real session_id the first time we see one.
                sid = getattr(event, "session_id", None)
                if sid and not pending.real_session_id:
                    pending.real_session_id = sid
                payload = event.to_sse().encode("utf-8")
            else:
                payload = f"event: error\ndata: {json.dumps(event)}\n\n".encode("utf-8")
            await response.write(payload)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        # Drop the pending entry once streaming finishes so memory doesn't grow.
        request.app["pending"].pop(ui_id, None)

    return response


async def handle_history_list(request: web.Request) -> web.Response:
    framework: CortexFramework = request.app["framework"]
    user_id = _resolve_user(request)
    if user_id is None:
        return _unauthorised(framework)

    store = framework._history_store
    if store is None:
        return web.json_response({"sessions": []})

    page = await store.read_user_history(user_id, page=1, page_size=100)
    sessions = []
    for rec in page.records:
        title = (rec.original_request or "").strip().splitlines()[0][:60] or "(untitled)"
        sessions.append({
            "session_id": rec.session_id,
            "title": title,
            "timestamp": rec.timestamp,
            "has_response": bool(rec.response_summary),
        })
    response = web.json_response({"sessions": sessions})
    _attach_anon_cookie(response, user_id)
    return response


async def handle_history_detail(request: web.Request) -> web.Response:
    framework: CortexFramework = request.app["framework"]
    user_id = _resolve_user(request)
    if user_id is None:
        return _unauthorised(framework)

    sid = request.match_info["sid"]
    store = framework._history_store
    if store is None:
        return web.json_response({"error": "history disabled"}, status=404)
    record = await store.read_session_detail(user_id, sid)
    if record is None:
        return web.json_response({"error": "not found"}, status=404)

    files = [
        {"task_name": pf.task_name, "filename": Path(pf.file_path).name, "mime_type": pf.mime_type}
        for pf in record.persisted_files
    ]
    return web.json_response({
        "session_id": record.session_id,
        "timestamp": record.timestamp,
        "request": record.original_request,
        "response": record.response_summary,
        "validation_score": record.validation_score,
        "duration_seconds": record.duration_seconds,
        "files": files,
    })


async def handle_history_file(request: web.Request) -> web.StreamResponse:
    framework: CortexFramework = request.app["framework"]
    user_id = _resolve_user(request)
    if user_id is None:
        return _unauthorised(framework)

    sid = request.match_info["sid"]
    task_name = request.match_info["task"]
    filename = request.match_info["name"]
    store = framework._history_store
    if store is None:
        return web.Response(status=404, text="history disabled")
    try:
        data, mime = await store.get_session_file(user_id, sid, task_name)
    except FileNotFoundError:
        return web.Response(status=404, text="not found")
    return web.Response(
        body=data,
        headers={
            "Content-Type": mime or "application/octet-stream",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


async def handle_history_delete(request: web.Request) -> web.Response:
    framework: CortexFramework = request.app["framework"]
    user_id = _resolve_user(request)
    if user_id is None:
        return _unauthorised(framework)
    sid = request.match_info["sid"]
    store = framework._history_store
    if store is not None:
        path = store._record_path(user_id, sid)
        path.unlink(missing_ok=True)
        files_dir = store._files_dir(user_id, sid)
        if files_dir.exists():
            import shutil
            shutil.rmtree(files_dir, ignore_errors=True)
    # Upload dirs are keyed by ui_session_id, not by real session_id, so we
    # don't have a direct mapping. Best-effort: leave them to retention cleanup.
    return web.json_response({"deleted": True})


# ── App wiring ────────────────────────────────────────────────────────────────

def build_app(framework: CortexFramework) -> web.Application:
    app = web.Application(client_max_size=1024 * 1024 * 1024)  # 1 GB body cap
    app["framework"] = framework
    app["pending"] = {}

    app.router.add_get("/", handle_index)
    app.router.add_get("/api/config", handle_config)
    app.router.add_post("/api/session", handle_new_session)
    app.router.add_get("/api/session/{ui_id}/events", handle_events)
    app.router.add_get("/api/history", handle_history_list)
    app.router.add_get("/api/history/{sid}", handle_history_detail)
    app.router.add_get("/api/history/{sid}/files/{task}/{name}", handle_history_file)
    app.router.add_delete("/api/history/{sid}", handle_history_delete)

    return app


async def run_ui_server(framework: CortexFramework) -> None:
    cfg = framework._config.ui
    app = build_app(framework)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.host, cfg.port)
    await site.start()
    logger.info("Cortex UI listening on http://%s:%d", cfg.host, cfg.port)
    try:
        # Sleep forever; aiohttp runs on the event loop.
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
