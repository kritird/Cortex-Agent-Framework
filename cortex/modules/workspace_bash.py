"""WorkspaceBash — workspace-aware file and command execution with mandatory HITL."""
import asyncio
import difflib
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from cortex.exceptions import CortexHITLDeniedError, CortexSecurityError

logger = logging.getLogger(__name__)

# Commands blocked regardless of workspace context
_BLOCKED_PATTERNS = frozenset(["rm -rf /", "sudo", "> /dev/", ":(){ :|:& };:"])


def extract_workspace_path(instruction: str) -> Optional[str]:
    """Return the first absolute or ~-rooted directory path found in *instruction*.

    Used by GenericMCPAgent._call_workspace_bash to extract the workspace root
    from the task instruction without embedding regex logic in the agent.
    """
    pattern = re.compile(r'(?:^|\s)((?:~|/)[^\s,;\'\"]+)', re.MULTILINE)
    for match in pattern.finditer(instruction):
        candidate = match.group(1).strip().rstrip(".,;")
        expanded = os.path.expanduser(candidate)
        if os.path.isdir(expanded):
            return expanded
    return None


class WorkspaceBash:
    """Workspace-aware file read/write and command execution.

    HITL is hardcoded for write and execute operations — it cannot be
    disabled regardless of the task config. Read-only operations
    (read_file, list_dir) never prompt.

    The workspace_path is NOT stored at init; it is supplied per-call so
    the same instance can serve any workspace directory.
    """

    def __init__(self, event_queue: Optional[asyncio.Queue], hitl_enabled: bool = True):
        self._event_queue = event_queue
        # hitl_enabled is informational — framework init enforces it cannot be False.
        self._hitl_enabled = hitl_enabled

    async def _emit_workspace_event(
        self,
        session_id: str,
        task,
        action: str,
        path: str,
        is_dir: bool = False,
    ) -> None:
        if self._event_queue is None:
            return
        try:
            from cortex.streaming.status_events import WorkspaceEvent
            await self._event_queue.put(WorkspaceEvent(
                session_id=session_id,
                task_id=getattr(task, "task_id", "workspace/unknown"),
                task_name=getattr(task, "task_name", "workspace_bash"),
                action=action,
                path=path,
                is_dir=is_dir,
            ))
        except Exception:
            pass

    # ── Read-only operations (no HITL) ────────────────────────────────────────

    async def read_file(self, workspace_path: str, rel_path: str) -> str:
        """Read a file from the workspace. No HITL."""
        target = self._resolve_path(workspace_path, rel_path)
        if not target.exists():
            return f"[File not found: {rel_path}]"
        if not target.is_file():
            return f"[Not a file: {rel_path}]"
        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return f"[Error reading {rel_path}: {exc}]"

    async def list_dir(self, workspace_path: str, rel_path: str = ".") -> str:
        """List directory contents. No HITL."""
        target = self._resolve_path(workspace_path, rel_path)
        if not target.exists():
            return f"[Directory not found: {rel_path}]"
        if not target.is_dir():
            return f"[Not a directory: {rel_path}]"
        try:
            entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
            lines = [("[f] " if e.is_file() else "[d] ") + e.name for e in entries]
            return "\n".join(lines) if lines else "[empty directory]"
        except Exception as exc:
            return f"[Error listing {rel_path}: {exc}]"

    # ── Mutating operations (HITL required) ───────────────────────────────────

    async def write_file(
        self,
        workspace_path: str,
        rel_path: str,
        content: str,
        task,
        session_id: str,
    ) -> str:
        """Write a file. HITL fires before write — diff shown when file exists."""
        target = self._resolve_path(workspace_path, rel_path)

        diff_preview = self._build_diff(target, rel_path, content)
        question = (
            f"I am about to write `{rel_path}` in `{workspace_path}`."
            f"{diff_preview}\nAllow this write? (yes/no)"
        )

        answer = await self._ask_hitl(task, question, session_id)
        if not _is_approved(answer):
            raise CortexHITLDeniedError(
                f"User denied write to {rel_path}", operation="write", path=rel_path
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"[Written: {rel_path} ({len(content)} chars)]"

    async def execute(
        self,
        workspace_path: str,
        command: str,
        task,
        session_id: str,
    ) -> str:
        """Run a shell command with workspace_path as cwd. HITL fires before execution."""
        workspace = Path(os.path.expanduser(workspace_path)).resolve()
        if not workspace.is_dir():
            return f"[Workspace directory not found: {workspace_path}]"

        self._check_command_safety(command, str(workspace))

        question = (
            f"I am about to run:\n```\n{command}\n```\n"
            f"in `{workspace_path}`.\nAllow? (yes/no)"
        )

        answer = await self._ask_hitl(task, question, session_id)
        if not _is_approved(answer):
            raise CortexHITLDeniedError(
                f"User denied command: {command}", operation="execute", path=workspace_path
            )

        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = proc.stdout or ""
            if proc.stderr:
                output += f"\n[stderr]\n{proc.stderr}"
            if proc.returncode != 0:
                output += f"\n[exit code: {proc.returncode}]"
            return output.strip() or "[no output]"
        except subprocess.TimeoutExpired:
            return "[Command timed out after 120s]"
        except Exception as exc:
            return f"[Command failed: {exc}]"

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _resolve_path(self, workspace_path: str, rel_path: str) -> Path:
        """Resolve path and verify it stays inside workspace_path."""
        workspace = Path(os.path.expanduser(workspace_path)).resolve()
        target = (workspace / rel_path).resolve()
        try:
            target.relative_to(workspace)
        except ValueError:
            raise CortexSecurityError(
                f"Path '{rel_path}' escapes workspace root '{workspace_path}'"
            )
        return target

    def _check_command_safety(self, command: str, workspace: str) -> None:
        """Block obviously dangerous commands before HITL fires."""
        lower = command.lower()
        for pattern in _BLOCKED_PATTERNS:
            if pattern in lower:
                raise CortexSecurityError(f"Blocked dangerous command: {command}")
        # Block writes to absolute paths outside workspace
        abs_paths = re.findall(r'(?:^|\s)(/[^\s;|&>]+)', command)
        for p in abs_paths:
            try:
                Path(p).resolve().relative_to(workspace)
            except ValueError:
                cmd_prefix = command[: command.find(p)]
                if any(kw in cmd_prefix for kw in (">", ">>", "tee ")):
                    raise CortexSecurityError(
                        f"Command writes to path outside workspace: {p}"
                    )

    @staticmethod
    def _build_diff(target: Path, rel_path: str, new_content: str) -> str:
        """Return a unified-diff block if the file already exists, else empty string."""
        if not (target.exists() and target.is_file()):
            return ""
        try:
            existing = target.read_text(encoding="utf-8", errors="replace")
            diff_lines = list(difflib.unified_diff(
                existing.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{rel_path}",
                tofile=f"b/{rel_path}",
                n=3,
            ))
            if not diff_lines:
                return "\n[No changes — content is identical]"
            return "\n```diff\n" + "".join(diff_lines[:60]) + "\n```"
        except Exception:
            return ""

    async def _ask_hitl(self, task, question: str, session_id: str) -> Optional[str]:
        """Emit a HITL clarification request and await the user's answer.

        Checks CORTEX_HITL_URL first so relay to parent framework works when
        this WorkspaceBash instance is running inside an ant subprocess.
        """
        hitl_url = os.environ.get("CORTEX_HITL_URL")
        if hitl_url:
            return await self._relay_hitl(hitl_url, question, task, session_id)

        event_queue = self._event_queue
        if event_queue is None:
            logger.warning(
                "WorkspaceBash HITL: no event_queue available for task %s — denying",
                getattr(task, "task_id", "?"),
            )
            return None

        import uuid
        from cortex.framework import _PENDING_TASK_CLARIFICATIONS
        from cortex.streaming.status_events import ClarificationRequestEvent

        task_id = getattr(task, "task_id", "workspace/unknown")
        task_name = getattr(task, "task_name", "workspace_bash")
        clarification_id = f"wbash_{task_id.replace('/', '_')}_{uuid.uuid4().hex[:6]}"
        wait_event = asyncio.Event()
        _PENDING_TASK_CLARIFICATIONS[clarification_id] = {
            "event": wait_event,
            "answer": None,
            "loop": asyncio.get_event_loop(),
        }

        await event_queue.put(ClarificationRequestEvent(
            question=question,
            session_id=session_id,
            clarification_id=clarification_id,
            task_id=task_id,
            task_name=task_name,
            context="workspace_bash_hitl",
        ))

        try:
            await asyncio.wait_for(wait_event.wait(), timeout=300)
            entry = _PENDING_TASK_CLARIFICATIONS.pop(clarification_id, {}) or {}
            return entry.get("answer")
        except asyncio.TimeoutError:
            _PENDING_TASK_CLARIFICATIONS.pop(clarification_id, None)
            logger.info("WorkspaceBash HITL timed out for task %s", task_id)
            return None

    @staticmethod
    async def _relay_hitl(
        hitl_url: str, question: str, task, session_id: str
    ) -> Optional[str]:
        """Relay HITL to parent framework relay when running inside an ant subprocess."""
        import aiohttp

        task_id = getattr(task, "task_id", "ant/unknown")
        payload = {"session_id": session_id, "question": question, "task_id": task_id}
        try:
            timeout = aiohttp.ClientTimeout(total=310)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{hitl_url}/hitl", json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("answer")
        except Exception as exc:
            logger.warning(
                "WorkspaceBash HITL relay to %s failed: %s — denying", hitl_url, exc
            )
        return None


def _is_approved(answer: Optional[str]) -> bool:
    """Return True if the answer string is an affirmative."""
    if answer is None:
        return False
    return answer.strip().lower() in {"yes", "y", "allow", "approve", "ok", "true", "1"}


# ── Per-session HITL relay server (used by CortexFramework) ──────────────────

class HITLRelayServer:
    """Lightweight aiohttp server that bridges ant HITL requests to the
    parent session's event_queue.

    Lifecycle: start() → (session runs) → stop()
    The URL (http://127.0.0.1:{port}) is passed to ants as CORTEX_HITL_URL.
    """

    def __init__(self, event_queue: asyncio.Queue, session_id: str):
        self._event_queue = event_queue
        self._session_id = session_id
        self._runner = None
        self.url: Optional[str] = None

    async def start(self) -> str:
        """Start the relay server and return its base URL."""
        import socket
        from aiohttp import web

        app = web.Application()
        app.router.add_post("/hitl", self._handle_hitl)

        # OS-assigned ephemeral port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", port)
        await site.start()
        self.url = f"http://127.0.0.1:{port}"
        logger.info("HITL relay started at %s for session %s", self.url, self._session_id)
        return self.url

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    async def _handle_hitl(self, request) -> None:
        from aiohttp import web
        import uuid
        from cortex.framework import _PENDING_TASK_CLARIFICATIONS
        from cortex.streaming.status_events import ClarificationRequestEvent

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)

        question = body.get("question", "")
        task_id = body.get("task_id", "ant/unknown")

        clarification_id = f"ant_relay_{uuid.uuid4().hex[:8]}"
        wait_event = asyncio.Event()
        _PENDING_TASK_CLARIFICATIONS[clarification_id] = {
            "event": wait_event,
            "answer": None,
            "loop": asyncio.get_event_loop(),
        }

        await self._event_queue.put(ClarificationRequestEvent(
            question=question,
            session_id=self._session_id,
            clarification_id=clarification_id,
            task_id=task_id,
            task_name="ant_hitl",
            context="ant_relay",
        ))

        try:
            await asyncio.wait_for(wait_event.wait(), timeout=300)
            entry = _PENDING_TASK_CLARIFICATIONS.pop(clarification_id, {}) or {}
            answer = entry.get("answer") or "no"
        except asyncio.TimeoutError:
            _PENDING_TASK_CLARIFICATIONS.pop(clarification_id, None)
            # Deny on timeout — surfaces as a task error so user knows HITL was bypassed
            answer = "no"
            logger.warning(
                "HITL relay timed out for task %s in session %s — denying",
                task_id, self._session_id,
            )

        return web.json_response({"answer": answer})
