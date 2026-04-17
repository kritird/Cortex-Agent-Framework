"""GenericMCPAgent — universal stateless task executor."""
import asyncio
import importlib
import logging
import time
from typing import Any, Dict, List, Optional

from cortex.config.schema import TaskTypeConfig
from cortex.exceptions import CortexTaskError, CortexTaskTimeoutError, CortexToolUnavailableError
from cortex.llm.client import LLMClient
from cortex.llm.context import TaskContext, TokenUsage
from cortex.modules.result_envelope_store import ResultEnvelope, ResultEnvelopeStore, TaskEnvelope
from cortex.modules.signal_registry import SignalRegistry
from cortex.modules.task_graph_compiler import RuntimeTask
from cortex.modules.tool_server_registry import ToolServerRegistry
from cortex.security.bash_sandbox import BashSandbox
from cortex.security.scrubber import CredentialScrubber
from cortex.storage.base import StorageBackend

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
    ):
        self._session_storage_path = session_storage_path
        self._scrubber = scrubber or CredentialScrubber()
        self._code_sandbox = code_sandbox
        self._code_store = code_store
        self._sandbox_config = sandbox_config
        self._discovery_callback = discovery_callback

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

        # Bash capability
        elif config.capability_hint == "bash":
            sandbox = BashSandbox(self._session_storage_path)
            output_content = await sandbox.execute(full_instruction)
            tool_trace.append("bash_sandbox")

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
            )

        # Tool server call (web_search, document_generation, image_generation, auto)
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
                )

        # Scrub credentials from output
        output_content = self._scrubber.scrub(output_content)

        # Extract bounded content summary
        summary_tokens = config.output.content_summary_tokens
        content_summary = _extract_content_summary(output_content, summary_tokens)

        duration_ms = int(time.time() * 1000) - start_ms

        return ResultEnvelope(
            task_id=task.task_id,
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
        system = (
            f"You are executing a '{config.name}' task. "
            f"Output format: {config.output_format}. "
            f"{config.description}"
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
            stop_streaming = False
            async for token in llm_client.stream(
                messages=conversation,
                system=system,
                provider_name=provider_name,
            ):
                tokens.append(token)
                accumulated += token
                if hitl_enabled and "</ask_human>" in accumulated.lower():
                    stop_streaming = True
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
        """Call an MCP tool server endpoint."""
        conn = tool_registry._connections.get(server_name)
        if not conn or not conn.session:
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
        base_url = info.url
        if not base_url:
            raise CortexToolUnavailableError(f"Tool server '{server_name}' has no URL", server_name=server_name)

        try:
            async with conn.session.post(
                f"{base_url}/tools/{tool_name}/invoke",
                json={"params": params},
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

    async def execute_bash(self, command: str, session_storage_path: str) -> str:
        """Execute bash command within security sandbox."""
        sandbox = BashSandbox(session_storage_path)
        return await sandbox.execute(command)

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
        output_dir = str(
            __import__("pathlib").Path(self._session_storage_path) / "code_output" / task.task_id.replace("/", "_")
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
