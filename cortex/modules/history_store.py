"""HistoryStore — manages persistent user session history."""
import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class PersistedFile:
    task_name: str
    file_path: str
    mime_type: str
    size_bytes: int = 0


@dataclass
class TaskCompletion:
    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    timed_out_tasks: int = 0
    skipped_tasks: int = 0

    @property
    def completion_rate(self) -> float:
        if self.total_tasks == 0:
            return 0.0
        return self.completed_tasks / self.total_tasks


@dataclass
class TokenUsageByRole:
    total_tokens: int = 0
    primary_agent_tokens: int = 0
    mcp_agent_tokens: int = 0
    validation_agent_tokens: int = 0
    by_provider: Dict[str, int] = field(default_factory=dict)


@dataclass
class HistoryRecord:
    session_id: str
    timestamp: str  # ISO 8601
    user_id: str
    original_request: str
    response_summary: str
    task_completion: TaskCompletion
    validation_score: Optional[float]
    validation_passed: Optional[bool]
    user_consent: str  # "positive" | "negative" | "none"
    token_usage: TokenUsageByRole
    persisted_files: List[PersistedFile]
    duration_seconds: float

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "HistoryRecord":
        tc = data.pop("task_completion", {})
        tu = data.pop("token_usage", {})
        pf = data.pop("persisted_files", [])
        data["task_completion"] = TaskCompletion(**tc) if isinstance(tc, dict) else tc
        data["token_usage"] = TokenUsageByRole(**tu) if isinstance(tu, dict) else tu
        data["persisted_files"] = [PersistedFile(**f) if isinstance(f, dict) else f for f in pf]
        return cls(**data)


@dataclass
class PaginatedHistory:
    records: List[HistoryRecord]
    page: int
    page_size: int
    total_records: int
    total_pages: int


@dataclass
class ResumeValidation:
    valid: bool
    session_id: str
    missing_files: List[str] = field(default_factory=list)
    error: Optional[str] = None


