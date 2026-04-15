"""run_handler() — utility for testing scripted task handlers."""
import asyncio
from typing import Any, Callable, Dict, Optional

from cortex.llm.context import TaskContext


async def run_handler(
    handler: Callable,
    task_name: str = "test_task",
    instruction: str = "Test instruction",
    session_id: str = "sess_test00",
    task_id: str = "sess_test00/000_test_task",
    input_refs: Optional[list] = None,
    context_hints: Optional[Dict[str, Any]] = None,
    output_format: str = "text",
) -> Any:
    """
    Run a scripted task handler function directly in a test.

    Args:
        handler: The async handler function to test
        task_name: Name of the task type
        instruction: The task instruction
        session_id: Session ID (default test value)
        task_id: Task ID (default test value)
        input_refs: Input references
        context_hints: Context hints dict
        output_format: Output format (text | md | json | file)

    Returns:
        The handler's return value

    Usage:
        async def my_handler(ctx: TaskContext) -> str:
            return f"Processed: {ctx.instruction}"

        result = await run_handler(my_handler, instruction="hello")
        assert result == "Processed: hello"
    """
    ctx = TaskContext(
        task_id=task_id,
        session_id=session_id,
        task_name=task_name,
        instruction=instruction,
        input_refs=input_refs or [],
        context_hints=context_hints or {},
        output_format=output_format,
    )
    if asyncio.iscoroutinefunction(handler):
        return await handler(ctx)
    return handler(ctx)
