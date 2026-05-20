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
from cortex.prompts import (
    BLUEPRINT_SYSTEM,
    CONVERSE_INTRO,
    DECOMP_BLUEPRINT_HEADER,
    DECOMP_BLUEPRINT_INTRO,
    DECOMP_CAPABILITIES_GUIDANCE,
    DECOMP_CAPABILITIES_HEADER,
    DECOMP_CAPABILITIES_SELECT,
    DECOMP_CAPABILITY_DESCRIPTIONS,
    DECOMP_CLARIFICATION_HEADER,
    DECOMP_CLARIFICATION_INTRO,
    DECOMP_FORMAT_BLOCK_WITH_AMR,
    DECOMP_FORMAT_BLOCK_WITHOUT_AMR,
    DECOMP_FORMAT_HEADER,
    DECOMP_FORMAT_INTRO,
    DECOMP_FORMAT_SUFFIX,
    DECOMP_MCP_NO_TYPES_INTRO,
    DECOMP_MCP_TOOLS_HEADER,
    DECOMP_MCP_WITH_TYPES_INTRO,
    DECOMP_PREBUILT_SCRIPTS_HEADER,
    DECOMP_PREBUILT_SCRIPTS_INTRO,
    DECOMP_SYNTHESIS_GUIDANCE_HEADER,
    DECOMP_TASK_TYPES_HEADER,
    FILE_SUMMARY_SYSTEM,
    FILE_SUMMARY_USER,
    REMEDIATE_PRIOR_ATTEMPT,
    REMEDIATE_SYSTEM,
    REMEDIATE_USER,
    INTERRUPT_REPLAN_SYSTEM,
    INTERRUPT_REPLAN_USER,
    REPLAN_SCRATCHPAD_BLOCK,
    REPLAN_SYSTEM,
    REPLAN_USER,
    SYNTHESIS_SYSTEM_DIRECT,
    SYNTHESIS_SYSTEM_WITH_RESULTS,
    TASK_VALIDATE_SYSTEM,
    TASK_VALIDATE_USER,
)
from cortex.streaming.status_events import (
    ClarificationEvent, EventType, ResultEvent, StatusEvent, UserInterruptEvent
)

logger = logging.getLogger(__name__)

