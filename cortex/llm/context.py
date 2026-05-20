"""TaskContext and LLMResponse dataclasses."""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


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
    """Context passed to a code node / scripted task handler.

    The static fields (task_id, instruction, …) are always populated. The
    runtime-wiring fields below — :attr:`request`, :attr:`deps`, and the
    :meth:`llm` / :meth:`call_tool` helpers — are populated by the framework
    when the handler runs inside a live session. They let a code node reach
    the same LLM providers and MCP tool servers the rest of the agent uses.
    """
    task_id: str
    session_id: str
    task_name: str
    instruction: str
    input_refs: List[str] = field(default_factory=list)
    context_hints: Dict[str, Any] = field(default_factory=dict)
    output_format: str = "text"
    timeout_seconds: int = 40

    # ── Runtime wiring (populated by GenericMCPAgent for live sessions) ──────
    # The original user request for the session. In a static-DAG agent this is
    # the same value every node sees; nodes route off it plus ``deps``.
    request: str = ""
    # Identifier of the principal that owns the session ("" when anonymous).
    user_id: str = ""
    # Outputs of upstream nodes: {dependency_node_name: output_string}.
    deps: Dict[str, str] = field(default_factory=dict)

    # Private handles wired by the framework — use the methods below, not these.
    _llm_client: Any = None        # cortex.llm.client.LLMClient
    _llm_provider: str = "default"
    _tool_caller: Optional[Callable] = None  # async (server, tool, params) -> str

    async def llm(
        self,
        prompt: str,
        *,
        system: str = "You are a helpful assistant.",
        max_tokens: Optional[int] = None,
        provider: Optional[str] = None,
    ) -> str:
        """One-shot LLM completion using the agent's configured provider.

        ``provider`` defaults to the node's ``llm_provider`` (the agent's
        "default" provider unless overridden). Returns the completion text.
        """
        if self._llm_client is None:
            raise RuntimeError(
                "ctx.llm() is only available while the node runs inside a "
                "CortexFramework session"
            )
        resp = await self._llm_client.complete(
            messages=[{"role": "user", "content": prompt}],
            system=system,
            provider_name=provider or self._llm_provider,
            max_tokens=max_tokens,
        )
        return resp.content

    async def call_tool(self, server: str, tool: str, **params: Any) -> str:
        """Invoke an MCP tool on a configured tool server. Returns its output."""
        if self._tool_caller is None:
            raise RuntimeError(
                "ctx.call_tool() is only available while the node runs inside "
                "a CortexFramework session"
            )
        return await self._tool_caller(server, tool, params)
