"""All framework exceptions for the Cortex Agent Framework."""
from dataclasses import dataclass, field
from typing import List, Optional, Any


class CortexException(Exception):
    """Base exception for all Cortex framework errors."""
    pass


class CortexConfigError(CortexException):
    """Configuration validation error with YAML path and line number."""
    def __init__(self, message: str, yaml_path: str = "", line: int = 0, column: int = 0):
        self.yaml_path = yaml_path
        self.line = line
        self.column = column
        super().__init__(message)

    def __str__(self):
        base = super().__str__()
        if self.yaml_path:
            loc = f"  → {self.yaml_path}"
            if self.line:
                loc += f", line {self.line}"
                if self.column:
                    loc += f", column {self.column}"
            return f"{base}\n{loc}"
        return base


@dataclass
class ActiveSessionInfo:
    session_id: str
    start_time: str
    request_preview: str


class CortexSessionLimitError(CortexException):
    """Raised when session limits are exceeded."""
    def __init__(
        self,
        message: str,
        active_session_exists: bool = True,
        active_sessions: List[ActiveSessionInfo] = None,
        max_allowed: int = 0,
    ):
        self.active_session_exists = active_session_exists
        self.active_sessions = active_sessions or []
        self.max_allowed = max_allowed
        super().__init__(message)


class CortexTaskError(CortexException):
    """Error during task execution."""
    def __init__(self, message: str, task_id: str = "", task_name: str = ""):
        self.task_id = task_id
        self.task_name = task_name
        super().__init__(message)


class CortexTaskTimeoutError(CortexTaskError):
    """Task exceeded its timeout."""
    pass


class CortexToolUnavailableError(CortexException):
    """Required tool server is unavailable."""
    def __init__(self, message: str, server_name: str = "", capability: str = ""):
        self.server_name = server_name
        self.capability = capability
        super().__init__(message)


class CortexValidationError(CortexException):
    """Validation agent found response below threshold."""
    def __init__(self, message: str, composite_score: Optional[float] = None):
        self.composite_score = composite_score
        super().__init__(message)


class CortexStorageError(CortexException):
    """Storage backend error."""
    pass


class CortexSecurityError(CortexException):
    """Security policy violation."""
    pass


class CortexLLMError(CortexException):
    """LLM provider error."""
    def __init__(self, message: str, provider: str = "", status_code: Optional[int] = None):
        self.provider = provider
        self.status_code = status_code
        super().__init__(message)


class CortexCycleError(CortexConfigError):
    """Circular dependency detected in task graph."""
    def __init__(self, cycle_path: List[str]):
        self.cycle_path = cycle_path
        path_str = " → ".join(cycle_path)
        super().__init__(f"Circular dependency detected: {path_str}", yaml_path="task_types")


class CortexMissingDependencyError(CortexConfigError):
    """depends_on references a non-existent task type."""
    def __init__(self, task_name: str, missing_dep: str, yaml_path: str = "", line: int = 0):
        super().__init__(
            f'Task type "{task_name}" depends_on "{missing_dep}" which is not defined in task_types.',
            yaml_path=yaml_path,
            line=line,
        )


class CortexProviderError(CortexException):
    """LLM provider not configured or unavailable."""
    def __init__(self, message: str, provider_name: str = ""):
        self.provider_name = provider_name
        super().__init__(message)


class CortexFileInputError(CortexException):
    """File input validation failed."""
    pass


class CortexQuotaError(CortexException):
    """Storage quota exceeded."""
    pass


class CortexDeltaError(CortexException):
    """Learning delta error."""
    pass


class CortexInvalidUserError(CortexException):
    """Raised when user_id is missing, empty, or invalid."""
    pass


class CortexAntError(CortexException):
    """Error in the ant colony subsystem."""
    def __init__(self, message: str, ant_name: str = ""):
        self.ant_name = ant_name
        super().__init__(message)