# ── Synthesis internal constants (not developer-configurable) ─────────────────
_EXCERPT_MAX_CHARS = 8_000       # Tier 1: per-file grep/head cap (was 2000)
_ITERATIVE_MAX_FILES = 3         # Tier 2: max concurrent file-summarise LLM calls
_ITERATIVE_SUMMARY_TOKENS = 400  # Tier 2: token budget per file summary


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
        lines.append(DECOMP_PREBUILT_SCRIPTS_HEADER)
        lines.append(DECOMP_PREBUILT_SCRIPTS_INTRO)
        for script in scout_result.code_utils:
            use_str = f" (used {script.use_count}×)" if script.use_count else ""
            desc = f": {script.description}" if script.description else ""
            lines.append(f"- {script.task_name}{use_str}{desc}")
        lines.append("")

    # ── Task type vocabulary ────────────────────────────────────────────────
    if has_predefined_tasks:
        lines.append(DECOMP_TASK_TYPES_HEADER)
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
        lines.append(DECOMP_MCP_TOOLS_HEADER)
        if not has_predefined_tasks:
            lines.append(DECOMP_MCP_NO_TYPES_INTRO)
        else:
            lines.append(DECOMP_MCP_WITH_TYPES_INTRO)
        lines.append("")
        for cap, tools in scout_result.tools_by_capability().items():
            lines.append(f"Capability: {cap}")
            for t in tools:
                desc = f" — {t.description}" if t.description else ""
                lines.append(f"  - {t.name}{desc}")
        lines.append("")
    # Always surface available capabilities so the decomposition LLM can set
    # the <capability> field even when task_types or scout tools are defined.
    if capabilities:
        lines += [DECOMP_CAPABILITIES_HEADER]
        lines.append(DECOMP_CAPABILITIES_SELECT)
        for cap in sorted(capabilities):
            desc = DECOMP_CAPABILITY_DESCRIPTIONS.get(cap, "")
            lines.append(f"  - {cap}" + (f": {desc}" if desc else ""))
        lines += [
            "",
            DECOMP_CAPABILITIES_GUIDANCE,
            "",
        ]

    # ── Task blueprints (accumulated guidance from prior runs) ──────────────
    if blueprint_blocks:
        lines.append(DECOMP_BLUEPRINT_HEADER)
        lines.append(DECOMP_BLUEPRINT_INTRO)
        lines.append("")
        for block in blueprint_blocks:
            lines.append(block)
            lines.append("")

    # ── Decomposition format ────────────────────────────────────────────────
    amr = config.adaptive_model_routing
    if amr.enabled:
        lines += [DECOMP_FORMAT_HEADER, DECOMP_FORMAT_INTRO] + DECOMP_FORMAT_BLOCK_WITH_AMR
    else:
        lines += [DECOMP_FORMAT_HEADER, DECOMP_FORMAT_INTRO] + DECOMP_FORMAT_BLOCK_WITHOUT_AMR
    guidance_parts = []
    if has_scripts:
        guidance_parts.append("prefer pre-built script names when they match")
    if has_predefined_tasks:
        guidance_parts.append("use predefined task types for everything else")
    if has_scout_tools and not has_predefined_tasks:
        guidance_parts.append("use discovered tool names as task names")
    if guidance_parts:
        lines.append("Priority: " + ", then ".join(guidance_parts) + ".")
    lines.append(DECOMP_FORMAT_SUFFIX)

    if config.agent.clarification.enabled:
        lines += [
            "",
            DECOMP_CLARIFICATION_HEADER,
            DECOMP_CLARIFICATION_INTRO,
        ]

    if config.agent.synthesis_guidance:
        lines += [
            "",
            DECOMP_SYNTHESIS_GUIDANCE_HEADER,
            config.agent.synthesis_guidance,
        ]

    return "\n".join(lines)


def _parse_task_blocks(text: str) -> List[DecomposedTask]:
    """Parse <task> XML blocks from LLM decomposition stream."""
    tasks = []
    pattern = re.compile(
        r'<task>\s*'
        r'<name>(.*?)</name>\s*'
        r'(?:<capability>(.*?)</capability>\s*)?'
        r'(?:<model_tier>(.*?)</model_tier>\s*)?'
        r'<instruction>(.*?)</instruction>\s*'
        r'(?:<depends_on>(.*?)</depends_on>\s*)?'
        r'</task>',
        re.DOTALL | re.IGNORECASE,
    )
    _valid_tiers = {"low", "medium", "high"}
    for match in pattern.finditer(text):
        name = match.group(1).strip()
        capability_raw = (match.group(2) or "").strip()
        tier_raw = (match.group(3) or "").strip().lower()
        instruction = match.group(4).strip()
        depends_on_raw = (match.group(5) or "").strip()
        depends_on = [d.strip() for d in depends_on_raw.split(",") if d.strip()] if depends_on_raw else []
        if name:
            tasks.append(DecomposedTask(
                task_name=name,
                instruction=instruction,
                depends_on=depends_on,
                capability_hint=capability_raw or None,
                complexity_tier=tier_raw if tier_raw in _valid_tiers else None,
            ))
    return tasks


