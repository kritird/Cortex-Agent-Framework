"""PrimaryAgent — thin orchestrator with maximum 3 LLM calls per session."""
import asyncio
import json
import logging
import re
from typing import AsyncIterator, Dict, List, Optional

from cortex.config.schema import CortexConfig
from cortex.llm.client import LLMClient
from cortex.modules.blueprint_store import BlueprintStore
from cortex.modules.capability_scout import ScoutResult
from cortex.modules.history_store import HistoryRecord
from cortex.modules.result_envelope_store import ResultEnvelope
from cortex.modules.task_graph_compiler import DecomposedTask
from cortex.modules.validation_agent import ValidationFinding
from cortex.streaming.status_events import (
    ClarificationEvent, EventType, ResultEvent, StatusEvent
)

logger = logging.getLogger(__name__)


def build_system_prompt(
    config: CortexConfig,
    capabilities: List[str],
    scout_result: Optional["ScoutResult"] = None,
    blueprint_blocks: Optional[List[str]] = None,
) -> str:
    """
    Framework generates the system prompt from cortex.yaml — developer never writes it.

    When a ScoutResult is provided, real tool names and descriptions from the matched
    MCP servers are surfaced so the decomposition LLM has concrete vocabulary to work
    with — critical when no task_types are defined in cortex.yaml. The ScoutResult
    also carries persisted sandbox code utilities discovered from AgentCodeStore, so
    the LLM learns which task names already have tested code in the agent store and
    should be preferred over generating new tasks.
    """
    has_predefined_tasks = bool(config.task_types)
    has_scout_tools = scout_result is not None and scout_result.has_tools
    has_scripts = scout_result is not None and scout_result.has_code_utils

    lines = [
        f"You are {config.agent.name}.",
        f"{config.agent.description}",
        "",
    ]

    # ── Pre-built scripts (highest priority — free to execute) ─────────────
    if has_scripts:
        lines.append("## Pre-built Agent Scripts")
        lines.append(
            "These task names have tested, persisted code that runs without LLM generation. "
            "Prefer them over other options when they match the request:"
        )
        for script in scout_result.code_utils:
            use_str = f" (used {script.use_count}×)" if script.use_count else ""
            desc = f": {script.description}" if script.description else ""
            lines.append(f"- {script.task_name}{use_str}{desc}")
        lines.append("")

    # ── Task type vocabulary ────────────────────────────────────────────────
    if has_predefined_tasks:
        lines.append("## Available Task Types")
        for task_type in config.task_types:
            mandatory_str = "(mandatory)" if task_type.mandatory else "(optional)"
            deps_str = (
                f" [depends on: {', '.join(task_type.depends_on)}]"
                if task_type.depends_on else ""
            )
            lines.append(
                f"- {task_type.name} {mandatory_str}: {task_type.description} "
                f"[output: {task_type.output_format}]{deps_str}"
            )
        lines.append("")

    if has_scout_tools:
        # Surface actual tool names and descriptions discovered from MCP servers.
        # These become the task name vocabulary when no predefined types exist,
        # or supplement predefined types when they do.
        lines.append("## Discovered MCP Tools")
        if not has_predefined_tasks:
            lines.append(
                "No predefined task types are configured. "
                "Use the tool names below as task names when decomposing."
            )
        else:
            lines.append(
                "The following tools are available in addition to the predefined task types above."
            )
        lines.append("")
        for cap, tools in scout_result.tools_by_capability().items():
            lines.append(f"Capability: {cap}")
            for t in tools:
                desc = f" — {t.description}" if t.description else ""
                lines.append(f"  - {t.name}{desc}")
        lines.append("")
    elif not has_predefined_tasks and not has_scripts:
        # No task types, no scout, no scripts — tell the LLM what capabilities exist at minimum
        lines += [
            "## Available Capabilities",
            f"The following tool capabilities are available: {', '.join(capabilities) or 'none'}",
            "",
        ]

    # ── Task blueprints (accumulated guidance from prior runs) ──────────────
    if blueprint_blocks:
        lines.append("## Task Blueprints")
        lines.append(
            "The following blueprints capture dos/don'ts, clarifications, and "
            "lessons from prior runs of these tasks. Treat them as authoritative "
            "guidance unless the user's request explicitly overrides them."
        )
        lines.append("")
        for block in blueprint_blocks:
            lines.append(block)
            lines.append("")

    # ── Decomposition format ────────────────────────────────────────────────
    lines += [
        "## Decomposition Output Format",
        "Decompose the user request into tasks. For each task, output a block:",
        "```",
        "<task>",
        "  <name>task_type_name</name>",
        "  <instruction>specific instruction for this task</instruction>",
        "  <depends_on>comma_separated_task_names_or_empty</depends_on>",
        "</task>",
        "```",
    ]
    guidance_parts = []
    if has_scripts:
        guidance_parts.append("prefer pre-built script names when they match")
    if has_predefined_tasks:
        guidance_parts.append("use predefined task types for everything else")
    if has_scout_tools and not has_predefined_tasks:
        guidance_parts.append("use discovered tool names as task names")
    if guidance_parts:
        lines.append("Priority: " + ", then ".join(guidance_parts) + ".")
    lines.append("Output ALL task blocks before any other text.")

    if config.agent.clarification.enabled:
        lines += [
            "",
            "## Clarification",
            "If the request is ambiguous and you need clarification before proceeding, output:",
            "<clarification>Your question here</clarification>",
            "Wait for the user's response before proceeding with decomposition.",
        ]

    if config.agent.synthesis_guidance:
        lines += [
            "",
            "## Synthesis Guidance",
            config.agent.synthesis_guidance,
        ]

    return "\n".join(lines)


