"""SessionManager — owns complete session lifecycle."""
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from cortex.config.schema import AgentConfig, StorageConfig, HistoryConfig
from cortex.exceptions import CortexSessionLimitError, ActiveSessionInfo
from cortex.modules.history_store import HistoryRecord, HistoryStore

logger = logging.getLogger(__name__)


@dataclass
class ActiveSession:
    session_id: str
    user_id: str
    start_time: str
    request_preview: str
    status: str = "running"  # running | completing | terminated


@dataclass
class CrashedSession:
    session_id: str
    user_id: str
    start_time: str
    request_preview: str
    wal_entry: dict


class SessionManager:
    """Owns complete session lifecycle. All methods are async."""

    def __init__(
        self,
        agent_config: AgentConfig,
        storage_config: StorageConfig,
        history_config: HistoryConfig,
        history_store: HistoryStore,
        storage_backend=None,
    ):
        self._agent_config = agent_config
        self._storage_config = storage_config
        self._history_config = history_config
        self._history_store = history_store
        self._storage = storage_backend
        self._active: Dict[str, ActiveSession] = {}  # {session_id: ActiveSession}
        self._user_sessions: Dict[str, List[str]] = {}  # {user_id: [session_id]}
        self._accepting = True
        self._wal_path = Path(storage_config.base_path) / ".wal"
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Replay WAL and recover any incomplete operations."""
        self._wal_path.parent.mkdir(parents=True, exist_ok=True)
        await self._replay_wal()

    async def _append_wal(self, entry: dict) -> None:
        """Append a WAL entry (JSON line)."""
        try:
            with open(self._wal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({**entry, "timestamp": datetime.now(timezone.utc).isoformat()}) + "\n")
        except OSError as e:
            logger.warning("WAL write failed: %s", e)

    async def _replay_wal(self) -> None:
        """On startup, replay any incomplete operations from the WAL."""
        if not self._wal_path.exists():
            return
        try:
            with open(self._wal_path, "r", encoding="utf-8") as f:
                raw_lines = f.readlines()
        except OSError as e:
            logger.warning("WAL replay failed to read file: %s", e)
            return

        entries = []
        for i, line in enumerate(raw_lines):
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as e:
                logger.warning("Skipping corrupted WAL entry at line %d: %s", i + 1, e)

        completed = set()
        for entry in entries:
            if entry.get("op") == "complete":
                completed.add(entry.get("session_id"))

        for entry in entries:
            if entry.get("op") == "create" and entry.get("session_id") not in completed:
                sid = entry.get("session_id")
                uid = entry.get("user_id", "unknown")
                logger.info("WAL recovery: found incomplete session %s for user %s", sid, uid)
                # Register as a crashed session indicator (not in active — just in WAL)
        # Truncate WAL on clean startup
        with open(self._wal_path, "w") as f:
            pass

    async def create_session(self, user_id: str, request: str) -> ActiveSession:
        """Create a new session, enforcing concurrency limits."""
        if not self._accepting:
            raise CortexSessionLimitError(
                "Framework is shutting down — no new sessions accepted",
                active_session_exists=False,
                active_sessions=[],
                max_allowed=0,
            )

        async with self._lock:
            concurrency = self._agent_config.concurrency
            max_global = concurrency.max_concurrent_sessions
            max_per_user = concurrency.max_concurrent_sessions_per_user

            # Global limit
            if len(self._active) >= max_global:
                active_infos = [
                    ActiveSessionInfo(
                        session_id=s.session_id,
                        start_time=s.start_time,
                        request_preview=s.request_preview,
                    )
                    for s in self._active.values()
                ]
                raise CortexSessionLimitError(
                    f"Global session limit reached ({max_global}). Try again later.",
                    active_session_exists=True,
                    active_sessions=active_infos,
                    max_allowed=max_global,
                )

            # Per-user limit
            user_session_ids = self._user_sessions.get(user_id, [])
            active_user_sessions = [self._active[sid] for sid in user_session_ids if sid in self._active]
            if len(active_user_sessions) >= max_per_user:
                active_infos = [
                    ActiveSessionInfo(
                        session_id=s.session_id,
                        start_time=s.start_time,
                        request_preview=s.request_preview[:100],
                    )
                    for s in active_user_sessions
                ]
                raise CortexSessionLimitError(
                    f"You already have {len(active_user_sessions)} active session(s). "
                    f"Maximum allowed per user: {max_per_user}.",
                    active_session_exists=True,
                    active_sessions=active_infos,
                    max_allowed=max_per_user,
                )

            session_id = f"sess_{uuid4().hex[:8]}"
            now = datetime.now(timezone.utc).isoformat()
            session = ActiveSession(
                session_id=session_id,
                user_id=user_id,
                start_time=now,
                request_preview=request[:100],
            )

            # WAL intent
            await self._append_wal({"op": "create", "session_id": session_id, "user_id": user_id})

            # Initialize session storage namespace
            session_dir = Path(self._storage_config.base_path) / session_id
            session_dir.mkdir(parents=True, exist_ok=True)

            self._active[session_id] = session
            if user_id not in self._user_sessions:
                self._user_sessions[user_id] = []
            self._user_sessions[user_id].append(session_id)

            logger.info("Session created: %s for user %s", session_id, user_id)
            return session

    async def complete_session(
        self,
        session_id: str,
        record: Optional[HistoryRecord] = None,
        skip_storage_cleanup: bool = False,
    ) -> None:
        """
        Complete a session: write history, clean storage, deregister.

        skip_storage_cleanup: When True (timed-out sessions), the session directory
        is NOT wiped so that completed envelopes and the graph snapshot survive
        for a future resume call.
        """
        async with self._lock:
            session = self._active.get(session_id)
            if not session:
                logger.warning("complete_session called for unknown session: %s", session_id)
                return

            session.status = "completing"

        # Write history record
        if record and self._history_config.enabled:
            try:
                await self._history_store.write_session(record)
            except Exception as e:
                logger.warning("Failed to write history for session %s: %s", session_id, e)

        # Atomic cleanup via WAL
        await self._append_wal({"op": "complete", "session_id": session_id})

        if self._storage_config.atomic_cleanup and not skip_storage_cleanup:
            await self._cleanup_session_storage(session_id)

        async with self._lock:
            self._active.pop(session_id, None)
            user_id = session.user_id
            if user_id in self._user_sessions:
                self._user_sessions[user_id] = [
                    sid for sid in self._user_sessions[user_id] if sid != session_id
                ]

        logger.info("Session completed: %s", session_id)

    async def _cleanup_session_storage(self, session_id: str) -> None:
        """Remove session's temporary working directory."""
        import shutil
        session_dir = Path(self._storage_config.base_path) / session_id
        if session_dir.exists():
            try:
                shutil.rmtree(session_dir)
            except OSError as e:
                logger.warning("Failed to cleanup session dir %s: %s", session_dir, e)

    async def recover_crashed_sessions(self, user_id: str) -> List[CrashedSession]:
        """
        Check for sessions in 'running' state in storage but not in active_sessions.
        These are crash candidates.
        """
        crashed = []
        if not self._wal_path.exists():
            return crashed
        # Sessions in WAL that never got a 'complete' entry
        try:
            with open(self._wal_path, "r", encoding="utf-8") as f:
                entries = [json.loads(line) for line in f if line.strip()]
        except Exception:
            return crashed

        completed = {e["session_id"] for e in entries if e.get("op") == "complete"}
        for entry in entries:
            if entry.get("op") == "create" and entry.get("user_id") == user_id:
                sid = entry["session_id"]
                if sid not in completed and sid not in self._active:
                    crashed.append(CrashedSession(
                        session_id=sid,
                        user_id=user_id,
                        start_time=entry.get("timestamp", ""),
                        request_preview=entry.get("request_preview", ""),
                        wal_entry=entry,
                    ))
        return crashed

    async def auto_cleanup_expired_history(self, user_id: str) -> None:
        """Delete history records older than retention_days for this user."""
        if not self._history_config.enabled:
            return
        try:
            deleted = await self._history_store.auto_cleanup(
                user_id, self._history_config.retention_days
            )
            if deleted > 0:
                logger.info("Auto-cleanup: removed %d old records for user %s", deleted, user_id)
        except Exception as e:
            logger.warning("Auto-cleanup failed for user %s: %s", user_id, e)

    async def terminate_session(self, session_id: str) -> None:
        """Force-terminate an in-flight session."""
        await self._append_wal({"op": "terminate", "session_id": session_id})
        async with self._lock:
            session = self._active.pop(session_id, None)
            if session:
                if session.user_id in self._user_sessions:
                    self._user_sessions[session.user_id] = [
                        sid for sid in self._user_sessions[session.user_id]
                        if sid != session_id
                    ]
        logger.info("Session terminated: %s", session_id)

    def get_active_sessions(self, user_id: str) -> List[ActiveSession]:
        """Return list of currently active sessions for a user_id."""
        session_ids = self._user_sessions.get(user_id, [])
        return [self._active[sid] for sid in session_ids if sid in self._active]

    # ── Session resumption ────────────────────────────────────────────────────

    async def save_graph_snapshot(
        self,
        session_id: str,
        user_id: str,
        original_request: str,
        snapshot: Dict[str, Any],
    ) -> None:
        """
        Persist the task graph state for a timed-out session so it can be resumed.
        Writes to filesystem (always) and to the storage backend (if configured).
        The session directory is NOT cleaned up so completed envelopes survive.
        """
        session_dir = Path(self._storage_config.base_path) / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        payload = {
            "session_id": session_id,
            "user_id": user_id,
            "original_request": original_request,
            "snapshot": snapshot,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }

        snapshot_path = session_dir / "graph_snapshot.json"
        with open(snapshot_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        if self._storage:
            try:
                await self._storage.set(
                    f"graph_snapshot:{session_id}", payload, ttl_seconds=86400 * 7
                )
            except Exception as e:
                logger.warning("Failed to persist snapshot to backend: %s", e)

        # Update per-user resumable index
        index_path = Path(self._storage_config.base_path) / f"resumable_{user_id}.json"
        index: Dict[str, Any] = {}
        if index_path.exists():
            try:
                with open(index_path, encoding="utf-8") as f:
                    index = json.load(f)
            except Exception:
                index = {}
        index[session_id] = {
            "saved_at": payload["saved_at"],
            "original_request": original_request[:200],
        }
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index, f)

        logger.info("Saved graph snapshot for session %s (user %s)", session_id, user_id)

    async def load_graph_snapshot(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Load a graph snapshot. Returns None if not found."""
        snapshot_path = Path(self._storage_config.base_path) / session_id / "graph_snapshot.json"
        if snapshot_path.exists():
            try:
                with open(snapshot_path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning("Failed to read snapshot file for %s: %s", session_id, e)

        if self._storage:
            try:
                return await self._storage.get(f"graph_snapshot:{session_id}")
            except Exception as e:
                logger.warning("Failed to read snapshot from backend for %s: %s", session_id, e)

        return None

    async def get_resumable_sessions(self, user_id: str) -> List[Dict[str, Any]]:
        """Return sessions that can be resumed for this user (snapshot exists on disk)."""
        index_path = Path(self._storage_config.base_path) / f"resumable_{user_id}.json"
        if not index_path.exists():
            return []
        try:
            with open(index_path, encoding="utf-8") as f:
                index = json.load(f)
        except Exception:
            return []

        result = []
        for session_id, meta in index.items():
            snapshot_path = Path(self._storage_config.base_path) / session_id / "graph_snapshot.json"
            if snapshot_path.exists():
                result.append({
                    "session_id": session_id,
                    "saved_at": meta.get("saved_at"),
                    "original_request": meta.get("original_request", ""),
                })
        return result

    async def discard_snapshot(self, session_id: str, user_id: str) -> None:
        """Remove snapshot after successful resume."""
        snapshot_path = Path(self._storage_config.base_path) / session_id / "graph_snapshot.json"
        snapshot_path.unlink(missing_ok=True)

        if self._storage:
            try:
                await self._storage.delete(f"graph_snapshot:{session_id}")
            except Exception:
                pass

        index_path = Path(self._storage_config.base_path) / f"resumable_{user_id}.json"
        if index_path.exists():
            try:
                with open(index_path, encoding="utf-8") as f:
                    index = json.load(f)
                index.pop(session_id, None)
                with open(index_path, "w", encoding="utf-8") as f:
                    json.dump(index, f)
            except Exception:
                pass

    async def reopen_session(self, session_id: str, user_id: str) -> ActiveSession:
        """
        Re-register a timed-out session as active for resumption.
        Called instead of create_session() when resume_session_id is provided.
        """
        async with self._lock:
            if session_id in self._active:
                return self._active[session_id]

            session = ActiveSession(
                session_id=session_id,
                user_id=user_id,
                start_time=datetime.now(timezone.utc).isoformat(),
                request_preview="(resumed)",
                status="running",
            )
            self._active[session_id] = session
            self._user_sessions.setdefault(user_id, [])
            if session_id not in self._user_sessions[user_id]:
                self._user_sessions[user_id].append(session_id)
            await self._append_wal({
                "op": "create", "session_id": session_id,
                "user_id": user_id, "resumed": True,
            })
            logger.info("Reopened session %s for user %s (resume)", session_id, user_id)
            return session

    async def graceful_shutdown(self, timeout_seconds: int = 30) -> None:
        """Stop accepting new sessions, wait for active sessions to checkpoint."""
        self._accepting = False
        logger.info("Graceful shutdown: waiting up to %ds for %d sessions", timeout_seconds, len(self._active))
        deadline = time.monotonic() + timeout_seconds
        while self._active and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
        if self._active:
            logger.warning("Shutdown timeout: %d sessions still active", len(self._active))
        else:
            logger.info("Graceful shutdown complete")