def _parse_clarification(text: str) -> Optional[str]:
    """Extract clarification question from stream."""
    match = re.search(r'<clarification>(.*?)</clarification>', text, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else None


def _amr_resolve_provider(config: "CortexConfig", task: "DecomposedTask") -> Optional[str]:
    """Return the AMR-assigned provider key for a task, or None if AMR is off.

    Only applies when adaptive_model_routing.enabled is True and the task has
    no explicit provider override (i.e. it is ad-hoc or its static config uses
    "default"). The tier emitted by the decomposer drives the lookup; unknown
    or missing tiers fall back to "medium".
    """
    amr = config.adaptive_model_routing
    if not amr.enabled:
        return None
    tier = task.complexity_tier or "medium"
    tiers = amr.tiers
    mapping = {"low": tiers.low, "medium": tiers.medium, "high": tiers.high}
    return mapping.get(tier, tiers.medium) or "default"


def _amr_validation_provider(config: "CortexConfig") -> str:
    """Return the provider key to use for wave-level task validation.

    Priority:
      1. adaptive_model_routing.validation_provider (explicit non-empty value)
      2. Auto-select: first key in llm_access.providers that is not "default"
      3. Fallback: "default"

    When AMR is disabled, falls back to validation.wave_gate_llm_provider
    (legacy field) or "default".
    """
    amr = config.adaptive_model_routing
    if not amr.enabled:
        return config.validation.wave_gate_llm_provider or "default"
    if amr.validation_provider:
        return amr.validation_provider
    # Auto-select first non-default named provider
    for key in config.llm_access.providers:
        if key != "default":
            return key
    return "default"


def _head_tail(text: str, head: int = 200, tail: int = 120) -> str:
    """Truncate while preserving both ends. Long task summaries often have
    decision-relevant tokens (URLs, IDs, error codes) at the tail that a plain
    `text[:N]` cut silently drops. Newlines collapsed to spaces so each entry
    fits on one line in the replan prompt."""
    s = (text or "").replace("\n", " ").strip()
    if len(s) <= head + tail + 5:
        return s
    return f"{s[:head]} … {s[-tail:]}"


def _format_history_record(rec: "HistoryRecord") -> str:
    """Render one prior session for the decomposition history snippet.

    Beyond request/summary, this surfaces the *outcome* — task completion
    counts and the validation verdict — so the decomposer has signal about
    whether a similar plan worked last time, not just what was asked."""
    lines = [
        f"[Prior session {rec.session_id[:8]}]",
        f"  Request: {_head_tail(rec.original_request or '', head=200, tail=80)}",
        f"  Summary: {_head_tail(rec.response_summary or '', head=240, tail=100)}",
    ]

    tc = rec.task_completion
    if tc and getattr(tc, "total_tasks", 0):
        outcome = f"{tc.completed_tasks}/{tc.total_tasks} tasks completed"
        problems = []
        if getattr(tc, "failed_tasks", 0):
            problems.append(f"{tc.failed_tasks} failed")
        if getattr(tc, "timed_out_tasks", 0):
            problems.append(f"{tc.timed_out_tasks} timed out")
        if problems:
            outcome += " (" + ", ".join(problems) + ")"
        lines.append(f"  Outcome: {outcome}")

    if rec.validation_score is not None:
        verdict = (
            "passed" if rec.validation_passed
            else "failed" if rec.validation_passed is False
            else "n/a"
        )
        lines.append(f"  Validation: {rec.validation_score:.2f} ({verdict})")

    return "\n".join(lines)


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
        # Estimated tokens consumed by this agent's own streaming LLM calls
        # (converse/decompose/synthesise). Streaming yields no usage object, so
        # this is a chars//4 estimate — the heuristic GenericMCPAgent also uses.
        self.primary_tokens: int = 0

    def reset_session_state(self) -> None:
        """Clear per-session state so a reused PrimaryAgent starts fresh."""
        self._scratchpad = ""
        self.primary_tokens = 0

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
            CONVERSE_INTRO,
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

        _in_chars = len(system_prompt) + sum(len(m.get("content", "")) for m in messages)
        self.primary_tokens += (_in_chars + len(full_response)) // 4

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

        # Build history context snippet. Each prior session contributes its
        # request/summary plus an outcome line (task completion + validation)
        # so the decomposer can learn from how similar requests fared — e.g.
        # avoid a plan shape that failed tasks last time.
        history_snippet = ""
        if history_context:
            snippets = [
                _format_history_record(rec)
                for rec in history_context[-self._config.history.max_sessions_in_context:]
            ]
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
                                    task.llm_provider = _amr_resolve_provider(self._config, task)
                                    yield task
                        return
                    except asyncio.TimeoutError:
                        logger.warning("Clarification timed out for session %s", session_id)

            # Parse tasks as they arrive in stream
            tasks = _parse_task_blocks(accumulated)
            for task in tasks:
                if task.task_name not in dispatched_tasks:
                    dispatched_tasks.add(task.task_name)
                    task.llm_provider = _amr_resolve_provider(self._config, task)
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

    async def _smart_excerpt(
        self,
        file_path: str,
        task_label: str,
        content_summary: str,
        storage_base_path: str,
    ) -> str:
        """Tier 1: extract a relevant excerpt from a file output.

        Builds a keyword pattern from the task label and summary, then greps
        the file for matching lines with context. Falls back to head if grep
        returns nothing. Hard-capped at _EXCERPT_MAX_CHARS.
        """
        from cortex.security.bash_sandbox import BashSandbox
        sandbox = BashSandbox(storage_base_path)

        # Build keyword pattern from task label tokens + first 80 chars of summary
        raw_keywords = re.sub(r"[^a-zA-Z0-9 ]", " ", task_label + " " + content_summary[:80])
        keywords = [w for w in raw_keywords.lower().split() if len(w) > 3][:6]
        excerpt = ""
        if keywords:
            pattern = "|".join(re.escape(k) for k in keywords)
            try:
                excerpt = await sandbox.execute(
                    f"grep -m 40 -i -n -E '{pattern}' '{file_path}' 2>/dev/null | head -c {_EXCERPT_MAX_CHARS}"
                )
            except Exception:
                excerpt = ""
        if not excerpt:
            try:
                excerpt = await sandbox.execute(
                    f"head -c {_EXCERPT_MAX_CHARS} '{file_path}'"
                )
            except Exception as e:
                logger.debug("Excerpt failed for %s: %s", task_label, e)
        return excerpt[:_EXCERPT_MAX_CHARS]

    async def assemble_context(
        self,
        result_envelopes: List[ResultEnvelope],
        storage_base_path: str = "",
    ) -> tuple[str, Dict[str, str]]:
        """Bash-assisted context assembly before synthesis.

        Returns (summary_text, bash_excerpts_dict).
        File-output tasks get a Tier 1 smart grep excerpt (up to
        _EXCERPT_MAX_CHARS) instead of the old hard head -c 2000 truncation.
        """
        summaries = []
        bash_excerpts: Dict[str, str] = {}

        for envelope in result_envelopes:
            status_icon = "✓" if envelope.status == "complete" else "✗"
            task_label = envelope.task_id.split("/", 1)[-1] if "/" in envelope.task_id else envelope.task_id

            if envelope.status == "complete":
                summaries.append(
                    f"## {task_label} [{status_icon}]\n{envelope.content_summary}"
                )
                if envelope.output_type == "file" and envelope.output_value and storage_base_path:
                    if envelope.output_value.startswith(storage_base_path):
                        excerpt = await self._smart_excerpt(
                            file_path=envelope.output_value,
                            task_label=task_label,
                            content_summary=envelope.content_summary or "",
                            storage_base_path=storage_base_path,
                        )
                        if excerpt:
                            bash_excerpts[task_label] = excerpt
            elif envelope.status == "failed":
                summaries.append(
                    f"## {task_label} [FAILED]\nError: {envelope.error or 'Unknown error'}"
                )
            elif envelope.status == "timeout":
                summaries.append(f"## {task_label} [TIMED OUT]")
            else:
                summaries.append(f"## {task_label} [{envelope.status}]")

        return "\n\n".join(summaries), bash_excerpts

    async def _summarise_file_for_synthesis(
        self,
        task_label: str,
        file_path: str,
        instruction: str,
    ) -> str:
        """Tier 2: LLM-based summary of a single file output.

        Called concurrently for up to _ITERATIVE_MAX_FILES file-output envelopes
        before the final synthesis pass. Falls back to an empty string on any
        failure so it never blocks synthesis.
        """
        try:
            user_msg = FILE_SUMMARY_USER.format(
                task_label=task_label,
                instruction_excerpt=instruction[:300],
                file_path=file_path,
            )
            response = await self._llm.complete(
                messages=[{"role": "user", "content": user_msg}],
                system=FILE_SUMMARY_SYSTEM,
                provider_name="default",
                max_tokens=_ITERATIVE_SUMMARY_TOKENS,
            )
            return (response.content or "").strip()
        except Exception as e:
            logger.debug("Tier 2 file summary failed for %s: %s", task_label, e)
            return ""

    async def synthesise(
        self,
        session_id: str,
        result_envelopes: List[ResultEnvelope],
        bash_excerpts: Dict[str, str],
        original_request: str,
        event_queue: asyncio.Queue,
        storage_base_path: str = "",
        scratchpad: str = "",
    ) -> str:
        """Final LLM call — streaming synthesis.

        Context: task summaries + Tier 1 smart excerpts for file outputs.
        When multiple file-output envelopes exist, Tier 2 fires concurrently
        (up to _ITERATIVE_MAX_FILES) to produce richer LLM summaries before
        the synthesis pass. Output is written to a file when file-output
        envelopes are present; otherwise returned as a text stream.
        Scratchpad (accumulated session reasoning from replanning) is injected
        when non-empty so the synthesis is aware of confirmed facts and strategy.
        """
        summary_text, auto_excerpts = await self.assemble_context(
            result_envelopes, storage_base_path
        )
        all_excerpts = {**auto_excerpts, **bash_excerpts}

        # ── Tier 2: concurrent LLM summaries for file outputs ─────────────────
        file_envelopes = [
            e for e in result_envelopes
            if e.status == "complete"
            and e.output_type == "file"
            and e.output_value
            and (not storage_base_path or e.output_value.startswith(storage_base_path))
        ]
        if file_envelopes:
            tier2_targets = file_envelopes[:_ITERATIVE_MAX_FILES]
            tier2_results = await asyncio.gather(*[
                self._summarise_file_for_synthesis(
                    task_label=e.task_id.split("/", 1)[-1] if "/" in e.task_id else e.task_id,
                    file_path=e.output_value,
                    instruction=e.content_summary or "",
                )
                for e in tier2_targets
            ])
            for envelope, summary in zip(tier2_targets, tier2_results):
                if summary:
                    label = envelope.task_id.split("/", 1)[-1] if "/" in envelope.task_id else envelope.task_id
                    all_excerpts[label] = summary  # replaces Tier 1 excerpt for this file

        has_envelopes = bool(result_envelopes)
        has_file_outputs = bool(file_envelopes)

        context_parts = [f"Original user request:\n{original_request}"]
        if has_envelopes:
            context_parts += ["", "Task results summary:", summary_text]
        if all_excerpts:
            excerpt_lines = ["", "File content:"]
            for label, excerpt in all_excerpts.items():
                excerpt_lines.append(f"### {label}\n{excerpt[:_EXCERPT_MAX_CHARS]}")
            context_parts.extend(excerpt_lines)

        failed = [e for e in result_envelopes if e.status in ("failed", "timeout")]
        if failed:
            context_parts.append(
                f"\nNote: {len(failed)} task(s) failed or timed out: "
                f"{', '.join(e.task_id.split('_', 1)[-1] for e in failed)}"
            )

        if scratchpad:
            context_parts += ["", "## Session Reasoning", scratchpad]

        if self._config.agent.synthesis_guidance:
            context_parts.append(f"\n{self._config.agent.synthesis_guidance}")

        if has_envelopes:
            synthesis_system = SYNTHESIS_SYSTEM_WITH_RESULTS.format(
                agent_name=self._config.agent.name,
            )
        else:
            synthesis_system = SYNTHESIS_SYSTEM_DIRECT.format(
                agent_name=self._config.agent.name,
                agent_description=self._config.agent.description,
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

        # ── File output: write synthesis to disk when tasks produced files ─────
        if has_file_outputs and storage_base_path:
            import aiofiles
            import os
            out_path = os.path.join(storage_base_path, f"synthesis_{session_id}.md")
            try:
                async with aiofiles.open(out_path, "w", encoding="utf-8") as fh:
                    await fh.write(full_response)
                await event_queue.put(ResultEvent(
                    content=out_path,
                    session_id=session_id,
                    partial=False,
                    metadata={"output_type": "file"},
                ))
                logger.info("Synthesis written to file %s", out_path)
                return out_path
            except Exception as e:
                logger.warning("Could not write synthesis file %s: %s — returning text", out_path, e)

        await event_queue.put(ResultEvent(
            content=full_response,
            session_id=session_id,
            partial=False,
        ))

        logger.info("Synthesis complete for session %s (%d chars)", session_id, len(full_response))
        return full_response

    @staticmethod
    def _format_findings(findings: List[ValidationFinding]) -> str:
        """Render validation findings as a bullet list for a remediation prompt."""
        return "\n".join(
            f"- [{f.dimension}] {f.issue} → Suggestion: {f.suggestion}"
            for f in findings
        ) or "General quality improvement needed."

    async def remediate(
        self,
        session_id: str,
        original_request: str,
        original_response: str,
        validation_findings: List[ValidationFinding],
        event_queue: asyncio.Queue,
        prior_attempts: Optional[List[tuple]] = None,
        stream: bool = True,
    ) -> str:
        """
        Called when ValidationAgent scores below threshold but above critical.
        One LLM call to correct specific findings.

        ``prior_attempts`` is a list of ``(response, findings)`` tuples from
        earlier remediation passes in this session — empty/None on the first
        pass. When present, each is rendered into the prompt so this pass does
        not repeat a correction that already proved insufficient.

        ``stream`` controls token emission. When the caller may run several
        passes (iterative remediation), it sets ``stream=False`` so discarded
        intermediate attempts are not streamed to the user — the caller emits
        the single chosen response itself once all passes are done.
        """
        findings_text = self._format_findings(validation_findings)

        prior_attempts_block = ""
        for i, (att_response, att_findings) in enumerate(prior_attempts or [], start=1):
            att_findings_text = (
                self._format_findings(att_findings)
                if att_findings and not isinstance(att_findings, str)
                else (att_findings or "General quality improvement needed.")
            )
            prior_attempts_block += REMEDIATE_PRIOR_ATTEMPT.format(
                n=i,
                attempt_response=att_response,
                attempt_findings=att_findings_text,
            )

        remediation_prompt = REMEDIATE_USER.format(
            original_request=original_request,
            original_response=original_response,
            findings_text=findings_text,
            prior_attempts_block=prior_attempts_block,
        )

        await event_queue.put(StatusEvent(
            message="Improving response quality...",
            session_id=session_id,
            event_type=EventType.STATUS,
        ))

        corrected = ""
        async for token in self._llm.stream(
            messages=[{"role": "user", "content": remediation_prompt}],
            system=REMEDIATE_SYSTEM.format(agent_name=self._config.agent.name),
            provider_name="default",
        ):
            corrected += token
            if stream:
                await event_queue.put(ResultEvent(
                    content=token,
                    session_id=session_id,
                    partial=True,
                ))

        if stream:
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

        system = BLUEPRINT_SYSTEM

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
            provider = _amr_validation_provider(self._config)
        except Exception:
            pass

        instruction = getattr(task, "instruction", "") or ""
        task_name = getattr(task, "task_name", "") or getattr(task, "name", "") or "task"
        summary = ""
        try:
            summary = envelope.content_summary or ""
        except Exception:
            summary = str(envelope)[:2000]

        system = TASK_VALIDATE_SYSTEM
        user_msg = TASK_VALIDATE_USER.format(
            task_name=task_name,
            instruction=instruction,
            validation_notes=validation_notes,
            summary=summary,
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
        trigger_reason: str = "unspecified",
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

        ``trigger_reason`` is a short label the wave loop passes so the prompt
        tells the LLM *why* it was woken (e.g. 'mandatory_failure',
        'stale_blueprint', 'adaptive_completed'). Without it the model has to
        infer intent from completed-task content alone, which is brittle.
        """
        pending_tasks = [
            t for t in runtime_graph.tasks.values() if t.status == "pending"
        ]
        # Pending tasks may be empty when replan fires for "adaptive task
        # completed" — that's exactly the case where add ops are useful, so
        # don't early-return unless there's also nothing to add against.
        if not pending_tasks and not completed_envelopes:
            return

        # Summarise completed work (cap to last 10 envelopes to keep prompt small).
        # Head+tail truncation preserves URLs / identifiers that often live at
        # the end of a summary and are exactly what a replan decision hinges on.
        completed_lines = []
        for env in completed_envelopes[-10:]:
            label = env.task_id.split("/", 1)[-1] if "/" in env.task_id else env.task_id
            icon = "✓" if env.status == "complete" else "✗"
            snippet = _head_tail(env.content_summary or "", head=200, tail=120)
            completed_lines.append(f"- {label} [{icon}]: {snippet}")

        # Pending tasks: render instruction + depends_on so the LLM can issue
        # informed 'modify' / 'remove' ops without having to guess the body.
        pending_lines = []
        for t in pending_tasks:
            instr = _head_tail(t.instruction or "", head=200, tail=80)
            deps = ", ".join(t.depends_on) if t.depends_on else "(none)"
            pending_lines.append(
                f"- {t.task_name}\n"
                f"    instruction: {instr}\n"
                f"    depends_on: {deps}"
            )

        # Available task types the replanner can pick from when adding.
        # Constrain to the declared set so we don't invent capability hints
        # at runtime — add_tasks_batch will reject anything unknown anyway.
        available_types = [
            f"- {tt.name} ({getattr(tt, 'capability_hint', 'auto')}): "
            f"{(tt.description or '')[:120]}"
            for tt in self._config.task_types
        ]

        scratchpad_block = (
            REPLAN_SCRATCHPAD_BLOCK.format(scratchpad=self._scratchpad)
            if self._scratchpad else ""
        )

        system = REPLAN_SYSTEM.format(scratchpad_block=scratchpad_block)

        user_msg = REPLAN_USER.format(
            trigger_reason=trigger_reason or "unspecified",
            completed_tasks="\n".join(completed_lines) if completed_lines else "  (none)",
            pending_tasks="\n".join(pending_lines) if pending_lines else "  (none)",
            available_types="\n".join(available_types),
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

    # ── Terminate keywords for fast-path detection ────────────────────────────
    _TERMINATE_KEYWORDS = frozenset({
        "stop", "cancel", "terminate", "abort", "quit", "halt", "kill",
        "exit", "end", "cease", "drop", "enough", "nevermind", "forget",
    })

    async def handle_user_interrupt(
        self,
        message: str,
        session_id: str,
        runtime_graph,
        completed_envelopes,
        task_compiler,
        event_queue,
        principal=None,
    ) -> str:
        """Process a user message injected mid-run.

        Fast-path: if the message contains a clear termination keyword AND no
        pending tasks were described (e.g. "stop", "cancel that"), return
        "terminate" immediately without an LLM call.

        Otherwise: make one non-streaming LLM call to decide between
        "terminate" and "replan", then apply any task-graph changes.

        Returns:
            "terminate" — caller should break the wave loop and wind down.
            "replan"    — graph has been updated; caller continues wave loop.
        """
        tokens = set(message.lower().split())
        is_short = len(tokens) <= 6
        looks_like_stop = bool(tokens & self._TERMINATE_KEYWORDS)

        # Fast-path: short message with a termination keyword — skip LLM
        if is_short and looks_like_stop:
            await event_queue.put(UserInterruptEvent(
                session_id=session_id,
                message=message,
                action="terminate",
            ))
            await event_queue.put(StatusEvent(
                message="Session terminated by user request.",
                session_id=session_id,
                event_type=EventType.STATUS,
            ))
            logger.info("Interrupt fast-path: terminate (message=%r)", message)
            return "terminate"

        # Slow-path: ask the LLM to decide
        pending_tasks = [
            t for t in runtime_graph.tasks.values() if t.status == "pending"
        ]
        completed_lines = []
        for env in completed_envelopes[-10:]:
            label = env.task_id.split("/", 1)[-1] if "/" in env.task_id else env.task_id
            icon = "✓" if env.status == "complete" else "✗"
            snippet = _head_tail(env.content_summary or "", head=200, tail=120)
            completed_lines.append(f"- {label} [{icon}]: {snippet}")

        pending_lines = []
        for t in pending_tasks:
            instr = _head_tail(t.instruction or "", head=200, tail=80)
            deps = ", ".join(t.depends_on) if t.depends_on else "(none)"
            pending_lines.append(
                f"- {t.task_name}\n"
                f"    instruction: {instr}\n"
                f"    depends_on: {deps}"
            )
        available_types = [
            f"- {tt.name} ({getattr(tt, 'capability_hint', 'auto')}): "
            f"{(tt.description or '')[:120]}"
            for tt in self._config.task_types
        ]

        scratchpad_block = (
            REPLAN_SCRATCHPAD_BLOCK.format(scratchpad=self._scratchpad)
            if self._scratchpad else ""
        )

        system = INTERRUPT_REPLAN_SYSTEM.format(scratchpad_block=scratchpad_block)
        user_msg = INTERRUPT_REPLAN_USER.format(
            user_message=message,
            completed_tasks="\n".join(completed_lines) if completed_lines else "  (none)",
            pending_tasks="\n".join(pending_lines) if pending_lines else "  (none)",
            available_types="\n".join(available_types),
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
            action = str(parsed.get("action", "replan")).lower()

            if action == "terminate":
                reason = str(parsed.get("reason", "User requested stop.")).strip()
                await event_queue.put(UserInterruptEvent(
                    session_id=session_id,
                    message=message,
                    action="terminate",
                ))
                await event_queue.put(StatusEvent(
                    message=f"Session terminated by user: {reason}",
                    session_id=session_id,
                    event_type=EventType.STATUS,
                ))
                logger.info("Interrupt: terminate — %s", reason)
                return "terminate"

            # action == "replan"
            changes = parsed.get("changes") or []
            updated_scratchpad = (parsed.get("scratchpad") or "").strip()
            if updated_scratchpad:
                self._scratchpad = updated_scratchpad

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
                            logger.info("Interrupt replan: removed task '%s'", task_name)
                            applied += 1
                            break
                elif op == "modify":
                    new_instruction = str(change.get("instruction", "")).strip()
                    if new_instruction:
                        for t in pending_tasks:
                            if t.task_name == task_name and t.status == "pending":
                                t.instruction = new_instruction
                                logger.info("Interrupt replan: modified task '%s'", task_name)
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

            summary = (
                f"Interrupt processed: {applied} task adjustment(s) applied."
                if applied else
                "Interrupt acknowledged — no task changes needed."
            )
            await event_queue.put(UserInterruptEvent(
                session_id=session_id,
                message=message,
                action="replan",
            ))
            await event_queue.put(StatusEvent(
                message=summary,
                session_id=session_id,
                event_type=EventType.STATUS,
            ))
            logger.info("Interrupt: replan — %d change(s)", applied)
            return "replan"

        except Exception as e:
            logger.warning("Interrupt LLM call failed (%s) — treating as replan no-op", e)
            await event_queue.put(UserInterruptEvent(
                session_id=session_id,
                message=message,
                action="replan",
            ))
            return "replan"