class HistoryStore:
    """
    Manages persistent user session history.
    Written at session end. Read at session start for agent context.
    Storage layout:
      {base_path}/history/{user_id}/{session_id}.json
      {base_path}/history/{user_id}/files/{session_id}/{task_name}/{filename}
    """

    def __init__(self, base_path: str, encryption_enabled: bool = False, encryption_key: Optional[str] = None):
        self._base = Path(base_path) / "history"
        self._encryption_enabled = encryption_enabled
        self._fernet = None
        if encryption_enabled and encryption_key:
            try:
                from cryptography.fernet import Fernet
                self._fernet = Fernet(encryption_key.encode() if isinstance(encryption_key, str) else encryption_key)
            except ImportError:
                logger.warning("cryptography package not available; encryption disabled")

    def _user_dir(self, user_id: str) -> Path:
        # Sanitize user_id to prevent path traversal
        safe_uid = user_id.replace("/", "_").replace("..", "_")
        return self._base / safe_uid

    def _record_path(self, user_id: str, session_id: str) -> Path:
        safe_sid = session_id.replace("/", "_").replace("..", "_")
        return self._user_dir(user_id) / f"{safe_sid}.json"

    def _files_dir(self, user_id: str, session_id: str) -> Path:
        safe_sid = session_id.replace("/", "_").replace("..", "_")
        return self._user_dir(user_id) / "files" / safe_sid

    def _encode(self, data: bytes) -> bytes:
        if self._fernet:
            return self._fernet.encrypt(data)
        return data

    def _decode(self, data: bytes) -> bytes:
        if self._fernet:
            return self._fernet.decrypt(data)
        return data

    async def write_session(self, record: HistoryRecord) -> None:
        """Write history record to filesystem. Encrypt if configured."""
        user_dir = self._user_dir(record.user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        path = self._record_path(record.user_id, record.session_id)
        raw = json.dumps(record.to_dict(), indent=2).encode("utf-8")
        encoded = self._encode(raw)
        with open(path, "wb") as f:
            f.write(encoded)
        logger.debug("History written: %s", path)

    async def read_user_history(
        self,
        user_id: str,
        page: int = 1,
        page_size: int = 20,
    ) -> PaginatedHistory:
        """Read history records, reverse chronological."""
        user_dir = self._user_dir(user_id)
        if not user_dir.exists():
            return PaginatedHistory(records=[], page=page, page_size=page_size, total_records=0, total_pages=0)

        records = await self._load_all_records(user_id)
        records.sort(key=lambda r: r.timestamp, reverse=True)

        total = len(records)
        total_pages = max(1, (total + page_size - 1) // page_size)
        start = (page - 1) * page_size
        end = start + page_size
        page_records = records[start:end]

        return PaginatedHistory(
            records=page_records,
            page=page,
            page_size=page_size,
            total_records=total,
            total_pages=total_pages,
        )

    async def _load_all_records(self, user_id: str) -> List[HistoryRecord]:
        user_dir = self._user_dir(user_id)
        records = []
        for path in user_dir.glob("*.json"):
            try:
                raw = path.read_bytes()
                decoded = self._decode(raw)
                data = json.loads(decoded.decode("utf-8"))
                records.append(HistoryRecord.from_dict(data))
            except Exception as e:
                logger.warning("Failed to load history record %s: %s", path, e)
        return records

    async def read_session_detail(self, user_id: str, session_id: str) -> Optional[HistoryRecord]:
        path = self._record_path(user_id, session_id)
        if not path.exists():
            return None
        try:
            raw = path.read_bytes()
            decoded = self._decode(raw)
            data = json.loads(decoded.decode("utf-8"))
            return HistoryRecord.from_dict(data)
        except Exception as e:
            logger.warning("Failed to read session detail %s: %s", session_id, e)
            return None

    async def get_context_sessions(self, user_id: str, max_sessions: int) -> List[HistoryRecord]:
        """Return most recent N sessions for primary agent context injection."""
        records = await self._load_all_records(user_id)
        records.sort(key=lambda r: r.timestamp, reverse=True)
        return records[:max_sessions]

    async def get_session_file(
        self,
        user_id: str,
        session_id: str,
        task_name: str,
    ) -> Tuple[bytes, str]:
        """Return (file_bytes, mime_type) for a persisted task output."""
        record = await self.read_session_detail(user_id, session_id)
        if not record:
            raise FileNotFoundError(f"Session {session_id} not found in history")
        for pf in record.persisted_files:
            if pf.task_name == task_name:
                file_path = Path(pf.file_path)
                if not file_path.exists():
                    raise FileNotFoundError(f"File not found: {pf.file_path}")
                data = file_path.read_bytes()
                if self._fernet:
                    data = self._fernet.decrypt(data)
                return data, pf.mime_type
        raise FileNotFoundError(f"No persisted file for task '{task_name}' in session {session_id}")

    async def persist_task_output(
        self,
        session_id: str,
        user_id: str,
        task_name: str,
        source_path: str,
        mime_type: str,
    ) -> str:
        """Copy task output to history path. Encrypt if configured."""
        files_dir = self._files_dir(user_id, session_id)
        safe_task = task_name.replace("/", "_").replace("..", "_")
        dest_dir = files_dir / safe_task
        dest_dir.mkdir(parents=True, exist_ok=True)
        src = Path(source_path)
        if not src.exists():
            raise FileNotFoundError(f"Source file not found: {source_path}")
        dest = dest_dir / src.name
        data = src.read_bytes()
        if self._fernet:
            data = self._fernet.encrypt(data)
        dest.write_bytes(data)
        logger.debug("Persisted task output: %s -> %s", source_path, dest)
        return str(dest)

    async def search_history(self, user_id: str, query: str) -> List[HistoryRecord]:
        """Keyword search across response_summary and original_request fields."""
        query_lower = query.lower()
        records = await self._load_all_records(user_id)
        return [
            r for r in records
            if query_lower in r.original_request.lower()
            or query_lower in r.response_summary.lower()
        ]

    async def delete_user_history(self, user_id: str) -> None:
        """GDPR deletion — atomic removal of all records, indices, and files."""
        import shutil
        user_dir = self._user_dir(user_id)
        if user_dir.exists():
            shutil.rmtree(user_dir)
            logger.info("Deleted all history for user: %s", user_id)

    async def auto_cleanup(self, user_id: str, retention_days: int) -> int:
        """Delete records older than retention_days. Return count deleted."""
        from datetime import datetime, timezone, timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        records = await self._load_all_records(user_id)
        deleted = 0
        for record in records:
            try:
                ts = datetime.fromisoformat(record.timestamp)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    path = self._record_path(user_id, record.session_id)
                    path.unlink(missing_ok=True)
                    # Also remove files
                    files_dir = self._files_dir(user_id, record.session_id)
                    if files_dir.exists():
                        import shutil
                        shutil.rmtree(files_dir)
                    deleted += 1
            except Exception as e:
                logger.warning("Error during cleanup for record %s: %s", record.session_id, e)
        logger.info("Auto-cleanup: deleted %d records for user %s (>%d days old)", deleted, user_id, retention_days)
        return deleted

    async def validate_resume(
        self,
        user_id: str,
        session_id: str,
        resume_tasks: Optional[List[str]] = None,
    ) -> ResumeValidation:
        """Validate that session exists and files are present before confirming resume."""
        record = await self.read_session_detail(user_id, session_id)
        if not record:
            return ResumeValidation(valid=False, session_id=session_id, error="Session not found in history")

        missing_files = []
        if resume_tasks:
            for pf in record.persisted_files:
                if pf.task_name in resume_tasks:
                    if not Path(pf.file_path).exists():
                        missing_files.append(pf.file_path)

        return ResumeValidation(
            valid=len(missing_files) == 0,
            session_id=session_id,
            missing_files=missing_files,
        )
