"""TaskContext and LLMResponse dataclasses."""
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


@dataclass
class LLMResponse:
    content: str
    model: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    stop_reason: str = "end_turn"
    provider: str = "anthropic"


@dataclass
class TaskContext:
    """Context passed to scripted task handlers."""
    task_id: str
    session_id: str
    task_name: str
    instruction: str
    input_refs: List[str] = field(default_factory=list)
    context_hints: Dict[str, Any] = field(default_factory=dict)
    output_format: str = "text"
    timeout_seconds: int = 40
