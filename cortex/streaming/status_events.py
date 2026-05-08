"""Event dataclasses for streaming status updates to clients."""
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class EventType(str, Enum):
    STATUS = "status"
    CLARIFICATION = "clarification"
    CLARIFICATION_REQUEST = "clarification_request"  # sub-agent → human, mid-task
    RESULT = "result"
    TASK_START = "task_start"
    TASK_COMPLETE = "task_complete"
    ERROR = "error"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    EXTERNAL_MCP_AUTH_REQUIRED = "external_mcp_auth_required"  # auth-gated MCP found during session
    ANT_HATCHED = "ant_hatched"    # ant agent spawned and registered
    ANT_STOPPED = "ant_stopped"    # ant agent stopped or crashed
    LEARNING = "learning"          # autonomic learning gate decision
    TASK_TOOL_CALL = "task_tool_call"          # sub-agent invoked a tool
    TASK_BLUEPRINT = "task_blueprint"          # full DAG after decomposition
    INTENT_CLASSIFIED = "intent_classified"    # intent gate result
    SYNTHESIS_TIER = "synthesis_tier"          # synthesis excerpt tier chosen
    WORKSPACE_EVENT = "workspace_event"        # file-system change in workspace
    FILE_OUTPUT = "file_output"                # agent produced a downloadable file
    SESSION_TOKEN_USAGE = "session_token_usage"  # cumulative token counters


@dataclass
class StatusEvent:
    message: str
    session_id: str
    event_type: EventType = EventType.STATUS
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_sse(self) -> str:
        import json
        data = {
            "type": self.event_type.value,
            "message": self.message,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            **self.metadata,
        }
        return f"event: {self.event_type.value}\ndata: {json.dumps(data)}\n\n"


@dataclass
class ClarificationEvent:
    question: str
    session_id: str
    clarification_id: str
    event_type: EventType = EventType.CLARIFICATION
    timestamp: float = field(default_factory=time.time)
    options: list = field(default_factory=list)

    def to_sse(self) -> str:
        import json
        data = {
            "type": self.event_type.value,
            "question": self.question,
            "session_id": self.session_id,
            "clarification_id": self.clarification_id,
            "options": self.options,
            "timestamp": self.timestamp,
        }
        return f"event: clarification\ndata: {json.dumps(data)}\n\n"


@dataclass
class ClarificationRequestEvent:
    """Emitted by a sub-agent mid-execution when it needs human input.

    Distinct from ClarificationEvent (which is pre-decomposition, emitted by
    the primary agent). This event pauses a specific running task until the
    user answers; other tasks in the same wave continue unaffected.
    """
    question: str
    session_id: str
    clarification_id: str
    task_id: str
    task_name: str
    context: Optional[str] = None
    event_type: EventType = EventType.CLARIFICATION_REQUEST
    timestamp: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        import json
        data = {
            "type": self.event_type.value,
            "question": self.question,
            "session_id": self.session_id,
            "clarification_id": self.clarification_id,
            "task_id": self.task_id,
            "task_name": self.task_name,
            "context": self.context,
            "timestamp": self.timestamp,
        }
        return f"event: clarification_request\ndata: {json.dumps(data)}\n\n"


@dataclass
class ResultEvent:
    content: str
    session_id: str
    partial: bool = False
    event_type: EventType = EventType.RESULT
    timestamp: float = field(default_factory=time.time)
    validation_score: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_sse(self) -> str:
        import json
        data = {
            "type": self.event_type.value,
            "content": self.content,
            "session_id": self.session_id,
            "partial": self.partial,
            "timestamp": self.timestamp,
            **self.metadata,
        }
        if self.validation_score is not None:
            data["validation_score"] = self.validation_score
        return f"event: result\ndata: {json.dumps(data)}\n\n"


@dataclass
class LearningEvent:
    """Emitted once at session end after the autonomic learning gate runs.

    Captures the observable inputs (complexity, validation, intent mode) that
    drove the decision so callers can reason about *why* learning did or did
    not fire without reading the audit log. The ``action`` field mirrors the
    ``learned_action`` column written to :class:`HistoryRecord`.
    """
    session_id: str
    action: str                          # see HistoryRecord.learned_action values
    complexity_score: Optional[float] = None
    validation_score: Optional[float] = None
    intent_mode: Optional[str] = None    # chat | task | hybrid
    staged_tasks: list = field(default_factory=list)
    applied_tasks: list = field(default_factory=list)
    message: str = ""
    event_type: EventType = EventType.LEARNING
    timestamp: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        import json
        data = {
            "type": self.event_type.value,
            "session_id": self.session_id,
            "action": self.action,
            "complexity_score": self.complexity_score,
            "validation_score": self.validation_score,
            "intent_mode": self.intent_mode,
            "staged_tasks": self.staged_tasks,
            "applied_tasks": self.applied_tasks,
            "message": self.message,
            "timestamp": self.timestamp,
        }
        return f"event: {self.event_type.value}\ndata: {json.dumps(data)}\n\n"