def _parse_task_blocks(text: str) -> List[DecomposedTask]:
    """Parse <task> XML blocks from LLM decomposition stream."""
    tasks = []
    pattern = re.compile(
        r'<task>\s*'
        r'<name>(.*?)</name>\s*'
        r'<instruction>(.*?)</instruction>\s*'
        r'(?:<depends_on>(.*?)</depends_on>\s*)?'
        r'</task>',
        re.DOTALL | re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        name = match.group(1).strip()
        instruction = match.group(2).strip()
        depends_on_raw = (match.group(3) or "").strip()
        depends_on = [d.strip() for d in depends_on_raw.split(",") if d.strip()] if depends_on_raw else []
        if name:
            tasks.append(DecomposedTask(
                task_name=name,
                instruction=instruction,
                depends_on=depends_on,
            ))
    return tasks


def _parse_clarification(text: str) -> Optional[str]:
    """Extract clarification question from stream."""
    match = re.search(r'<clarification>(.*?)</clarification>', text, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else None


class PrimaryAgent:
    """
    Thin orchestrator. Up to 3 LLM calls per session on the hot path
    (decompose, synthesise, optional remediate). One additional post-session
    LLM call may run for blueprint updates — consent-gated and off the user's
    latency path, so it does not count against the 3-call budget.
    Uses llm_access.default — not configurable per call.
    All hot-path LLM calls are streaming.
    """

    def __init__(
        self,
        config: CortexConfig,
        llm_client: LLMClient,
        blueprint_store: Optional["BlueprintStore"] = None,
    ):
        self._config = config
        self._llm = llm_client
        self._blueprint_store = blueprint_store
        self._clarification_events: Dict[str, asyncio.Event] = {}
        self._clarification_answers: Dict[str, str] = {}
        self._scratchpad: str = ""  # session-scoped reasoning trace, reset each session

    def reset_session_state(self) -> None:
        """Clear per-session state so a reused PrimaryAgent starts fresh."""
        self._scratchpad = ""

    async def _load_blueprint_blocks(
        self,
        stale_task_names: Optional[set] = None,
    ) -> List[str]:
        """Fetch blueprint prompt blocks for every task type that references one.

        Tasks without ``blueprint:`` set are skipped — zero cost when unused.
        Missing files/keys are logged and skipped so a stale reference never
        blocks decomposition.

        When ``stale_task_names`` is provided, the corresponding blueprint block
        is rendered with ``is_stale=True`` so the injected text directs the LLM
        to re-discover subtasks instead of blindly following stored topology.
        """
        if not self._blueprint_store or not self._config.blueprint.enabled:
            return []
        max_chars = self._config.blueprint.inject_max_chars
        stale = stale_task_names or set()
        blocks: List[str] = []
        for tt in self._config.task_types:
            ref = getattr(tt, "blueprint", None)
            if not ref:
                continue
            bp = await self._blueprint_store.load(ref)
            if bp is None:
                logger.warning(
                    "Blueprint %r referenced by task %r not found — skipping",
                    ref, tt.name,
                )
                continue
            blocks.append(bp.to_prompt_block(max_chars=max_chars, is_stale=tt.name in stale))
        return blocks

    async def converse(
        self,
        session_id: str,
        request: str,
        history_context: List[HistoryRecord],
        available_capabilities: List[str],
        event_queue: asyncio.Queue,
        task_type_names: Optional[List[str]] = None,
    ) -> str:
        """Streaming direct reply for chat-mode turns.

        Used when IntentGate routes a turn as pure conversation — no scout,
        no decomposition, no envelopes. The system prompt is derived from
        ``agent.description`` plus the agent's declared capabilities so the
        model can answer capability questions ("what can you do?") truthfully
        without inventing tools it doesn't have.
        """
        caps = sorted(set(available_capabilities or []))
        task_names = sorted(set(task_type_names or []))

        system_parts = [
            f"You are {self._config.agent.name}.",
            self._config.agent.description,
            "",
            "You are replying to a conversational turn from the user. "
            "Answer directly and concisely. Do not pretend to execute a task. "
            "If the user asks what you can do, describe the capabilities and "
            "task types listed below in plain language.",
        ]
        if caps:
            system_parts.append("")
            system_parts.append("Available capabilities: " + ", ".join(caps))
        if task_names:
            system_parts.append("Declared task types: " + ", ".join(task_names))
        system_prompt = "\n".join(system_parts)

        messages: List[Dict] = []
        for rec in (history_context or [])[-self._config.history.max_sessions_in_context:]:
            if rec.original_request:
                messages.append({"role": "user", "content": rec.original_request[:1000]})
            if rec.response_summary:
                messages.append({"role": "assistant", "content": rec.response_summary[:1000]})
        messages.append({"role": "user", "content": request})

        await event_queue.put(StatusEvent(
            message="Responding...",
            session_id=session_id,
            event_type=EventType.STATUS,
        ))

        full_response = ""
        async for token in self._llm.stream(
            messages=messages,
            system=system_prompt,
            provider_name="default",
        ):
            full_response += token
            await event_queue.put(ResultEvent(
                content=token,
                session_id=session_id,
                partial=True,
            ))

        await event_queue.put(ResultEvent(
            content=full_response,
            session_id=session_id,
            partial=False,
        ))

        logger.info(
            "Converse complete for session %s (%d chars)",
            session_id, len(full_response),
        )
        return full_response

    async def decompose(
        self,
        session_id: str,
        user_id: str,
        request: str,
        file_refs: List[str],
        history_context: List[HistoryRecord],
        available_capabilities: List[str],
        event_queue: asyncio.Queue,
        scout_result: Optional[ScoutResult] = None,
        stale_task_names: Optional[set] = None,
    ) -> AsyncIterator[DecomposedTask]:
        """
        LLM Call #1 — streaming decomposition.
        Parses task envelopes as they stream and yields immediately.
        When scout_result is provided, the system prompt includes real tool names
        from matched MCP servers and persisted sandbox code utilities.
        """
        blueprint_blocks = await self._load_blueprint_blocks(stale_task_names=stale_task_names)
        system_prompt = build_system_prompt(
            self._config, available_capabilities, scout_result, blueprint_blocks
        )

        # Build history context snippet
        history_snippet = ""
        if history_context:
            snippets = []
            for rec in history_context[-self._config.history.max_sessions_in_context:]:
                snippets.append(
                    f"[Prior session {rec.session_id[:8]}]: "
                    f"Request: {rec.original_request[:200]} | "
                    f"Summary: {rec.response_summary[:300]}"
                )
            history_snippet = "\n\nPrior session context:\n" + "\n".join(snippets)

        user_message = request
        if file_refs:
            user_message += f"\n\nAttached files: {', '.join(file_refs)}"
        if history_snippet:
            user_message += history_snippet

        await event_queue.put(StatusEvent(
            message="Analysing your request and planning tasks...",
            session_id=session_id,
            event_type=EventType.STATUS,
        ))

        accumulated = ""
        dispatched_tasks = set()

        async for token in self._llm.stream(
            messages=[{"role": "user", "content": user_message}],
            system=system_prompt,
            provider_name="default",
        ):
            accumulated += token

            # Check for clarification request
            if self._config.agent.clarification.enabled:
                clarification_q = _parse_clarification(accumulated)
                if clarification_q and session_id not in self._clarification_events:
                    clarification_id = f"clar_{session_id[-4:]}"
                    event = asyncio.Event()
                    self._clarification_events[session_id] = event
                    await event_queue.put(ClarificationEvent(
                        question=clarification_q,
                        session_id=session_id,
                        clarification_id=clarification_id,
                    ))
                    # Wait for answer
                    try:
                        await asyncio.wait_for(event.wait(), timeout=300)
                        answer = self._clarification_answers.get(session_id, "")
                        user_message += (
                            f"\n\nClarification answer: {answer}"
                            f"\n\nThe clarification has been answered. "
                            f"Do NOT ask for further clarification. "
                            f"Proceed directly with task decomposition now."
                        )
                        accumulated = ""
                        # Resume decomposition with clarification injected
                        async for token2 in self._llm.stream(
                            messages=[{"role": "user", "content": user_message}],
                            system=system_prompt,
                            provider_name="default",
                        ):
                            accumulated += token2
                            tasks = _parse_task_blocks(accumulated)
                            for task in tasks:
                                if task.task_name not in dispatched_tasks:
                                    dispatched_tasks.add(task.task_name)
                                    yield task
                        return
                    except asyncio.TimeoutError:
                        logger.warning("Clarification timed out for session %s", session_id)

            # Parse tasks as they arrive in stream
            tasks = _parse_task_blocks(accumulated)
            for task in tasks:
                if task.task_name not in dispatched_tasks:
                    dispatched_tasks.add(task.task_name)
                    await event_queue.put(StatusEvent(
                        message=f"Planning task: {task.task_name}",
                        session_id=session_id,
                        event_type=EventType.TASK_START,
                    ))
                    yield task

        logger.info(
            "Decomposition complete: %d tasks for session %s",
            len(dispatched_tasks), session_id
        )

    def respond_clarification(self, session_id: str, answer: str) -> None:
        """Called by the application to provide a clarification answer."""
        self._clarification_answers[session_id] = answer
        event = self._clarification_events.get(session_id)
        if event:
            event.set()

    async def assemble_context(
        self,
        result_envelopes: List[ResultEnvelope],
        storage_base_path: str = "",
    ) -> tuple[str, Dict[str, str]]:
        """
        Bash-assisted context assembly before synthesis.
        Returns (summary_text, bash_excerpts_dict).
        Primary agent only reads content_summary — never full files directly.
        """
        summaries = []
        bash_excerpts: Dict[str, str] = {}

        for envelope in result_envelopes:
            status_icon = "✓" if envelope.status == "complete" else "✗"
            task_label = envelope.task_id.split("_", 1)[-1] if "_" in envelope.task_id else envelope.task_id

            if envelope.status == "complete":
                summaries.append(
                    f"## {task_label} [{status_icon}]\n{envelope.content_summary}"
                )
                # If output is a large file, read a section via bash
                if envelope.output_type == "file" and envelope.output_value and storage_base_path:
                    try:
                        from cortex.security.bash_sandbox import BashSandbox
                        sandbox = BashSandbox(storage_base_path)
                        # Read first 2000 chars of the file
                        file_path = envelope.output_value
                        if file_path.startswith(storage_base_path):
                            excerpt = await sandbox.execute(f"head -c 2000 '{file_path}'")
                            bash_excerpts[task_label] = excerpt
                    except Exception as e:
                        logger.debug("Bash excerpt failed for %s: %s", task_label, e)
            elif envelope.status == "failed":
                summaries.append(
                    f"## {task_label} [FAILED]\nError: {envelope.error or 'Unknown error'}"
                )
            elif envelope.status == "timeout":
                summaries.append(f"## {task_label} [TIMED OUT]")
            else:
                summaries.append(f"## {task_label} [{envelope.status}]")

        return "\n\n".join(summaries), bash_excerpts

    async def synthesise(
        self,
        session_id: str,
        result_envelopes: List[ResultEnvelope],
        bash_excerpts: Dict[str, str],
        original_request: str,
        event_queue: asyncio.Queue,
        storage_base_path: str = "",
    ) -> str:
        """
        Final LLM call — streaming synthesis.
        Context composed from content_summary excerpts + bash excerpts only.
        """
        summary_text, auto_excerpts = await self.assemble_context(
            result_envelopes, storage_base_path
        )
        # Merge auto excerpts with provided bash_excerpts
        all_excerpts = {**auto_excerpts, **bash_excerpts}

        has_envelopes = bool(result_envelopes)
        context_parts = [f"Original user request:\n{original_request}"]
        if has_envelopes:
            context_parts += ["", "Task results summary:", summary_text]
        if all_excerpts:
            excerpt_lines = ["", "Additional file content:"]
            for label, excerpt in all_excerpts.items():
                excerpt_lines.append(f"### {label}\n{excerpt[:2000]}")
            context_parts.extend(excerpt_lines)

        # Note failed/skipped tasks
        failed = [e for e in result_envelopes if e.status in ("failed", "timeout")]
        if failed:
            context_parts.append(
                f"\nNote: {len(failed)} task(s) failed or timed out: "
                f"{', '.join(e.task_id.split('_', 1)[-1] for e in failed)}"
            )

        if self._config.agent.synthesis_guidance:
            context_parts.append(f"\n{self._config.agent.synthesis_guidance}")

        if has_envelopes:
            synthesis_system = (
                f"You are {self._config.agent.name}. "
                f"Synthesise the task results into a complete, coherent response for the user. "
                f"Use the task summaries provided — do not invent information not present in the summaries."
            )
        else:
            # No tasks ran — respond directly. Avoids the "I need task summaries"
            # meta-reply when this path is hit (e.g. RPC caller sent a
            # non-actionable input, or IntentGate is disabled).
            synthesis_system = (
                f"You are {self._config.agent.name}. {self._config.agent.description} "
                f"Respond directly and concisely to the user's request below."
            )

        await event_queue.put(StatusEvent(
            message="Synthesising final response...",
            session_id=session_id,
            event_type=EventType.STATUS,
        ))

        full_response = ""
        async for token in self._llm.stream(
            messages=[{"role": "user", "content": "\n".join(context_parts)}],
            system=synthesis_system,
            provider_name="default",
        ):
            full_response += token
            await event_queue.put(ResultEvent(
                content=token,
                session_id=session_id,
                partial=True,
            ))

        await event_queue.put(ResultEvent(
            content=full_response,
            session_id=session_id,
            partial=False,
        ))

        logger.info("Synthesis complete for session %s (%d chars)", session_id, len(full_response))
        return full_response

    async def remediate(
        self,
        session_id: str,
        original_request: str,
        original_response: str,
        validation_findings: List[ValidationFinding],
        event_queue: asyncio.Queue,
    ) -> str:
        """
        Called when ValidationAgent scores below threshold but above critical.
        Single LLM call to correct specific findings.
        """
        findings_text = "\n".join(
            f"- [{f.dimension}] {f.issue} → Suggestion: {f.suggestion}"
            for f in validation_findings
        ) or "General quality improvement needed."

        remediation_prompt = (
            f"The following response to the user request needs improvement:\n\n"
            f"USER REQUEST:\n{original_request}\n\n"
            f"ORIGINAL RESPONSE:\n{original_response}\n\n"
            f"QUALITY ISSUES FOUND:\n{findings_text}\n\n"
            f"Please provide a corrected response that addresses all the issues above. "
            f"Return only the corrected response, no meta-commentary."
        )

        await event_queue.put(StatusEvent(
            message="Improving response quality...",
            session_id=session_id,
            event_type=EventType.STATUS,
        ))

        corrected = ""
        async for token in self._llm.stream(
            messages=[{"role": "user", "content": remediation_prompt}],
            system=f"You are {self._config.agent.name}. Improve the response as directed.",
            provider_name="default",
        ):
            corrected += token
            await event_queue.put(ResultEvent(
                content=token,
                session_id=session_id,
                partial=True,
            ))

        await event_queue.put(ResultEvent(
            content=corrected,
            session_id=session_id,
            partial=False,
        ))

        logger.info("Remediation complete for session %s", session_id)
        return corrected

    async def generate_blueprint_updates(
        self,
        session_id: str,
        task_inputs: List[dict],
        clarifications: List[str],
        validation_findings: List[str],
    ) -> Dict[str, dict]:
        """One batched LLM call that produces a structured blueprint update
        for every task that has a blueprint configured.

        ``task_inputs`` is a list of dicts with keys:
            task_name, complexity, instruction, summary, status,
            existing_topology, existing_discovery_hints,
            existing_preconditions, existing_known_failure_modes,
            existing_dos, existing_donts.

        Returns a dict keyed by ``task_name`` with the schema accepted by
        :meth:`cortex.modules.blueprint_store.Blueprint.merge_update`:
            {
              "topology": str,             # pinned tasks only
              "discovery_hints": str,      # adaptive tasks only
              "preconditions": [str],
              "known_failure_modes": [str],
              "dos": [str],
              "donts": [str],
              "clarifications": [str],
              "lesson_summary": str,
            }

        Failures are logged and return an empty dict so consent-gated
        persistence never blocks a session on LLM/infra issues.
        """
        if not task_inputs:
            return {}

        system = (
            "You curate per-task 'blueprints' that guide an AI agent on how to execute a "
            "recurring task. Given each task's instruction, output summary, user clarifications, "
            "and validation findings, produce a concise structured update the framework will "
            "merge into the stored blueprint.\n\n"
            "Rules:\n"
            "- Be specific and actionable. Generic advice ('do a good job') is forbidden.\n"
            "- For scripted tasks (complexity=scripted): the task runs a Python handler — "
            "  leave both 'topology' and 'discovery_hints' empty; focus on preconditions and failure modes.\n"
            "- For pinned tasks (complexity=pinned): populate 'topology' with a clear prose "
            "  description of the subtask dependency graph (which subtasks run in parallel, which "
            "  are serial, their exact order) distilled from what actually executed. This topology "
            "  will be injected as a hard constraint on future runs. Leave 'discovery_hints' empty.\n"
            "- For adaptive tasks (complexity=adaptive): populate 'discovery_hints' with soft "
            "  navigation guidance (heuristics, common patterns, what to probe first). The LLM "
            "  will decompose freely but be steered by these hints. Leave 'topology' empty.\n"
            "- preconditions: entry conditions that must hold before this task starts. "
            "  Only add NEW ones not already in the existing list.\n"
            "- known_failure_modes: failure patterns observed this session. "
            "  Only add NEW ones not already in the existing list.\n"
            "- dos/donts: short imperative bullets. Only NEW guidance not already present.\n"
            "- clarifications: Q/A pairs surfaced this session, formatted as 'Q: ... A: ...'.\n"
            "- lesson_summary: one sentence capturing the single most important takeaway.\n"
            "- If there is nothing new for a field, omit it or return it empty.\n\n"
            "Respond with EXACTLY one JSON object and nothing else:\n"
            '  {"updates": {"<task_name>": {'
            '"topology": "...", "discovery_hints": "...", '
            '"preconditions": ["..."], "known_failure_modes": ["..."], '
            '"dos": ["..."], "donts": ["..."], '
            '"clarifications": ["..."], "lesson_summary": "..."}}}'
        )

        payload_lines = []
        if clarifications:
            payload_lines.append("Session clarifications:")
            payload_lines.extend(f"- {c}" for c in clarifications)
            payload_lines.append("")
        if validation_findings:
            payload_lines.append("Validation findings:")
            payload_lines.extend(f"- {f}" for f in validation_findings)
            payload_lines.append("")
        payload_lines.append("Tasks:")
        for t in task_inputs:
            payload_lines.append(f"### task_name: {t.get('task_name', '')}")
            payload_lines.append(f"complexity: {t.get('complexity', 'adaptive')}")
            payload_lines.append(f"status: {t.get('status', '')}")
            payload_lines.append(f"instruction: {t.get('instruction', '')}")
            summary = (t.get("summary") or "").strip()
            if summary:
                payload_lines.append(f"output_summary: {summary[:1000]}")
            et = (t.get("existing_topology") or "").strip()
            if et:
                payload_lines.append(f"existing_topology: {et[:400]}")
            eh = (t.get("existing_discovery_hints") or "").strip()
            if eh:
                payload_lines.append(f"existing_discovery_hints: {eh[:400]}")
            if t.get("existing_preconditions"):
                payload_lines.append(
                    "existing_preconditions: " + "; ".join(t["existing_preconditions"][:8])
                )
            if t.get("existing_known_failure_modes"):
                payload_lines.append(
                    "existing_known_failure_modes: "
                    + "; ".join(t["existing_known_failure_modes"][:8])
                )
            if t.get("existing_dos"):
                payload_lines.append("existing_dos: " + "; ".join(t["existing_dos"][:12]))
            if t.get("existing_donts"):
                payload_lines.append("existing_donts: " + "; ".join(t["existing_donts"][:12]))
            payload_lines.append("")

        user_msg = "\n".join(payload_lines)

        try:
            response = await self._llm.complete(
                messages=[{"role": "user", "content": user_msg}],
                system=system,
                provider_name="default",
                max_tokens=1200,
            )
            raw = (response.content or "").strip()
            if raw.startswith("```"):
                raw = raw.split("```", 2)[1]
                if raw.lower().startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            parsed = json.loads(raw)
            updates = parsed.get("updates") or {}
            if not isinstance(updates, dict):
                return {}
            # keep only entries for requested tasks
            allowed = {t.get("task_name") for t in task_inputs}
            return {k: v for k, v in updates.items() if k in allowed and isinstance(v, dict)}
        except Exception as e:
            logger.warning(
                "Blueprint LLM update failed for session %s: %s",
                session_id, e,
            )
            return {}

    async def validate_task_output(
        self,
        task,
        envelope,
        validation_notes: str,
    ) -> Optional[str]:
        """Wave-level LLM judge: validate a single task output against free-text rules.

        Returns None if acceptable, otherwise a concise feedback string describing
        what's wrong. Feedback is threaded back into the sub-agent on retry by
        the wave validation gate in framework.py.

        Uses the provider configured at `validation.wave_gate_llm_provider`
        (default: "default"). Failures during the judge call are logged and
        treated as PASS so validation never blocks a session on infra issues.
        """
        provider = "default"
        try:
            provider = self._config.validation.wave_gate_llm_provider or "default"
        except Exception:
            pass

        instruction = getattr(task, "instruction", "") or ""
        task_name = getattr(task, "task_name", "") or getattr(task, "name", "") or "task"
        summary = ""
        try:
            summary = envelope.content_summary or ""
        except Exception:
            summary = str(envelope)[:2000]

        system = (
            "You are a strict but fair task-output judge for an AI agent framework. "
            "You will be given a sub-task's instruction, the developer's validation rules, "
            "and the agent's produced output summary. Decide whether the output satisfies "
            "the validation rules in the context of the instruction.\n\n"
            "Respond with EXACTLY one JSON object and nothing else:\n"
            '  {"verdict": "pass"}  — if the output satisfies the rules\n'
            '  {"verdict": "fail", "feedback": "<concise actionable feedback>"}  — otherwise\n'
            "Feedback must be short (≤3 sentences), specific, and actionable so the "
            "agent can fix the issue on retry. Do not include any prose outside the JSON."
        )
        user_msg = (
            f"Task name: {task_name}\n"
            f"Task instruction:\n{instruction}\n\n"
            f"Validation rules (developer-defined):\n{validation_notes}\n\n"
            f"Agent output summary:\n{summary}"
        )

        try:
            response = await self._llm.complete(
                messages=[{"role": "user", "content": user_msg}],
                system=system,
                provider_name=provider,
                max_tokens=400,
            )
            raw = (response.content or "").strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            parsed = json.loads(raw)
            verdict = str(parsed.get("verdict", "")).lower()
            if verdict == "pass":
                return None
            if verdict == "fail":
                feedback = str(parsed.get("feedback", "")).strip()
                return feedback or "Output did not meet validation rules."
            logger.warning("Wave judge returned unknown verdict: %r — passing", verdict)
            return None
        except Exception as e:
            logger.warning(
                "Wave judge LLM call failed for task %s (%s) — passing",
                task_name, e,
            )
            return None

    async def replan(
        self,
        runtime_graph,
        completed_envelopes,
        task_compiler,
        event_queue,
        principal=None,
    ) -> None:
        """Mid-session replanning — invoked by the wave loop only when a stale
        task completes its wave or a mandatory task fails.

        Contract: this method may only inspect completed envelopes and modify
        pending tasks (status == 'pending'). It MUST NOT mutate tasks whose
        status is 'complete' or 'failed' — those represent state the user may
        already have observed via streaming.

        Makes a single non-streaming LLM call. Parses add / remove / modify
        instructions and applies them to the pending task list. Add operations
        are logged but not yet applied (requires task_compiler.add_task support).
        Failures are silently swallowed — replan must never block a session.
        """
        pending_tasks = [
            t for t in runtime_graph.tasks.values() if t.status == "pending"
        ]
        # Pending tasks may be empty when replan fires for "adaptive task
        # completed" — that's exactly the case where add ops are useful, so
        # don't early-return unless there's also nothing to add against.
        if not pending_tasks and not completed_envelopes:
            return

        # Summarise completed work (cap to last 10 envelopes to keep prompt small)
        completed_lines = []
        for env in completed_envelopes[-10:]:
            label = env.task_id.split("_", 1)[-1] if "_" in env.task_id else env.task_id
            icon = "✓" if env.status == "complete" else "✗"
            snippet = (env.content_summary or "")[:300].replace("\n", " ")
            completed_lines.append(f"- {label} [{icon}]: {snippet}")

        pending_names = [t.task_name for t in pending_tasks]

        # Available task types the replanner can pick from when adding.
        # Constrain to the declared set so we don't invent capability hints
        # at runtime — add_tasks_batch will reject anything unknown anyway.
        available_types = [
            f"- {tt.name} ({getattr(tt, 'capability_hint', 'auto')}): "
            f"{(tt.description or '')[:120]}"
            for tt in self._config.task_types
        ]

        scratchpad_block = (
            f"\n\n## Reasoning Scratchpad (accumulated this session)\n{self._scratchpad}"
            if self._scratchpad else ""
        )

        system = (
            "You are a session replanner for an AI agent framework. "
            "Given the results of completed tasks and the remaining pending tasks, "
            "decide whether the plan needs adjustment based on what was learned. "
            "You can remove, modify, or ADD tasks to the graph.\n\n"
            "Operations:\n"
            "- 'remove' — drop a pending task that is now redundant or impossible.\n"
            "- 'modify' — rewrite a pending task's instruction in light of new info.\n"
            "- 'add' — introduce a NEW task the initial plan didn't anticipate "
            "  (e.g. verify a suspicious result, read an extra file, run a fix "
            "  after a failed test). New tasks may depend on tasks already "
            "  completed OR on other newly-added tasks in this same batch.\n\n"
            "Rules:\n"
            "- Only propose changes when completed results clearly justify them. "
            "  Minimal edits preferred; an empty changes list is valid and often best.\n"
            "- Do NOT remove mandatory tasks unless their work was fully covered.\n"
            "- For add ops: 'task_type' MUST be one of the types listed below — "
            "  you cannot invent new types. 'depends_on' is a list of task_name "
            "  strings. Never re-add work that is already pending or completed.\n\n"
            "You must also update the reasoning scratchpad: a concise structured note "
            "(max 300 words) accumulating what has been confirmed, what is still open, "
            "and any strategy adjustments. This replaces the previous scratchpad entirely.\n\n"
            "Respond with EXACTLY one JSON object and nothing else:\n"
            "  {\"changes\": [\n"
            "    {\"op\": \"remove\", \"task_name\": \"...\"},\n"
            "    {\"op\": \"modify\", \"task_name\": \"...\", \"instruction\": \"...\"},\n"
            "    {\"op\": \"add\", \"task_name\": \"...\", \"task_type\": \"...\", "
            "\"instruction\": \"...\", \"depends_on\": [\"...\"]}\n"
            "  ],\n"
            "  \"scratchpad\": \"### Confirmed:\\n...\\n### Open:\\n...\\n### Strategy:\\n...\"\n"
            "  }\n"
            + scratchpad_block
        )

        user_msg = (
            "Completed tasks:\n"
            + ("\n".join(completed_lines) if completed_lines else "  (none)")
            + "\n\nPending tasks:\n"
            + ("\n".join(f"- {n}" for n in pending_names) if pending_names else "  (none)")
            + "\n\nAvailable task types (for 'add' ops):\n"
            + "\n".join(available_types)
        )

        try:
            response = await self._llm.complete(
                messages=[{"role": "user", "content": user_msg}],
                system=system,
                provider_name="default",
                max_tokens=1000,
            )
            raw = (response.content or "").strip()
            if raw.startswith("```"):
                raw = raw.split("```", 2)[1]
                if raw.lower().startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            parsed = json.loads(raw)
            changes = parsed.get("changes") or []

            # Persist the updated scratchpad regardless of whether there are changes.
            updated_scratchpad = (parsed.get("scratchpad") or "").strip()
            if updated_scratchpad:
                self._scratchpad = updated_scratchpad
                logger.debug("Replan: scratchpad updated (%d chars)", len(self._scratchpad))

            if not changes:
                logger.debug("Replan: no changes needed")
                return

            applied = 0
            add_batch: List[dict] = []
            for change in changes:
                op = str(change.get("op", "")).lower()
                task_name = str(change.get("task_name", "")).strip()
                if not task_name:
                    continue

                if op == "remove":
                    for t in pending_tasks:
                        if t.task_name == task_name and t.status == "pending":
                            task_compiler.mark_failed(runtime_graph, t.task_id)
                            logger.info("Replan: removed pending task '%s'", task_name)
                            applied += 1
                            break

                elif op == "modify":
                    new_instruction = str(change.get("instruction", "")).strip()
                    if new_instruction:
                        for t in pending_tasks:
                            if t.task_name == task_name and t.status == "pending":
                                t.instruction = new_instruction
                                logger.info(
                                    "Replan: updated instruction for pending task '%s'",
                                    task_name,
                                )
                                applied += 1
                                break

                elif op == "add":
                    add_batch.append({
                        "task_name": task_name,
                        "task_type": str(change.get("task_type", "")).strip(),
                        "instruction": str(change.get("instruction", "")).strip(),
                        "depends_on": change.get("depends_on") or [],
                        "mandatory": change.get("mandatory"),
                    })

            if add_batch:
                effective_types = {tt.name: tt for tt in self._config.task_types}
                committed = task_compiler.add_tasks_batch(
                    runtime_graph, add_batch, effective_types,
                    principal=principal,
                )
                applied += len(committed)

            if applied:
                await event_queue.put(StatusEvent(
                    message=f"Replanning applied {applied} adjustment(s) to pending tasks.",
                    session_id="",
                    event_type=EventType.STATUS,
                ))
                logger.info("Replan: %d change(s) applied to pending tasks", applied)

        except Exception as e:
            logger.warning("Replan LLM call failed (%s) — skipping", e)
