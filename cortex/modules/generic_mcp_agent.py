"""GenericMCPAgent — universal stateless task executor."""
import asyncio
import importlib
import logging
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

from cortex.config.schema import TaskTypeConfig
from cortex.exceptions import CortexTaskError, CortexToolUnavailableError
from cortex.llm.client import LLMClient
from cortex.llm.context import TaskContext, TokenUsage
from cortex.modules.result_envelope_store import ResultEnvelope, ResultEnvelopeStore
from cortex.modules.signal_registry import SignalRegistry
from cortex.modules.task_graph_compiler import RuntimeTask
from cortex.modules.tool_server_registry import ToolServerRegistry
from cortex.security.bash_sandbox import BashSandbox
from cortex.security.scrubber import CredentialScrubber

logger = logging.getLogger(__name__)


async def _select_tool_for_task(
    capability_hint: str,
    task_tool_servers: List[str],
    registry: ToolServerRegistry,
):
    """
    Select the best tool server connection for a task.
    1. If task specifies tool_servers[], use those first.
    2. Match by capability_hint.
    3. If auto: use registry auto-classification.
    4. If ambiguous: use first available match.
    5. If no match: return None.

    Triggers lazy server initialization if eager_discovery=false was configured.
    """
    # Try explicitly-named servers first
    if task_tool_servers:
        for name in task_tool_servers:
            for srv in registry.list_servers():
                if srv.name == name:
                    await registry.ensure_server_ready(name)
                    srv = registry._servers.get(name, srv)
                    if srv.status.startswith("READY"):
                        c = registry._connections.get(name)
                        if c:
                            return c
    # Match by capability
    if capability_hint and capability_hint != "auto":
        conns = await registry.get_capability_servers(capability_hint)
        if conns:
            return conns[0]
    # Auto: return first available
    for cap in ["web_search", "document_generation", "image_generation", "bash", "llm_synthesis"]:
        conns = await registry.get_capability_servers(cap)
        if conns:
            logger.debug("Auto-selected capability '%s' for task", cap)
            return conns[0]
    return None