@dataclass
class ExternalMCPAuthRequiredEvent:
    """Emitted at session end when an external MCP was found but requires auth.

    The ``cortex_yaml_snippet`` field contains a ready-to-paste YAML block the
    developer can add to ``tool_servers:`` in cortex.yaml to configure the server
    with credentials for the next run.
    """
    session_id: str
    servers: list          # list of dicts: {url, name, capabilities, reason}
    cortex_yaml_snippet: str
    event_type: EventType = EventType.EXTERNAL_MCP_AUTH_REQUIRED
    timestamp: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        import json
        data = {
            "type": self.event_type.value,
            "session_id": self.session_id,
            "servers": self.servers,
            "cortex_yaml_snippet": self.cortex_yaml_snippet,
            "timestamp": self.timestamp,
        }
        return f"event: external_mcp_auth_required\ndata: {json.dumps(data)}\n\n"


@dataclass
class TaskToolCallEvent:
    """Emitted when a task sub-agent invokes a tool (MCP or built-in)."""
    session_id: str
    task_id: str
    task_name: str
    tool_name: str
    tool_input: Dict[str, Any] = field(default_factory=dict)
    event_type: EventType = EventType.TASK_TOOL_CALL
    timestamp: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        import json
        data = {
            "type": self.event_type.value,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "task_name": self.task_name,
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
            "timestamp": self.timestamp,
        }
        return f"event: task_tool_call\ndata: {json.dumps(data)}\n\n"


@dataclass
class TaskBlueprintEvent:
    """Emitted after decomposition — describes the full task DAG before execution."""
    session_id: str
    tasks: list   # list of {id, name, description, depends_on, wave}
    waves: int = 0
    event_type: EventType = EventType.TASK_BLUEPRINT
    timestamp: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        import json
        data = {
            "type": self.event_type.value,
            "session_id": self.session_id,
            "tasks": self.tasks,
            "waves": self.waves,
            "timestamp": self.timestamp,
        }
        return f"event: task_blueprint\ndata: {json.dumps(data)}\n\n"


@dataclass
class IntentClassifiedEvent:
    """Emitted by Intent Gate immediately after classifying a request."""
    session_id: str
    intent_mode: str          # chat | task | hybrid
    confidence: float = 0.0
    reasoning: str = ""
    event_type: EventType = EventType.INTENT_CLASSIFIED
    timestamp: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        import json
        data = {
            "type": self.event_type.value,
            "session_id": self.session_id,
            "intent_mode": self.intent_mode,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "timestamp": self.timestamp,
        }
        return f"event: intent_classified\ndata: {json.dumps(data)}\n\n"


@dataclass
class SynthesisTierEvent:
    """Emitted when a synthesis excerpt tier is determined."""
    session_id: str
    tier: str           # short | medium | full | structured
    reason: str = ""
    event_type: EventType = EventType.SYNTHESIS_TIER
    timestamp: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        import json
        data = {
            "type": self.event_type.value,
            "session_id": self.session_id,
            "tier": self.tier,
            "reason": self.reason,
            "timestamp": self.timestamp,
        }
        return f"event: synthesis_tier\ndata: {json.dumps(data)}\n\n"


@dataclass
class WorkspaceEvent:
    """Emitted by WorkspaceBash or file-writing tools to report workspace changes."""
    session_id: str
    task_id: str
    task_name: str
    action: str         # created | modified | deleted | listed
    path: str           # relative path inside workspace
    is_dir: bool = False
    event_type: EventType = EventType.WORKSPACE_EVENT
    timestamp: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        import json
        data = {
            "type": self.event_type.value,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "task_name": self.task_name,
            "action": self.action,
            "path": self.path,
            "is_dir": self.is_dir,
            "timestamp": self.timestamp,
        }
        return f"event: workspace_event\ndata: {json.dumps(data)}\n\n"


@dataclass
class FileOutputEvent:
    """Emitted when an agent writes a downloadable output file."""
    session_id: str
    task_name: str
    filename: str
    mime_type: str = "application/octet-stream"
    size_bytes: int = 0
    event_type: EventType = EventType.FILE_OUTPUT
    timestamp: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        import json
        data = {
            "type": self.event_type.value,
            "session_id": self.session_id,
            "task_name": self.task_name,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "timestamp": self.timestamp,
        }
        return f"event: file_output\ndata: {json.dumps(data)}\n\n"


@dataclass
class SessionTokenUsageEvent:
    """Emitted periodically to report cumulative token consumption."""
    session_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    event_type: EventType = EventType.SESSION_TOKEN_USAGE
    timestamp: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        import json
        data = {
            "type": self.event_type.value,
            "session_id": self.session_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "timestamp": self.timestamp,
        }
        return f"event: session_token_usage\ndata: {json.dumps(data)}\n\n"
