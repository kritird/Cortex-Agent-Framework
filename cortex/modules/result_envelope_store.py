"""ResultEnvelopeStore — manages storage and retrieval of result envelopes."""
import dataclasses
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from cortex.exceptions import CortexStorageError, CortexSecurityError
from cortex.llm.context import TokenUsage

logger = logging.getLogger(__name__)


@dataclass
class ResultEnvelope:
    schema_version: str = "1.0"
    task_id: str = ""
    session_id: str = ""
    status: str = "pending"   # pending | running | complete | failed | timeout
    mandatory: bool = True
    output_type: str = "text"  # md | json | text | file
    output_value: str = ""     # content string or file path
    content_summary: str = ""  # bounded excerpt — primary agent reads this
    error: Optional[str] = None
    duration_ms: int = 0
    tool_trace: List[str] = field(default_factory=list)
    context_hints: Dict[str, str] = field(default_factory=dict)
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    generated_script: Optional[str] = None   # LLM-generated source code if task ran via code_exec
    is_adhoc: bool = False                    # True if task was not in cortex.yaml at runtime

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "ResultEnvelope":
        usage_data = data.pop("token_usage", {})
        if isinstance(usage_data, dict):
            data["token_usage"] = TokenUsage(**usage_data)
        return cls(**data)


@dataclass
class TaskEnvelope:
    schema_version: str = "1.0"
    task_id: str = ""
    session_id: str = ""
    task_name: str = ""
    instruction: str = ""
    input_refs: List[str] = field(default_factory=list)
    output_location: str = ""
    output_format: str = "text"
    mandatory: bool = True
    capability_hint: str = "auto"
    llm_provider: str = "default"
    timeout_seconds: int = 40
    context_hints: Dict[str, str] = field(default_factory=dict)


class ResultEnvelopeStore:
    """
    Manages storage and retrieval of result envelopes.
    Hot path: always in-process memory.
    Crash resilience: also written to SQLite or Redis if configured.
    Large files: always written to local filesystem.
    """

    def __init__(
        self,
        base_path: str,
        storage_backend=None,
        result_envelope_max_kb: int = 64,
        large_file_threshold_mb: int = 5,
    ):
        self._base_path = Path(base_path)
        self._storage = storage_backend
        self._max_envelope_bytes = result_envelope_max_kb * 1024
        self._large_threshold = large_file_threshold_mb * 1024 * 1024
        # In-process memory: {(session_id, task_id): ResultEnvelope}
        self._memory: Dict[tuple, ResultEnvelope] = {}

    async def write_envelope(self, envelope: ResultEnvelope) -> None:
        """
        Write result envelope to memory and optionally to persistent backend.
        """
        if not envelope.task_id or not envelope.session_id:
            raise CortexStorageError("ResultEnvelope must have task_id and session_id")

        key = (envelope.session_id, envelope.task_id)

        # Check envelope size
        serialized = json.dumps(envelope.to_dict())
        if len(serialized.encode()) > self._max_envelope_bytes:
            # If output_value is large content (not a file path), write to file
            if envelope.output_type != "file" and len(envelope.output_value) > 1000:
                file_path = await self._spill_to_file(
                    envelope.session_id,
                    envelope.task_id,
                    envelope.output_value.encode("utf-8"),
                    f"output.{envelope.output_type}",
                )
                envelope.output_value = file_path
                envelope.output_type = "file"

        self._memory[key] = envelope

        # Persist to backend
        if self._storage:
            storage_key = f"envelope:{envelope.session_id}:{envelope.task_id}"
            try:
                await self._storage.set(storage_key, envelope.to_dict(), ttl_seconds=3600)
            except Exception as e:
                logger.warning("Failed to persist envelope to backend: %s", e)

    async def read_envelope(self, session_id: str, task_id: str) -> Optional[ResultEnvelope]:
        """Read envelope from memory or fall back to backend."""
        key = (session_id, task_id)
        if key in self._memory:
            return self._memory[key]

        # Fall back to storage backend (crash recovery)
        if self._storage:
            storage_key = f"envelope:{session_id}:{task_id}"
            data = await self._storage.get(storage_key)
            if data:
                envelope = ResultEnvelope.from_dict(data)
                self._memory[key] = envelope
                return envelope

        return None

    async def read_all_session_envelopes(self, session_id: str) -> List[ResultEnvelope]:
        """Return all envelopes for a session."""
        # From memory
        envelopes = [
            env for (sid, _), env in self._memory.items()
            if sid == session_id
        ]

        # Also check backend for any not in memory
        if self._storage:
            pattern = f"envelope:{session_id}:*"
            try:
                keys = await self._storage.keys(pattern)
                for key in keys:
                    task_id = key.split(":")[-1]
                    mem_key = (session_id, task_id)
                    if mem_key not in self._memory:
                        data = await self._storage.get(key)
                        if data:
                            env = ResultEnvelope.from_dict(data)
                            self._memory[mem_key] = env
                            envelopes.append(env)
            except Exception as e:
                logger.warning("Failed to read envelopes from backend: %s", e)

        return envelopes

    async def write_large_file(
        self,
        session_id: str,
        task_id: str,
        content: bytes,
        filename: str,
    ) -> str:
        """Write large file output to filesystem. Returns file path."""
        return await self._spill_to_file(session_id, task_id, content, filename)

    async def _spill_to_file(
        self,
        session_id: str,
        task_id: str,
        content: bytes,
        filename: str,
    ) -> str:
        """Write content to session's output directory. Returns path string."""
        # Validate session_id doesn't contain path traversal
        if ".." in session_id or "/" in session_id:
            raise CortexSecurityError(f"Invalid session_id for file storage: {session_id}")

        # Sanitise task_id for path
        safe_task_id = task_id.replace("/", "_").replace("..", "_")
        output_dir = self._base_path / session_id / "outputs" / safe_task_id
        output_dir.mkdir(parents=True, exist_ok=True)

        # Sanitise filename
        safe_filename = os.path.basename(filename).replace("..", "_")
        if not safe_filename:
            safe_filename = "output.bin"

        file_path = output_dir / safe_filename

        with open(file_path, "wb") as f:
            f.write(content)

        logger.debug("Spilled large output to: %s", file_path)
        return str(file_path)

    async def read_file_for_bash(self, file_path: str, session_id: str) -> str:
        """Read a file for bash-assisted context assembly. Validates path is in session namespace."""
        resolved = Path(file_path).resolve()
        session_dir = (self._base_path / session_id).resolve()

        if not str(resolved).startswith(str(session_dir)):
            raise CortexSecurityError(
                f"File path '{file_path}' is outside session namespace for session '{session_id}'"
            )

        if not resolved.exists():
            raise CortexStorageError(f"File not found: {file_path}")

        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def cleanup_session(self, session_id: str) -> None:
        """Remove all in-process envelopes for this session."""
        keys_to_remove = [k for k in self._memory if k[0] == session_id]
        for k in keys_to_remove:
            del self._memory[k]
        logger.debug("ResultEnvelopeStore cleaned up session: %s", session_id)