def _extract_field(text: str, field: str) -> Optional[str]:
    """Extract a labelled field from a structured instruction block.

    Matches ``FIELD: value`` or ``FIELD:\\nvalue`` patterns, returning the
    value up to (but not including) the next field or end of string.
    """
    pattern = re.compile(
        rf'^{re.escape(field)}:\s*(.+?)(?=\n[A-Z_]+:|\Z)',
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(text)
    return m.group(1).strip() if m else None


def _extract_content_summary(full_content: str, max_tokens: int) -> str:
    """
    Extract a compact excerpt bounded by max_tokens (approx 4 chars/token).
    Prefers beginning + does not truncate mid-sentence.
    """
    max_chars = max_tokens * 4
    if len(full_content) <= max_chars:
        return full_content

    truncated = full_content[:max_chars]
    # Find last sentence boundary
    for sep in (". ", ".\n", "! ", "? ", "\n\n"):
        idx = truncated.rfind(sep)
        if idx > max_chars // 2:
            return truncated[:idx + len(sep)].strip()

    return truncated.strip()


class GenericMCPAgent:
    """
    Universal task executor. Co-located with PrimaryAgent.
    Stateless per task — safe for parallel execution.
    Uses per-task llm_provider from task config (or default).
    All LLM calls are streaming.
    """

    def __init__(
        self,
        session_storage_path: str,
        scrubber: Optional[CredentialScrubber] = None,
        code_sandbox=None,       # cortex.sandbox.CodeSandbox instance (optional)
        code_store=None,         # cortex.sandbox.AgentCodeStore instance (optional)
        sandbox_config=None,     # cortex.config.schema.CodeSandboxConfig
        discovery_callback=None, # async callable(capability: str) -> bool
                                 # injected by CortexFramework; triggers CapabilityScout
                                 # mid-run when no tool server is found for a capability.
        workspace_bash=None,     # cortex.modules.workspace_bash.WorkspaceBash instance
        hitl_relay_url: Optional[str] = None,  # URL of per-session HITL relay (for ant calls)
        builtin_web_search_enabled: bool = True,
    ):
        self._session_storage_path = session_storage_path
        self._scrubber = scrubber or CredentialScrubber()
        self._code_sandbox = code_sandbox
        self._code_store = code_store
        self._sandbox_config = sandbox_config
        self._discovery_callback = discovery_callback
        self._workspace_bash = workspace_bash
        self._hitl_relay_url = hitl_relay_url
        self._builtin_web_search_enabled = builtin_web_search_enabled

    async def execute_task(
        self,
        task: RuntimeTask,
        tool_registry: ToolServerRegistry,
        llm_client: LLMClient,
        envelope_store: ResultEnvelopeStore,
        signal_registry: SignalRegistry,
        config: TaskTypeConfig,
        event_queue=None,
    ) -> ResultEnvelope:
        """
        Full task execution pipeline with retry logic.
        event_queue is forwarded to _execute_once for code_exec consent events.
        """
        max_attempts = config.retry.max_attempts
        backoff_ms = config.retry.backoff_initial_ms

        for attempt in range(1, max_attempts + 1):
            try:
                envelope = await asyncio.wait_for(
                    self._execute_once(task, tool_registry, llm_client, envelope_store, config, event_queue=event_queue),
                    timeout=config.timeout_seconds,
                )
                await envelope_store.write_envelope(envelope)
                signal_registry.fire_signal(task.task_id.split("/")[0], task.task_id)
                return envelope
            except asyncio.TimeoutError:
                logger.warning("Task %s timed out (attempt %d/%d)", task.task_id, attempt, max_attempts)
                if attempt == max_attempts:
                    envelope = ResultEnvelope(
                        task_id=task.task_id,
                        session_id=task.task_id.split("/")[0],
                        status="timeout",
                        mandatory=task.mandatory,
                        error=f"Task timed out after {max_attempts} attempts",
                    )
                    await envelope_store.write_envelope(envelope)
                    signal_registry.fire_signal(task.task_id.split("/")[0], task.task_id)
                    return envelope
                await asyncio.sleep(backoff_ms / 1000 * attempt)
            except Exception as e:
                logger.error("Task %s failed (attempt %d/%d): %s", task.task_id, attempt, max_attempts, e)
                if attempt == max_attempts:
                    envelope = ResultEnvelope(
                        task_id=task.task_id,
                        session_id=task.task_id.split("/")[0],
                        status="failed",
                        mandatory=task.mandatory,
                        error=str(e),
                    )
                    await envelope_store.write_envelope(envelope)
                    signal_registry.fire_signal(task.task_id.split("/")[0], task.task_id)
                    return envelope
                await asyncio.sleep(backoff_ms / 1000 * attempt)

    async def ask_human(
        self,
        task: RuntimeTask,
        question: str,
        event_queue,
        context: Optional[str] = None,
        timeout_seconds: int = 300,
    ) -> Optional[str]:
        """Pause this sub-agent and ask the user a clarification question.

        Contract:
          - Only permitted when `task.config.human_in_loop` is True. If the
            task type disables HITL, this method returns None without emitting
            any event (the sub-agent must then make its best guess).
          - Blocks on an asyncio.Event until the user answers via
            CortexFramework.resolve_task_clarification(), or until timeout.
          - Other sub-agents in the same wave keep running while this one
            waits — the wave loop joins on asyncio.gather so it will naturally
            wait for this task to complete before dispatching the next wave.

        Returns the user's answer string, or None if HITL is disabled,
        no event queue is available, or the wait times out.
        """
        if not task.config or not task.config.human_in_loop:
            return None
        if event_queue is None:
            return None

        # When running inside an ant subprocess, relay through the parent's HITL relay.
        import os as _os
        hitl_url = _os.environ.get("CORTEX_HITL_URL")
        if hitl_url:
            return await self._relay_hitl(hitl_url, question, task)

        # Local imports avoid a circular import at module load time.
        import uuid
        from cortex.framework import _PENDING_TASK_CLARIFICATIONS
        from cortex.streaming.status_events import ClarificationRequestEvent

        session_id = task.task_id.split("/")[0]
        clarification_id = f"hitl_{task.task_id.replace('/', '_')}_{uuid.uuid4().hex[:6]}"
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
            task_id=task.task_id,
            task_name=task.task_name,
            context=context,
        ))

        try:
            await asyncio.wait_for(wait_event.wait(), timeout=timeout_seconds)
            entry = _PENDING_TASK_CLARIFICATIONS.pop(clarification_id, {}) or {}
            return entry.get("answer")
        except asyncio.TimeoutError:
            _PENDING_TASK_CLARIFICATIONS.pop(clarification_id, None)
            logger.info(
                "HITL clarification timed out for task %s after %ds",
                task.task_id, timeout_seconds,
            )
            return None

    @staticmethod
    async def _relay_hitl(hitl_url: str, question: str, task) -> Optional[str]:
        """Relay a HITL question to the parent framework when running in an ant subprocess."""
        import aiohttp

        payload = {
            "session_id": task.task_id.split("/")[0],
            "question": question,
            "task_id": task.task_id,
        }
        try:
            timeout = aiohttp.ClientTimeout(total=310)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(f"{hitl_url}/hitl", json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("answer")
        except Exception as exc:
            logger.warning(
                "HITL relay to %s failed for task %s: %s — auto-denying",
                hitl_url, task.task_id, exc,
            )
        return None

    async def _call_workspace_bash(
        self,
        task: RuntimeTask,
        instruction: str,
        session_id: str,
        event_queue,
    ) -> str:
        """Dispatch a workspace_bash task: parse the operation from the instruction,
        then call the appropriate WorkspaceBash method.

        Expects the instruction to encode the operation in one of these forms::

            ACTION: <verb>
            PATH: <rel_path>
            WORKSPACE: /abs/path/to/workspace   (optional — overrides default_workspace)
            [CONTENT: <file content for write operations>]
            [COMMAND: <shell command for execute operations>]

        If WORKSPACE is absent, falls back to WorkspaceBash._default_workspace (set via
        the UI sidebar, CORTEX_DEFAULT_WORKSPACE env var, or workspace_bash.default_workspace
        in cortex.yaml).
        """
        if self._workspace_bash is None:
            return "[workspace_bash not enabled — add workspace_bash.enabled: true to cortex.yaml]"

        # Update the workspace_bash event_queue for this session
        self._workspace_bash._event_queue = event_queue

        # Parse structured fields
        action = _extract_field(instruction, "ACTION")
        rel_path = _extract_field(instruction, "PATH")
        workspace = _extract_field(instruction, "WORKSPACE") or self._workspace_bash._default_workspace
        content = _extract_field(instruction, "CONTENT")
        command = _extract_field(instruction, "COMMAND")

        # Free-form fallback: treat whole instruction as a shell command
        if not action:
            if not workspace:
                return (
                    "[workspace_bash: no workspace set — configure it in the Synapse sidebar, "
                    "set CORTEX_DEFAULT_WORKSPACE, or add workspace_bash.default_workspace to cortex.yaml]"
                )
            command = command or instruction
            action = "execute"

        if not workspace:
            return (
                "[workspace_bash: no workspace set — configure it in the Synapse sidebar, "
                "set CORTEX_DEFAULT_WORKSPACE, or add workspace_bash.default_workspace to cortex.yaml]"
            )

        verb = (action or "").strip().lower()
        try:
            if verb in ("read", "read_file"):
                result = await self._workspace_bash.read_file(workspace, rel_path or ".")
                await self._workspace_bash._emit_workspace_event(session_id, task, "read", rel_path or ".")
                return result
            elif verb in ("list", "list_dir", "ls"):
                result = await self._workspace_bash.list_dir(workspace, rel_path or ".")
                await self._workspace_bash._emit_workspace_event(session_id, task, "listed", rel_path or ".", is_dir=True)
                return result
            elif verb in ("write", "write_file"):
                if not content:
                    return "[workspace_bash write: no CONTENT provided]"
                result = await self._workspace_bash.write_file(
                    workspace, rel_path or "output.txt", content, task, session_id
                )
                await self._workspace_bash._emit_workspace_event(session_id, task, "modified", rel_path or "output.txt")
                return result
            elif verb in ("execute", "exec", "run", "bash"):
                cmd = command or instruction
                result = await self._workspace_bash.execute(workspace, cmd, task, session_id)
                await self._workspace_bash._emit_workspace_event(session_id, task, "executed", workspace)
                return result
            else:
                # Unknown verb — treat entire instruction as a command
                result = await self._workspace_bash.execute(workspace, instruction, task, session_id)
                await self._workspace_bash._emit_workspace_event(session_id, task, "executed", workspace)
                return result
        except Exception as exc:
            from cortex.exceptions import CortexHITLDeniedError
            if isinstance(exc, CortexHITLDeniedError):
                return f"[workspace_bash: operation denied by user — {exc}]"
            logger.error("workspace_bash error for task %s: %s", task.task_id, exc)
            return f"[workspace_bash error: {exc}]"

    async def _execute_once(
        self,
        task: RuntimeTask,
        tool_registry: ToolServerRegistry,
        llm_client: LLMClient,
        envelope_store: ResultEnvelopeStore,
        config: TaskTypeConfig,
        event_queue=None,
        **kwargs,
    ) -> ResultEnvelope:
        """Single execution attempt."""
        start_ms = int(time.time() * 1000)
        session_id = task.task_id.split("/")[0]
        tool_trace = []
        kwargs["event_queue"] = event_queue
        # Derive available capabilities from the registry for LLM context
        available_capabilities = list(tool_registry._capability_map.keys()) + ["llm_synthesis", "workspace_bash"]

        # Log principal identity for audit trail
        if task.principal:
            logger.info(
                "Task %s executing as principal %s (type=%s%s)",
                task.task_id,
                task.principal.principal_id,
                task.principal.principal_type,
                f", delegated_by={task.principal.delegation_chain}" if task.principal.is_delegated else "",
            )

        # Resolve input_refs from storage
        input_context = ""
        for ref in task.input_refs:
            parts = ref.split(":")
            ref_session = parts[0] if len(parts) > 1 else session_id
            ref_task_id = parts[-1]
            # Security: only allow refs within same session
            if ref_session != session_id:
                logger.warning("Cross-session input_ref rejected: %s", ref)
                continue
            ref_envelope = await envelope_store.read_envelope(ref_session, ref_task_id)
            if ref_envelope:
                input_context += f"\n\n[Input from {ref_task_id}]:\n{ref_envelope.content_summary}"

        # Build full instruction
        full_instruction = task.instruction
        if input_context:
            full_instruction += f"\n\nContext from prior tasks:{input_context}"

        # Retry-with-feedback: if the wave validation gate re-queued this task
        # with analysis of what went wrong on the previous attempt, surface it
        # so the sub-agent can correct itself instead of blindly re-running.
        if task.validation_feedback:
            full_instruction += (
                "\n\n[RETRY FEEDBACK — the previous attempt failed validation]\n"
                f"{task.validation_feedback}\n"
                "Fix the issues described above in this attempt."
            )

        output_content = ""
        output_type = config.output_format
        generated_script: Optional[str] = None   # populated for code_exec tasks
        forged_server_path: Optional[str] = None  # populated for forge_mcp tasks
        token_usage = TokenUsage()

        # Scripted handler
        if config.complexity == "scripted" and config.handler:
            output_content, output_type = await self._call_handler(
                config.handler,
                task, full_instruction, config,
            )
            tool_trace.append(f"handler:{config.handler}")

        # Code execution sandbox
        elif config.capability_hint == "code_exec":
            output_content, generated_script = await self._call_code_exec(
                task=task,
                instruction=full_instruction,
                config=config,
                llm_client=llm_client,
                tool_trace=tool_trace,
                event_queue=kwargs.get("event_queue"),
            )

        # ToolForge — generate a new MCP server script and stage it for wave-boundary registration
        elif config.capability_hint == "forge_mcp":
            output_content, forged_server_path = await self._call_forge_mcp(
                task=task,
                instruction=full_instruction,
                config=config,
                llm_client=llm_client,
                tool_trace=tool_trace,
                event_queue=kwargs.get("event_queue"),
            )

        # Bash capability
        elif config.capability_hint == "bash":
            sandbox = BashSandbox(self._session_storage_path)
            output_content = await sandbox.execute(full_instruction)
            tool_trace.append("bash_sandbox")

        # Workspace bash — reads/writes/executes in the user's own workspace directory
        elif config.capability_hint == "workspace_bash":
            output_content = await self._call_workspace_bash(
                task=task,
                instruction=full_instruction,
                session_id=session_id,
                event_queue=event_queue,
            )
            tool_trace.append("workspace_bash")

        # LLM synthesis
        elif config.capability_hint == "llm_synthesis":
            output_content, token_usage = await self._call_llm(
                task_id=task.task_id,
                instruction=full_instruction,
                config=config,
                llm_client=llm_client,
                tool_trace=tool_trace,
                task=task,
                event_queue=event_queue,
                available_capabilities=available_capabilities,
            )

        # Web search — try configured tool server first, fall back to built-in DuckDuckGo
        elif config.capability_hint == "web_search":
            conn = await _select_tool_for_task("web_search", config.tool_servers, tool_registry)
            if conn:
                try:
                    output_content = await self.call_tool_server(
                        server_name=conn.server_name,
                        tool_name=config.capability_hint,
                        params={"instruction": full_instruction, "task_id": task.task_id},
                        tool_registry=tool_registry,
                    )
                    tool_trace.append(f"tool:{conn.server_name}")
                except Exception as e:
                    logger.warning("Configured web_search server failed (%s) — using built-in DDG", e)
                    conn = None
            if not conn:
                if not self._builtin_web_search_enabled:
                    output_content = "Web search is disabled. Configure a web_search tool server or enable builtin_web_search_enabled in the agent config."
                    tool_trace.append("builtin:duckduckgo:disabled")
                else:
                    from cortex.modules.builtin_search import DuckDuckGoSearch
                    output_content = await DuckDuckGoSearch().search(full_instruction)
                    tool_trace.append("builtin:duckduckgo")

        # Tool server call (document_generation, image_generation, auto, etc.)
        else:
            conn = await _select_tool_for_task(
                config.capability_hint,
                config.tool_servers,
                tool_registry,
            )
            if conn is None and self._discovery_callback and config.capability_hint not in (
                "llm_synthesis", "bash", "code_exec", "auto"
            ):
                # No internal tool found — ask the scout to search for an external MCP
                # before falling back to LLM synthesis.
                logger.info(
                    "Task %s: no tool for '%s' — triggering mid-run external discovery",
                    task.task_id, config.capability_hint,
                )
                try:
                    discovered = await self._discovery_callback(config.capability_hint)
                    if discovered:
                        # Re-attempt tool selection with the newly registered server
                        conn = await _select_tool_for_task(
                            config.capability_hint,
                            config.tool_servers,
                            tool_registry,
                        )
                except Exception as disc_err:
                    logger.warning(
                        "Mid-run discovery callback failed for task %s: %s",
                        task.task_id, disc_err,
                    )

            if conn:
                if event_queue:
                    from cortex.streaming.status_events import TaskToolCallEvent
                    await event_queue.put(TaskToolCallEvent(
                        session_id=session_id,
                        task_id=task.task_id,
                        task_name=task.task_name,
                        tool_name=config.capability_hint,
                        tool_input={"server": conn.server_name},
                    ))
                tool_result = await self.call_tool_server(
                    server_name=conn.server_name,
                    tool_name=config.capability_hint,
                    params={"instruction": full_instruction, "task_id": task.task_id},
                    tool_registry=tool_registry,
                )
                tool_trace.append(f"tool:{conn.server_name}")
                # If tool returned instructions (not data), make an LLM call
                if tool_result.startswith("INSTRUCTIONS:"):
                    output_content, token_usage = await self._call_llm(
                        task_id=task.task_id,
                        instruction=tool_result[len("INSTRUCTIONS:"):].strip(),
                        config=config,
                        llm_client=llm_client,
                        tool_trace=tool_trace,
                        task=task,
                        event_queue=event_queue,
                        available_capabilities=available_capabilities,
                    )
                else:
                    output_content = tool_result
            else:
                # No tool server available — fall back to LLM
                logger.warning(
                    "No tool server for capability '%s' on task %s — falling back to LLM",
                    config.capability_hint, task.task_id,
                )
                output_content, token_usage = await self._call_llm(
                    task_id=task.task_id,
                    instruction=full_instruction,
                    config=config,
                    llm_client=llm_client,
                    tool_trace=tool_trace,
                    task=task,
                    event_queue=event_queue,
                    available_capabilities=available_capabilities,
                )

        # Scrub credentials from output
        output_content = self._scrubber.scrub(output_content)

        # Extract bounded content summary
        summary_tokens = config.output.content_summary_tokens
        content_summary = _extract_content_summary(output_content, summary_tokens)

        duration_ms = int(time.time() * 1000) - start_ms

        return ResultEnvelope(
            task_id=task.task_id,
            task_name=task.task_name,
            session_id=session_id,
            status="complete",
            mandatory=task.mandatory,
            output_type=output_type,
            output_value=output_content,
            content_summary=content_summary,
            duration_ms=duration_ms,
            tool_trace=tool_trace,
            context_hints=task.context_hints,
            token_usage=token_usage,
            generated_script=generated_script,
            forged_server_path=forged_server_path,
            is_adhoc=task.is_adhoc,
        )

    async def _call_llm(
        self,
        task_id: str,
        instruction: str,
        config: TaskTypeConfig,
        llm_client: LLMClient,
        tool_trace: List[str],
        task: Optional[RuntimeTask] = None,
        event_queue=None,
        available_capabilities: Optional[List[str]] = None,
    ) -> tuple[str, TokenUsage]:
        """Make a streaming LLM call for this task. Returns (content, token_usage).

        When `task.config.human_in_loop` is True and an `event_queue` is
        available, the sub-agent is allowed to emit `<ask_human>question</ask_human>`
        mid-stream to request clarification from the user. On detection, the
        stream is interrupted, `ask_human()` is called, the Q&A pair is appended
        to the conversation, and the LLM is re-invoked. This loop is capped at
        3 asks per task attempt via `task.hitl_ask_count`.
        """
        import re as _re

        provider_name = config.llm_provider or "default"
        caps_note = ""
        if available_capabilities:
            caps_note = (
                f" Agent capabilities available: {', '.join(sorted(available_capabilities))}."
            )
        system = (
            f"You are executing a '{config.name}' task as part of an AI agent.{caps_note} "
            f"Output format: {config.output_format}. "
            f"{config.description} "
            f"Generate the requested content directly and completely. "
            f"If the task involves creating a document, PDF, report, or any file, "
            f"produce the full content as your output — the framework handles saving it to disk. "
            f"Never refuse by saying you cannot create files or access the internet; "
            f"just produce the best output you can for this task."
        )

        hitl_enabled = (
            task is not None
            and config.human_in_loop
            and event_queue is not None
        )
        if hitl_enabled:
            system += (
                "\n\n## Human-in-the-Loop\n"
                "If anything in the task is ambiguous or you are missing information "
                "you need to proceed confidently, DO NOT GUESS. Instead, ask the user a "
                "single focused question by emitting EXACTLY this tag and then stopping "
                "your output immediately:\n"
                "<ask_human>your concise question here</ask_human>\n"
                "The system will pause execution, get the answer, and restart you with "
                "the answer included in the conversation. You may ask up to 3 questions "
                "per attempt. Only ask when necessary; prefer acting on clear instructions."
            )

        tool_trace.append(f"llm:{provider_name}")

        ask_pattern = _re.compile(r"<ask_human>(.*?)</ask_human>", _re.DOTALL | _re.IGNORECASE)
        conversation: List[Dict[str, str]] = [{"role": "user", "content": instruction}]
        total_input_chars = len(instruction)
        total_output_chars = 0
        final_content = ""
        MAX_ASKS = 3

        while True:
            tokens: List[str] = []
            accumulated = ""
            async for token in llm_client.stream(
                messages=conversation,
                system=system,
                provider_name=provider_name,
            ):
                tokens.append(token)
                accumulated += token
                if hitl_enabled and "</ask_human>" in accumulated.lower():
                    break

            content = "".join(tokens)
            total_output_chars += len(content)

            match = ask_pattern.search(content) if hitl_enabled else None
            if not match:
                final_content = content
                break

            if task.hitl_ask_count >= MAX_ASKS:
                logger.info(
                    "Task %s hit HITL ask cap (%d) — instructing agent to proceed",
                    task.task_id, MAX_ASKS,
                )
                pre_ask = content[:match.start()].strip()
                conversation.append({"role": "assistant", "content": pre_ask or "(asking for clarification)"})
                conversation.append({
                    "role": "user",
                    "content": (
                        "You have reached the maximum number of clarification questions "
                        "for this attempt. Proceed using your best interpretation of the "
                        "original instruction. Do not emit any more <ask_human> tags."
                    ),
                })
                continue

            question = match.group(1).strip()
            task.hitl_ask_count += 1
            logger.info(
                "Task %s requesting HITL clarification (%d/%d): %s",
                task.task_id, task.hitl_ask_count, MAX_ASKS, question[:120],
            )
            answer = await self.ask_human(
                task=task,
                question=question,
                event_queue=event_queue,
                context=None,
            )
            if not answer:
                answer = (
                    "(No answer received — proceed using your best interpretation "
                    "of the original instruction and do not ask again.)"
                )

            pre_ask = content[:match.start()].strip()
            conversation.append({
                "role": "assistant",
                "content": pre_ask + f"\n<ask_human>{question}</ask_human>",
            })
            conversation.append({
                "role": "user",
                "content": f"[Answer to your question]\n{answer}\n\nNow continue the task with this information.",
            })
            total_input_chars += len(answer) + len(question)

        input_tokens = total_input_chars // 4
        output_tokens = total_output_chars // 4
        usage = TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        )
        # Strip any stray <ask_human> residue from final content
        final_content = ask_pattern.sub("", final_content).strip()
        return final_content, usage

    async def _call_handler(
        self,
        handler_path: str,
        task: RuntimeTask,
        instruction: str,
        config: TaskTypeConfig,
    ) -> tuple[str, str]:
        """Call a scripted handler function."""
        parts = handler_path.rsplit(".", 1)
        if len(parts) != 2:
            raise CortexTaskError(f"Invalid handler path: {handler_path}", task_id=task.task_id)
        module_path, fn_name = parts
        try:
            module = importlib.import_module(module_path)
            fn = getattr(module, fn_name)
        except (ImportError, AttributeError) as e:
            raise CortexTaskError(f"Cannot load handler {handler_path}: {e}", task_id=task.task_id)

        ctx = TaskContext(
            task_id=task.task_id,
            session_id=task.task_id.split("/")[0],
            task_name=task.task_name,
            instruction=instruction,
            input_refs=task.input_refs,
            context_hints=task.context_hints,
            output_format=config.output_format,
        )
        result = await fn(ctx)
        if result is None:
            return "", config.output_format
        if isinstance(result, tuple):
            return result[0] or "", result[1] if len(result) > 1 else config.output_format
        return str(result), config.output_format

    async def call_tool_server(
        self,
        server_name: str,
        tool_name: str,
        params: Dict,
        tool_registry: ToolServerRegistry,
    ) -> str:
        """Call an MCP tool server endpoint (HTTP or stdio)."""
        conn = tool_registry._connections.get(server_name)
        if not conn:
            raise CortexToolUnavailableError(
                f"Tool server '{server_name}' has no active connection",
                server_name=server_name,
            )
        info = tool_registry._servers.get(server_name)
        if not info or not info.status.startswith("READY"):
            raise CortexToolUnavailableError(
                f"Tool server '{server_name}' is not ready (status: {info.status if info else 'unknown'})",
                server_name=server_name,
            )

        # ── stdio transport ────────────────────────────────────────────────────
        if conn.stdio_session is not None:
            return await self._call_stdio_tool_server(conn, info, tool_name, params, tool_registry)

        # ── HTTP transport ─────────────────────────────────────────────────────
        if not conn.session:
            raise CortexToolUnavailableError(
                f"Tool server '{server_name}' has no HTTP session",
                server_name=server_name,
            )
        base_url = info.url
        if not base_url:
            raise CortexToolUnavailableError(f"Tool server '{server_name}' has no URL", server_name=server_name)

        # Inject HITL relay URL into ant calls so the ant can relay HITL back
        invoke_params = dict(params)
        if info.trust_tier == "ant" and self._hitl_relay_url:
            invoke_params["hitl_url"] = self._hitl_relay_url

        try:
            async with conn.session.post(
                f"{base_url}/tools/{tool_name}/invoke",
                json={"params": invoke_params},
            ) as resp:
                if resp.status >= 400:
                    raise CortexToolUnavailableError(
                        f"Tool server '{server_name}' returned HTTP {resp.status}",
                        server_name=server_name,
                    )
                content_type = resp.headers.get("Content-Type", "")
                result = await resp.json(content_type=None)
                raw_content = result.get("content", result.get("result", str(result)))
                filename = result.get("filename") if isinstance(result, dict) else None

                # External servers: run output through MCPOutputGuard before returning.
                # Internal servers: no-op (apply_output_guard checks trust_tier).
                try:
                    safe_content = tool_registry.apply_output_guard(
                        server_name,
                        str(raw_content),
                        content_type=content_type,
                        filename=filename,
                    )
                except Exception as guard_err:
                    raise CortexToolUnavailableError(
                        f"Tool server '{server_name}' output failed safety check: {guard_err}",
                        server_name=server_name,
                    )

                return self._scrubber.scrub(safe_content)
        except CortexToolUnavailableError:
            raise
        except Exception as e:
            raise CortexToolUnavailableError(
                f"Tool server '{server_name}' call failed: {e}",
                server_name=server_name,
            )

    async def _call_stdio_tool_server(
        self,
        conn,
        info,
        capability_hint: str,
        params: Dict,
        tool_registry,
    ) -> str:
        """Call a stdio MCP tool server, mapping capability_hint → actual tool name + args."""
        instruction = params.get("instruction", "")

        # Find the actual tool name that matches this capability
        actual_tool = capability_hint
        if info.tools:
            for t in info.tools:
                if capability_hint.lower() in t.name.lower() or t.name.lower() in capability_hint.lower():
                    actual_tool = t.name
                    break
            else:
                # Default: first tool on the server
                actual_tool = info.tools[0].name

        # Map Cortex instruction → MCP tool arguments based on tool schema
        arguments = self._map_instruction_to_args(actual_tool, instruction, info.tools)

        try:
            result = await conn.stdio_session.call_tool(actual_tool, arguments)
            return tool_registry.apply_output_guard(conn.server_name, result)
        except Exception as e:
            from cortex.exceptions import CortexToolUnavailableError
            raise CortexToolUnavailableError(
                f"stdio tool '{actual_tool}' on '{conn.server_name}' failed: {e}",
                server_name=conn.server_name,
            )

    @staticmethod
    def _map_instruction_to_args(tool_name: str, instruction: str, tools) -> Dict:
        """Map a natural-language instruction to the MCP tool's argument schema."""
        # Find the schema for this tool
        schema: Dict = {}
        for t in (tools or []):
            if t.name == tool_name:
                schema = t.input_schema or {}
                break

        props = schema.get("properties", {})

        # Common search tools: map first string property to the instruction
        search_props = [p for p in props if p in ("query", "q", "search", "text", "input")]
        if search_props:
            return {search_props[0]: instruction[:500]}

        # Fetch/URL tools
        url_props = [p for p in props if p in ("url", "uri", "href", "link")]
        if url_props:
            import re as _re
            url_match = _re.search(r'https?://\S+', instruction)
            return {url_props[0]: url_match.group(0) if url_match else instruction}

        # Fallback: use first required property or first property
        required = schema.get("required", [])
        first_prop = (required or list(props.keys()) or ["query"])[0]
        return {first_prop: instruction[:500]}

    async def execute_bash(self, command: str, session_storage_path: str) -> str:
        """Execute bash command within security sandbox."""
        sandbox = BashSandbox(session_storage_path)
        return await sandbox.execute(command)

    async def _call_forge_mcp(
        self,
        task: RuntimeTask,
        instruction: str,
        config: TaskTypeConfig,
        llm_client: LLMClient,
        tool_trace: List[str],
        event_queue=None,
    ) -> tuple[str, Optional[str]]:
        """Generate and write a FastMCP-compatible MCP server script (ToolForge path).

        Reuses the code sandbox for LLM code generation. Unlike ``_call_code_exec``,
        the script is written to a **stable named path** under
        ``{storage_base}/ants/{task_name}/server.py`` rather than the session-ephemeral
        ``code_output/`` directory, so ``AntColony.hatch_from_script()`` can find and
        spawn it at the wave boundary after this task completes.

        The generated script must:
        - Use FastMCP and bind to ``CORTEX_ANT_PORT`` env var
        - Expose a ``/health`` endpoint returning HTTP 200
        - Define at least one ``@mcp.tool()`` decorated function

        Returns ``(status_message, script_path)`` on success or raises
        ``CortexTaskError`` on codegen/sandbox failure.
        ``script_path`` is ``None`` only if an unexpected error prevented the write.
        """
        from cortex.streaming.status_events import StatusEvent, EventType

        session_id = task.task_id.split("/")[0]
        task_name = task.task_name

        if not self._code_sandbox:
            raise CortexTaskError(
                "forge_mcp capability requires code_sandbox to be enabled in cortex.yaml "
                "(set code_sandbox.enabled: true)",
                task_id=task.task_id,
                task_name=task_name,
            )

        if event_queue:
            await event_queue.put(StatusEvent(
                message=f"Generating MCP server code for '{task_name}'...",
                session_id=session_id,
                event_type=EventType.STATUS,
            ))

        # Stable output path — lives under ants/<task_name>/ so AntColony.hatch_from_script()
        # can find it without the caller needing to pass a path explicitly.
        storage_base = Path(self._session_storage_path).parent.parent
        server_dir = storage_base / "ants" / task_name
        server_dir.mkdir(parents=True, exist_ok=True)
        script_path = server_dir / "server.py"

        # Augment the developer/LLM instruction with the structural requirements
        # the framework needs to spawn and health-check the generated server.
        forge_instruction = (
            f"{instruction}\n\n"
            "--- ToolForge structural requirements (mandatory) ---\n"
            "Generate a complete, runnable Python MCP server using FastMCP.\n"
            "Requirements:\n"
            "  1. `from mcp.server.fastmcp import FastMCP` and `mcp = FastMCP('<name>')`\n"
            f"     Use name: {task_name}\n"
            "  2. Read the port from env: `int(os.environ.get('CORTEX_ANT_PORT', 8080))`\n"
            "  3. Expose a /health route returning HTTP 200 (use a background thread or\n"
            "     aiohttp alongside FastMCP if needed).\n"
            "  4. Define all tool functions with the `@mcp.tool()` decorator.\n"
            "  5. End with:\n"
            "     `if __name__ == '__main__':\n"
            "         import os\n"
            f"        mcp.run(transport='sse', port=int(os.environ.get('CORTEX_ANT_PORT', 8080)))`\n"
            "  6. No placeholders — the file must run as-is with `python server.py`.\n"
        )

        source_code, result = await self._code_sandbox.generate_and_execute(
            task_name=task_name,
            description=config.description,
            instruction=forge_instruction,
            output_format="text",
            task_input={"instruction": forge_instruction, "output_dir": str(server_dir)},
            session_id=session_id,
            output_dir=str(server_dir),
            llm_client=llm_client,
        )
        tool_trace.append("sandbox:forge_codegen")

        if result.exit_code != 0:
            raise CortexTaskError(
                f"Forge codegen/validation failed: {result.error or result.stderr}",
                task_id=task.task_id,
                task_name=task_name,
            )

        script_path.write_text(source_code, encoding="utf-8")
        tool_trace.append(f"forge:wrote:{script_path}")
        logger.info("ToolForge: server script written to %s for task '%s'", script_path, task_name)

        return (
            f"MCP server script generated at {script_path}. "
            "Will be spawned and registered at wave boundary.",
            str(script_path),
        )

    async def _call_code_exec(
        self,
        task: RuntimeTask,
        instruction: str,
        config: TaskTypeConfig,
        llm_client: LLMClient,
        tool_trace: List[str],
        event_queue=None,
    ) -> tuple[str, Optional[str]]:
        """
        Code execution flow:
        1. Check AgentCodeStore for an existing persisted script.
        2. If found → run it directly (skip LLM codegen).
        3. If not found → ask LLM to generate code → execute in sandbox.
        4. Return (output_text, generated_source_code_or_None).
           generated_source_code is None when a cached (already-persisted) script was used.
           The caller stores source_code in the ResultEnvelope; consent is handled
           end-of-session by the LearningEngine — NOT here.
        """
        from cortex.streaming.status_events import StatusEvent, EventType

        session_id = task.task_id.split("/")[0]
        task_name = task.task_name

        # If the instruction mentions a user workspace path, write code there so the
        # file lands exactly where the user asked. Matches patterns like:
        #   "workspace folder is: /some/path"  "workspace_path: /some/path"
        # Falls back to the managed session path when no valid absolute path is found.
        _ws_match = re.search(
            r"workspace[_\s]*(?:folder|path|dir(?:ectory)?)?\s*(?:is\s*)?[:\s]+([/~][^\s\n,;]+)",
            instruction,
            re.IGNORECASE,
        )
        if _ws_match:
            candidate = _ws_match.group(1).rstrip(".,;")
            output_dir = candidate if Path(candidate).is_absolute() else str(
                Path(self._session_storage_path) / "code_output" / task.task_id.replace("/", "_")
            )
        else:
            output_dir = str(
                Path(self._session_storage_path) / "code_output" / task.task_id.replace("/", "_")
            )

        # ── Step 1: check for persisted script ───────────────────────────────
        if self._code_store and self._code_store.has_script(task_name):
            cached = self._code_store.get_script(task_name)
            if cached:
                source_code, record = cached
                logger.info("Reusing persisted script for task '%s' (used %d times)", task_name, record.use_count)
                tool_trace.append(f"code_store:{task_name}")

                if event_queue:
                    await event_queue.put(StatusEvent(
                        message=f"Reusing saved script for '{task_name}'...",
                        session_id=session_id,
                        event_type=EventType.STATUS,
                    ))

                # Install requirements if any
                if record.requirements and self._code_sandbox:
                    await self._code_sandbox.install_requirements(record.requirements)

                result = await self._code_sandbox.execute(
                    source_code=source_code,
                    task_input={"instruction": instruction, "output_dir": output_dir},
                    session_id=session_id,
                    output_dir=output_dir,
                )
                tool_trace.append("sandbox:cached")

                if result.error:
                    logger.warning("Cached script failed for '%s': %s — regenerating", task_name, result.error)
                    # Fall through to regenerate below
                else:
                    output = result.stdout
                    if result.output_files:
                        output += f"\n\nOutput files: {', '.join(result.output_files)}"
                    # None for source_code — already persisted, no new consent needed
                    return output, None

        # ── Step 2: generate and execute new code ─────────────────────────────
        if not self._code_sandbox:
            raise CortexTaskError(
                "code_exec capability requires code_sandbox to be enabled in cortex.yaml "
                "(set code_sandbox.enabled: true)",
                task_id=task.task_id,
                task_name=task_name,
            )

        if event_queue:
            await event_queue.put(StatusEvent(
                message=f"Generating Python code for '{task_name}'...",
                session_id=session_id,
                event_type=EventType.STATUS,
            ))

        source_code, result = await self._code_sandbox.generate_and_execute(
            task_name=task_name,
            description=config.description,
            instruction=instruction,
            output_format=config.output_format,
            task_input={"instruction": instruction, "output_dir": output_dir},
            session_id=session_id,
            output_dir=output_dir,
            llm_client=llm_client,
        )
        tool_trace.append("sandbox:generated")

        if result.exit_code != 0:
            raise CortexTaskError(
                f"Sandbox execution failed: {result.error or result.stderr}",
                task_id=task.task_id,
                task_name=task_name,
            )

        output = result.stdout
        if result.output_files:
            output += f"\n\nOutput files: {', '.join(result.output_files)}"

        # Return the generated source_code so the caller can store it in the
        # ResultEnvelope. End-of-session consent is handled by LearningEngine.
        return output, source_code
