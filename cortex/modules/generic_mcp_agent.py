"""GenericMCPAgent — universal stateless task executor."""
import asyncio
import importlib
import logging
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from cortex.config.schema import ReactConfig, TaskTypeConfig
from cortex.exceptions import CortexTaskError, CortexToolUnavailableError
from cortex.llm.client import LLMClient, LLMQueueCredit, llm_queue_credit
from cortex.llm.context import TaskContext, TokenUsage
from cortex.modules.react_loop import ActionResult, ReactLoop, ReactResult
from cortex.modules.result_envelope_store import ResultEnvelope, ResultEnvelopeStore
from cortex.modules.signal_registry import SignalRegistry
from cortex.modules.task_graph_compiler import RuntimeTask
from cortex.modules.tool_server_registry import ToolServerRegistry
from cortex.prompts import (
    APP_CONTROL_SYSTEM,
    APP_CONTROL_WITH_CAPS_USER,
    BASH_CODEGEN_SYSTEM,
    BASH_CODEGEN_USER,
    REACT_BUILTIN_ACTIONS,
    REACT_SYSTEM,
    REACT_USER_INITIAL,
    TASK_EXEC_HITL_SUFFIX,
    TASK_EXEC_SESSION_CONTEXT,
    TASK_EXEC_SESSION_SCRATCHPAD_LINE,
    TASK_EXEC_SYSTEM,
)
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
        app_control=None,        # cortex.modules.app_control.AppControl instance
        app_control_config=None, # cortex.config.schema.AppControlConfig
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
        self._app_control = app_control
        self._app_control_config = app_control_config
        self._hitl_relay_url = hitl_relay_url
        self._builtin_web_search_enabled = builtin_web_search_enabled

    async def _run_with_credit_timeout(self, coro, timeout: float):
        """Run ``coro`` under a wall-clock timeout that excludes time the task
        spent queued for the shared LLM gate.

        A single-stream LLM backend serializes inference, so a task running
        alongside others is charged wall-clock time it spent merely waiting its
        turn. That starvation is credited back via the ``llm_queue_credit``
        ContextVar, so a task is timed out only for its *own* work. Raises
        ``asyncio.TimeoutError`` on breach — callers handle it as before.
        """
        credit = LLMQueueCredit()
        token = llm_queue_credit.set(credit)
        try:
            inner = asyncio.ensure_future(coro)
            start = time.monotonic()
            while True:
                done, _ = await asyncio.wait({inner}, timeout=5.0)
                if inner in done:
                    return inner.result()
                if time.monotonic() - start > timeout + credit.seconds:
                    inner.cancel()
                    try:
                        await inner
                    except BaseException:
                        pass
                    raise asyncio.TimeoutError()
        finally:
            llm_queue_credit.reset(token)

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
                envelope = await self._run_with_credit_timeout(
                    self._execute_once(task, tool_registry, llm_client, envelope_store, config, event_queue=event_queue),
                    config.timeout_seconds,
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

    async def _call_app_control(
        self,
        task: RuntimeTask,
        instruction: str,
        session_id: str,
        llm_client,
        config,
        event_queue,
    ) -> str:
        """
        Dispatch an app_control task using a two-path strategy:

        1. Primary — scripting dictionary
           Run AppCapabilityScout to find the app's automation interface (macOS
           sdef, Windows UI Automation / COM). Inject the summary into the LLM
           prompt so it generates precise, API-correct actions.

        2. Fallback — screenshot vision loop
           When no scripting interface is found, enter a screenshot → vision LLM
           → execute action loop (up to max_vision_steps).
        """
        if self._app_control is None:
            return "[app_control not enabled — add app_control.enabled: true to cortex.yaml]"

        import platform as _platform
        self._app_control._event_queue = event_queue

        # Pull config knobs (defaults if no config provided)
        ac_cfg = self._app_control_config
        sdef_max_chars   = getattr(ac_cfg, "sdef_max_chars",   8000)
        max_vision_steps = getattr(ac_cfg, "max_vision_steps", 10)
        vision_provider  = getattr(ac_cfg, "vision_provider",  "default")
        scout_timeout    = getattr(ac_cfg, "timeout_seconds",  15)

        # ── Step 1: extract the app name from the instruction ─────────────────
        app_name = await self._extract_app_name(instruction, llm_client, config)

        # ── Step 2: run capability scout ──────────────────────────────────────
        from cortex.modules.app_control import AppCapabilityScout
        scout = AppCapabilityScout(
            timeout_seconds=scout_timeout,
            sdef_max_chars=sdef_max_chars,
        )
        capability = await scout.discover(app_name)
        logger.info(
            "AppCapabilityScout: app=%r type=%s (task %s)",
            app_name, capability.type, task.task_id,
        )

        # ── Step 3a: scripting dictionary path ────────────────────────────────
        if capability.type != "none":
            try:
                prompt = APP_CONTROL_WITH_CAPS_USER.format(
                    instruction=instruction,
                    platform=_platform.system(),
                    capabilities=capability.summary,
                    app_name=app_name,
                )
                resp = await llm_client.complete(
                    messages=[{"role": "user", "content": prompt}],
                    system=APP_CONTROL_SYSTEM,
                    provider_name=config.llm_provider or "default",
                    max_tokens=800,
                )
                raw_plan = (resp.content or "").strip()
            except Exception as e:
                return f"[app_control: LLM plan generation failed — {e}]"

            blocks = [b.strip() for b in raw_plan.split("---") if b.strip()]
            results = []
            for block in blocks:
                result = await self._app_control._execute_action_block(block, task, session_id)
                results.append(result)
            return "\n---\n".join(results) if results else "[app_control: no actions generated]"

        # ── Step 3b: vision loop fallback ─────────────────────────────────────
        logger.info(
            "app_control: no scripting dict for %r — falling back to vision loop "
            "(max_steps=%d, provider=%s)",
            app_name, max_vision_steps, vision_provider,
        )
        import os as _os
        vision_output_dir = _os.path.join(self._session_storage_path, "app_control_vision")
        return await self._app_control.execute_with_vision_loop(
            instruction=instruction,
            app_name=app_name,
            llm_client=llm_client,
            session_id=session_id,
            task=task,
            output_dir=vision_output_dir,
            max_steps=max_vision_steps,
            vision_provider=vision_provider,
            event_queue=event_queue,
        )

    async def _extract_app_name(
        self, instruction: str, llm_client, config
    ) -> str:
        """Ask the LLM to extract the target app name from a free-text instruction."""
        # Fast heuristic first: look for quoted app names or known patterns
        quoted = re.search(r'"([^"]{2,40})"', instruction)
        if quoted:
            return quoted.group(1)

        # Small LLM call to identify the app
        try:
            resp = await llm_client.complete(
                messages=[{
                    "role": "user",
                    "content": (
                        f"Extract the name of the application being controlled from this "
                        f"instruction. Reply with ONLY the app name — no punctuation, "
                        f"no explanation:\n\n{instruction[:400]}"
                    ),
                }],
                system="You are a text extractor. Reply with only the app name.",
                provider_name=config.llm_provider or "default",
                max_tokens=15,
            )
            return (resp.content or "").strip().strip('"').strip("'")
        except Exception:
            return ""

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
        available_capabilities = list(tool_registry._capability_map.keys()) + [
            "llm_synthesis", "workspace_bash", "app_control",
        ]

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
        upstream_files: list[str] = []
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
                # Pipe any artifacts produced by the upstream task into this one
                # so app_control / code_exec can act on them (e.g. open the file
                # generated by an earlier code_exec step).
                refs_files = getattr(ref_envelope, "output_files", None) or []
                upstream_files.extend(refs_files)

        # Build base instruction (instruction + upstream context).
        # The retry-feedback block is *not* baked in here — LLM-synthesis paths
        # consume `task.attempt_history` and thread prior (output, feedback)
        # pairs as real conversation turns inside `_call_llm`. Non-LLM paths
        # (code_exec, bash, app_control, MCP tool calls) don't take a message
        # list, so for them we append a compact feedback block below.
        base_instruction = task.instruction
        if input_context:
            base_instruction += f"\n\nContext from prior tasks:{input_context}"
        if upstream_files:
            base_instruction += (
                "\n\nUPSTREAM_FILES (artifacts produced by prior tasks):\n  "
                + "\n  ".join(upstream_files)
                + "\n\nWhen launching or opening files, prefer one of these absolute paths."
            )

        # For non-LLM dispatch paths: append validation feedback to instruction
        # (legacy behavior). The LLM-synthesis path uses task.attempt_history
        # instead and ignores this block.
        full_instruction = base_instruction
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
        produced_files: list[str] = []           # absolute paths produced by this task
        token_usage = TokenUsage()

        # Scripted-handler / code-node tasks run their Python handler directly:
        # deterministic, no LLM step to loop over. Every other task is executed
        # by the ReAct loop, which reasons, picks an action, observes the
        # result, and repeats until the sub-agent decides the task is done.
        if config.complexity == "scripted" and config.handler:
            output_content, output_type = await self._call_handler(
                config.handler,
                task, full_instruction, config,
                tool_registry=tool_registry,
                llm_client=llm_client,
                envelope_store=envelope_store,
            )
            tool_trace.append(f"handler:{config.handler}")
        else:
            react_result = await self._run_react_loop(
                task=task,
                config=config,
                base_instruction=base_instruction,
                tool_registry=tool_registry,
                llm_client=llm_client,
                event_queue=event_queue,
                available_capabilities=available_capabilities,
            )
            output_content = react_result.final_answer
            token_usage = react_result.token_usage
            produced_files = list(react_result.produced_files)
            generated_script = react_result.generated_script
            forged_server_path = react_result.forged_server_path
            tool_trace.extend(react_result.tool_trace)

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
            output_files=produced_files,
        )

    async def _run_react_loop(
        self,
        task: RuntimeTask,
        config: TaskTypeConfig,
        base_instruction: str,
        tool_registry: ToolServerRegistry,
        llm_client: LLMClient,
        event_queue,
        available_capabilities: List[str],
    ) -> ReactResult:
        """Execute a task via the ReAct (reason -> act -> observe) loop.

        Builds the action menu, system prompt, and initial instruction, then
        hands control to :class:`ReactLoop`. The loop calls back into
        :meth:`_execute_action` for every step and stops as soon as the
        sub-agent's LLM emits a ``finish`` action. Each observation is fed
        back together with the model's own stated expectation for that step,
        so every reasoning turn sees both what happened and what was intended.
        """
        from cortex.streaming.status_events import EventType, StatusEvent

        session_id = task.task_id.split("/")[0]
        react_cfg = getattr(config, "react", None) or ReactConfig()

        # Action menu — built-ins gated by what is actually wired up, plus
        # every ready MCP tool-server capability.
        actions = self._build_action_menu(tool_registry, config)
        action_names = [name for name, _ in actions]
        action_menu = "\n".join(f"  - {name}: {desc}" for name, desc in actions)

        # System prompt: role + task + action menu, then optional session
        # context (same gating as the legacy llm_synthesis path).
        system = REACT_SYSTEM.format(
            task_name=config.name,
            description=config.description,
            output_format=config.output_format,
            action_menu=action_menu,
        )
        session_goal = (getattr(task, "session_goal", "") or "").strip()
        if session_goal:
            scratchpad = (getattr(task, "session_scratchpad", "") or "").strip()
            scratchpad_line = (
                TASK_EXEC_SESSION_SCRATCHPAD_LINE.format(scratchpad=scratchpad[:1500])
                if scratchpad else ""
            )
            system += TASK_EXEC_SESSION_CONTEXT.format(
                session_goal=session_goal[:800],
                scratchpad_line=scratchpad_line,
            )

        # Initial instruction + retry context. On a wave-validation retry,
        # prior attempts and judge feedback are threaded in so the loop diffs
        # against what failed instead of starting blind.
        initial = REACT_USER_INITIAL.format(instruction=base_instruction)
        attempt_history = list(getattr(task, "attempt_history", []) or [])
        if attempt_history:
            blocks = ["\n\n## Previous attempts (rejected by the validation judge)"]
            for i, entry in enumerate(attempt_history, start=1):
                prev_output = (entry.get("output") or "").strip()
                prev_feedback = (entry.get("feedback") or "").strip()
                blocks.append(
                    f"\nAttempt {i} produced:\n{prev_output[:800]}\n"
                    f"Judge feedback: {prev_feedback}"
                )
            blocks.append(
                "\nKeep what was correct; fix only the issues the judge raised."
            )
            initial += "\n".join(blocks)
        elif task.validation_feedback:
            initial += (
                "\n\n[RETRY FEEDBACK - the previous attempt failed validation]\n"
                f"{task.validation_feedback}"
            )

        async def _emit(message: str, metadata: dict) -> None:
            if event_queue is None:
                return
            await event_queue.put(StatusEvent(
                message=message,
                session_id=session_id,
                event_type=EventType.STATUS,
                metadata={
                    "task_id": task.task_id,
                    "task_name": task.task_name,
                    **metadata,
                },
            ))

        async def _exec(action: str, action_input: str) -> ActionResult:
            return await self._execute_action(
                action=action,
                action_input=action_input,
                task=task,
                config=config,
                tool_registry=tool_registry,
                llm_client=llm_client,
                event_queue=event_queue,
                available_capabilities=available_capabilities,
                session_id=session_id,
            )

        loop = ReactLoop(
            llm_client=llm_client,
            provider_name=config.llm_provider or "default",
            system_prompt=system,
            max_iterations=react_cfg.max_iterations,
            observation_max_tokens=react_cfg.observation_max_tokens,
            context_char_budget=react_cfg.context_char_budget,
            valid_actions=action_names,
            execute_action=_exec,
            emit_status=_emit,
        )
        result = await loop.run(initial)
        logger.info(
            "Task %s: ReAct loop finished in %d step(s) (%d action call(s))",
            task.task_id, result.steps, len(result.tool_trace),
        )
        return result

    def _build_action_menu(
        self, tool_registry: ToolServerRegistry, config: TaskTypeConfig
    ) -> List[tuple]:
        """Return ``[(action_name, description)]`` for this task's ReAct loop.

        A built-in action is offered only when its backing capability is
        actually wired up on this agent; every ready MCP tool-server
        capability is appended so the loop can reach discovered tools too.
        """
        actions: List[tuple] = [
            ("llm_synthesis", REACT_BUILTIN_ACTIONS["llm_synthesis"]),
            ("web_search", REACT_BUILTIN_ACTIONS["web_search"]),
            ("bash", REACT_BUILTIN_ACTIONS["bash"]),
        ]
        if self._code_sandbox is not None:
            actions.append(("code_exec", REACT_BUILTIN_ACTIONS["code_exec"]))
            actions.append(("forge_mcp", REACT_BUILTIN_ACTIONS["forge_mcp"]))
        if self._workspace_bash is not None:
            actions.append(("workspace_bash", REACT_BUILTIN_ACTIONS["workspace_bash"]))
        if self._app_control is not None:
            actions.append(("app_control", REACT_BUILTIN_ACTIONS["app_control"]))
        if config.human_in_loop:
            actions.append(("ask_user", REACT_BUILTIN_ACTIONS["ask_user"]))

        builtin = {name for name, _ in actions}
        try:
            for cap in sorted(tool_registry._capability_map):
                if cap not in builtin and tool_registry._capability_map[cap]:
                    actions.append((cap, f"MCP tool-server capability: {cap}"))
        except Exception:
            pass
        return actions

    async def _execute_action(
        self,
        *,
        action: str,
        action_input: str,
        task: RuntimeTask,
        config: TaskTypeConfig,
        tool_registry: ToolServerRegistry,
        llm_client: LLMClient,
        event_queue,
        available_capabilities: List[str],
        session_id: str,
    ) -> ActionResult:
        """Execute one ReAct action and return its observation.

        Each action maps to one execution capability - the same set the legacy
        single-pass dispatch used. Exceptions raised here are caught by the
        caller (:class:`ReactLoop`) and fed back as observations, so a failed
        action lets the loop adapt rather than aborting the whole task.
        """
        tool_trace: List[str] = []

        # ── code_exec ─────────────────────────────────────────────────────────
        if action == "code_exec":
            task._produced_files = []  # capture only this step's files
            output, script = await self._call_code_exec(
                task=task,
                instruction=action_input,
                config=config,
                llm_client=llm_client,
                tool_trace=tool_trace,
                event_queue=event_queue,
            )
            return ActionResult(
                observation=output,
                generated_script=script,
                produced_files=list(getattr(task, "_produced_files", []) or []),
                tool_trace=tool_trace,
            )

        # ── forge_mcp ─────────────────────────────────────────────────────────
        if action == "forge_mcp":
            output, server_path = await self._call_forge_mcp(
                task=task,
                instruction=action_input,
                config=config,
                llm_client=llm_client,
                tool_trace=tool_trace,
                event_queue=event_queue,
            )
            return ActionResult(
                observation=output,
                forged_server_path=server_path,
                tool_trace=tool_trace,
            )

        # ── bash ──────────────────────────────────────────────────────────────
        if action == "bash":
            sandbox = BashSandbox(self._session_storage_path)
            bash_cmd = action_input
            try:
                bash_resp = await llm_client.complete(
                    messages=[{
                        "role": "user",
                        "content": BASH_CODEGEN_USER.format(instruction=action_input),
                    }],
                    system=BASH_CODEGEN_SYSTEM,
                    provider_name=config.llm_provider or "default",
                    max_tokens=60,
                )
                generated = (bash_resp.content or "").strip().strip("`").strip()
                if generated:
                    bash_cmd = generated
                    tool_trace.append("bash:llm_generated_cmd")
            except Exception as be:
                logger.debug("bash LLM command generation failed: %s", be)
            output = await sandbox.execute(bash_cmd)
            tool_trace.append("bash_sandbox")
            return ActionResult(observation=output, tool_trace=tool_trace)

        # ── workspace_bash (falls back to code_exec when no workspace set) ────
        if action == "workspace_bash":
            ws_ready = (
                self._workspace_bash is not None
                and self._workspace_bash._default_workspace is not None
            )
            if not ws_ready:
                task._produced_files = []
                output, script = await self._call_code_exec(
                    task=task,
                    instruction=action_input,
                    config=config,
                    llm_client=llm_client,
                    tool_trace=tool_trace,
                    event_queue=event_queue,
                )
                tool_trace.append("workspace_bash->code_exec")
                return ActionResult(
                    observation=output,
                    generated_script=script,
                    produced_files=list(getattr(task, "_produced_files", []) or []),
                    tool_trace=tool_trace,
                )
            output = await self._call_workspace_bash(
                task=task,
                instruction=action_input,
                session_id=session_id,
                event_queue=event_queue,
            )
            tool_trace.append("workspace_bash")
            return ActionResult(observation=output, tool_trace=tool_trace)

        # ── app_control ───────────────────────────────────────────────────────
        if action == "app_control":
            output = await self._call_app_control(
                task=task,
                instruction=action_input,
                session_id=session_id,
                llm_client=llm_client,
                config=config,
                event_queue=event_queue,
            )
            tool_trace.append("app_control")
            return ActionResult(observation=output, tool_trace=tool_trace)

        # ── ask_user (human-in-the-loop clarification) ───────────────────────
        if action == "ask_user":
            answer = await self.ask_human(
                task=task,
                question=action_input,
                event_queue=event_queue,
            )
            observation = answer or (
                "(No answer available - human-in-the-loop is disabled or the "
                "request timed out. Proceed with your best judgement and do "
                "not ask again.)"
            )
            return ActionResult(observation=observation, tool_trace=["hitl:ask_user"])

        # ── llm_synthesis ─────────────────────────────────────────────────────
        if action == "llm_synthesis":
            output, usage = await self._call_llm(
                task_id=task.task_id,
                instruction=action_input,
                config=config,
                llm_client=llm_client,
                tool_trace=tool_trace,
                task=task,
                event_queue=event_queue,
                available_capabilities=available_capabilities,
            )
            return ActionResult(observation=output, token_usage=usage, tool_trace=tool_trace)

        # ── web_search — configured tool server first, built-in DDG fallback ──
        if action == "web_search":
            conn = await _select_tool_for_task("web_search", config.tool_servers, tool_registry)
            if conn:
                try:
                    output = await self.call_tool_server(
                        server_name=conn.server_name,
                        tool_name="web_search",
                        params={"instruction": action_input, "task_id": task.task_id},
                        tool_registry=tool_registry,
                    )
                    tool_trace.append(f"tool:{conn.server_name}")
                    return ActionResult(observation=output, tool_trace=tool_trace)
                except Exception as e:
                    logger.warning("Configured web_search server failed (%s) - using built-in DDG", e)
            if not self._builtin_web_search_enabled:
                tool_trace.append("builtin:duckduckgo:disabled")
                return ActionResult(
                    observation=(
                        "Web search is disabled. Configure a web_search tool "
                        "server or enable builtin_web_search_enabled in the agent config."
                    ),
                    tool_trace=tool_trace,
                )
            from cortex.modules.builtin_search import DuckDuckGoSearch
            output = await DuckDuckGoSearch().search(action_input)
            tool_trace.append("builtin:duckduckgo")
            return ActionResult(observation=output, tool_trace=tool_trace)

        # ── MCP tool-server capability ────────────────────────────────────────
        conn = await _select_tool_for_task(action, config.tool_servers, tool_registry)
        if conn is None and self._discovery_callback:
            logger.info(
                "Task %s: no tool for '%s' - triggering mid-run external discovery",
                task.task_id, action,
            )
            try:
                discovered = await self._discovery_callback(action)
                if discovered:
                    conn = await _select_tool_for_task(action, config.tool_servers, tool_registry)
            except Exception as disc_err:
                logger.warning(
                    "Mid-run discovery callback failed for task %s: %s",
                    task.task_id, disc_err,
                )
        if conn is None:
            return ActionResult(
                observation=(
                    f"[no tool server provides capability '{action}'. Pick a "
                    "different action - e.g. code_exec, web_search, bash, or "
                    "llm_synthesis - or finish with what you already have.]"
                ),
                tool_trace=[f"{action}:unavailable"],
            )
        if event_queue:
            from cortex.streaming.status_events import TaskToolCallEvent
            await event_queue.put(TaskToolCallEvent(
                session_id=session_id,
                task_id=task.task_id,
                task_name=task.task_name,
                tool_name=action,
                tool_input={"server": conn.server_name},
            ))
        tool_result = await self.call_tool_server(
            server_name=conn.server_name,
            tool_name=action,
            params={"instruction": action_input, "task_id": task.task_id},
            tool_registry=tool_registry,
        )
        tool_trace.append(f"tool:{conn.server_name}")
        if tool_result.startswith("INSTRUCTIONS:"):
            output, usage = await self._call_llm(
                task_id=task.task_id,
                instruction=tool_result[len("INSTRUCTIONS:"):].strip(),
                config=config,
                llm_client=llm_client,
                tool_trace=tool_trace,
                task=task,
                event_queue=event_queue,
                available_capabilities=available_capabilities,
            )
            return ActionResult(observation=output, token_usage=usage, tool_trace=tool_trace)
        return ActionResult(observation=tool_result, tool_trace=tool_trace)

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
        caps_note = (
            f" Agent capabilities available: {', '.join(sorted(available_capabilities))}."
            if available_capabilities else ""
        )
        system = TASK_EXEC_SYSTEM.format(
            task_name=config.name,
            caps_note=caps_note,
            output_format=config.output_format,
            description=config.description,
        )

        # Session context: give the worker the overall goal + planner scratchpad
        # so it can reason about why its task exists instead of running blind.
        # Populated by the framework at wave dispatch only when
        # agent.inject_session_context is enabled — empty otherwise.
        session_goal = (getattr(task, "session_goal", "") or "").strip() if task else ""
        if session_goal:
            scratchpad = (getattr(task, "session_scratchpad", "") or "").strip()
            scratchpad_line = (
                TASK_EXEC_SESSION_SCRATCHPAD_LINE.format(scratchpad=scratchpad[:1500])
                if scratchpad else ""
            )
            system += TASK_EXEC_SESSION_CONTEXT.format(
                session_goal=session_goal[:800],
                scratchpad_line=scratchpad_line,
            )

        hitl_enabled = (
            task is not None
            and config.human_in_loop
            and event_queue is not None
        )
        if hitl_enabled:
            system += TASK_EXEC_HITL_SUFFIX

        # Wave-validation retry: if previous attempts produced output that the
        # judge rejected, thread each (output, feedback) pair as real
        # conversation turns so the model can diff its prior text against the
        # rule that fired instead of regenerating blind from a single
        # appended-feedback blob.
        attempt_history = list(getattr(task, "attempt_history", []) or []) if task else []
        attempt_index = len(attempt_history) + 1  # 1-indexed: this call's attempt
        if attempt_history:
            system += (
                f"\n\n## Retry Context\n"
                f"This is attempt {attempt_index} of 3. Previous attempts are "
                f"shown as assistant turns followed by judge feedback. "
                f"Keep what was correct; fix only the issues the judge raised. "
                f"Do not regenerate from scratch."
            )

        tool_trace.append(f"llm:{provider_name}")

        ask_pattern = _re.compile(r"<ask_human>(.*?)</ask_human>", _re.DOTALL | _re.IGNORECASE)
        conversation: List[Dict[str, str]] = [{"role": "user", "content": instruction}]
        for i, entry in enumerate(attempt_history, start=1):
            prev_output = (entry.get("output") or "").strip()
            prev_feedback = (entry.get("feedback") or "").strip()
            if not prev_output and not prev_feedback:
                continue
            conversation.append({
                "role": "assistant",
                "content": prev_output or "(no output produced)",
            })
            conversation.append({
                "role": "user",
                "content": (
                    f"[Attempt {i}/3 failed validation]\n"
                    f"Judge feedback: {prev_feedback}\n\n"
                    "Produce a revised response that addresses ONLY the issues "
                    "above. Preserve any parts of your previous response that "
                    "the judge did not call out."
                ),
            })
        total_input_chars = sum(len(m.get("content", "")) for m in conversation)
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
        tool_registry: Optional[ToolServerRegistry] = None,
        llm_client: Optional[LLMClient] = None,
        envelope_store: Optional[ResultEnvelopeStore] = None,
    ) -> tuple[str, str]:
        """Run a scripted handler / code node.

        Two handler addressing schemes are supported:
          - ``cortex:node:<id>`` — an in-process callable registered by
            CortexBuilder.node(). Resolved via cortex.handler_registry.
          - ``my_module.my_function`` — a dotted import path (cortex.yaml
            ``handler:`` field). Imported from the agent_tools store.
        """
        from cortex.handler_registry import is_registered_handler, resolve_handler

        session_id = task.task_id.split("/")[0]

        if is_registered_handler(handler_path):
            try:
                fn = resolve_handler(handler_path)
            except KeyError:
                raise CortexTaskError(
                    f"Code node handler '{handler_path}' is not registered in "
                    f"this process — rebuild the agent with CortexBuilder",
                    task_id=task.task_id,
                )
        else:
            parts = handler_path.rsplit(".", 1)
            if len(parts) != 2:
                raise CortexTaskError(f"Invalid handler path: {handler_path}", task_id=task.task_id)
            module_path, fn_name = parts
            try:
                # Ensure the agent_tools directory is importable. The store lives at
                # {base_path}/agent_tools/ so we need {base_path} on sys.path.
                if self._code_store is not None:
                    store_parent = str(self._code_store._store_dir.parent)
                    if store_parent not in sys.path:
                        sys.path.insert(0, store_parent)
                module = importlib.import_module(module_path)
                fn = getattr(module, fn_name)
            except (ImportError, AttributeError) as e:
                raise CortexTaskError(f"Cannot load handler {handler_path}: {e}", task_id=task.task_id)

        # Resolve upstream node outputs into a {dep_name: output} dict so a
        # code node can read its dependencies' results directly.
        deps: Dict[str, str] = {}
        if envelope_store is not None and task.depends_on_ids:
            for dep_name, dep_id in zip(task.depends_on, task.depends_on_ids):
                try:
                    env = await envelope_store.read_envelope(session_id, dep_id)
                except Exception as e:  # never fail a node on dep-read errors
                    logger.debug("Code node dep read failed (%s): %s", dep_id, e)
                    env = None
                if env is not None:
                    deps[dep_name] = (
                        env.output_value
                        if env.output_type != "file"
                        else (env.content_summary or env.output_value)
                    )

        ctx = TaskContext(
            task_id=task.task_id,
            session_id=session_id,
            task_name=task.task_name,
            instruction=instruction,
            input_refs=task.input_refs,
            context_hints=task.context_hints,
            output_format=config.output_format,
        )
        # ── Runtime wiring ──────────────────────────────────────────────────
        ctx.request = task.context_hints.get("request") or task.instruction
        ctx.user_id = task.principal.principal_id if task.principal else ""
        ctx.deps = deps
        ctx._llm_client = llm_client
        ctx._llm_provider = config.llm_provider or "default"
        if tool_registry is not None:
            async def _tool_caller(server: str, tool: str, params: Dict) -> str:
                return await self.call_tool_server(server, tool, params, tool_registry)
            ctx._tool_caller = _tool_caller

        if asyncio.iscoroutinefunction(fn):
            result = await fn(ctx)
        else:
            result = fn(ctx)
            if asyncio.iscoroutine(result):
                result = await result

        if result is None:
            return "", config.output_format
        if isinstance(result, tuple):
            return str(result[0] or ""), (result[1] if len(result) > 1 else config.output_format)
        if isinstance(result, (dict, list)):
            import json as _json
            return _json.dumps(result, default=str), "json"
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
            # Stash on task for the caller to surface into the envelope.
            try:
                task._produced_files = list(result.output_files)
            except Exception:
                pass

        # Return the generated source_code so the caller can store it in the
        # ResultEnvelope. End-of-session consent is handled by LearningEngine.
        return output, source_code
