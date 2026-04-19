"""IntentGate — pre-scout classifier that routes each turn.

Sits in front of CapabilityScout + decomposition. Outputs one of three modes:

- ``chat``    — answer directly, skip scout/decompose (pure conversation).
- ``task``    — run the existing scout → decompose → execute → synthesise path.
- ``hybrid``  — conversational framing around a real task; task path runs, but
                decompose is told to strip chat preamble and synthesis uses
                a conversational tone.

Stage 1 (heuristics, ~0 cost) handles the common cases: greetings/ack go to
chat, obvious task verbs or file attachments go to task, direct references to
known task-type names also go to task. Stage 2 (small LLM call) only runs when
the heuristic confidence is below the configured threshold. In ``rpc``
interaction mode the gate short-circuits to ``task`` and never emits a
clarification request — an MCP caller cannot answer one.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

from cortex.config.schema import IntentGateConfig
from cortex.llm.client import LLMClient
from cortex.modules.history_store import HistoryRecord

logger = logging.getLogger(__name__)


# ─── decision object ─────────────────────────────────────────────────────────

@dataclass
class IntentDecision:
    mode: str  # "chat" | "task" | "hybrid"
    needs_clarify: bool = False
    clarify_q: Optional[str] = None
    # Optional capability names that Stage 2 guessed — fed to CapabilityScout
    # as a warm start. Ignored on chat turns.
    scout_hint: List[str] = field(default_factory=list)
    # "heuristic" | "llm" | "forced_rpc" | "heuristic_fallback"
    source: str = "heuristic"
    rationale: str = ""


# ─── heuristic lexicon ───────────────────────────────────────────────────────

_GREETING_TOKENS = {
    "hi", "hey", "hello", "yo", "sup", "hola", "howdy",
    "thanks", "thank", "thankyou", "ty", "cheers",
    "ok", "okay", "k", "kk", "cool", "nice", "great",
    "bye", "goodbye", "cya", "later",
    "yes", "no", "yep", "nope", "sure", "fine",
}

# Verbs that strongly imply a task when they appear near the start.
_TASK_VERBS = {
    "search", "fetch", "get", "find", "list", "show",
    "summarize", "summarise", "compile", "build", "create",
    "generate", "write", "draft", "make", "produce",
    "analyze", "analyse", "compare", "diff", "review",
    "translate", "convert", "extract", "download", "upload",
    "send", "post", "email", "call", "run", "execute",
    "query", "fix", "refactor", "implement", "deploy",
    "schedule", "remind", "plan",
}

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_'-]*")


def _tokens(text: str) -> List[str]:
    return [t.lower() for t in _WORD_RE.findall(text or "")]


# ─── the gate ────────────────────────────────────────────────────────────────

class IntentGate:
    def __init__(self, config: IntentGateConfig, llm_client: LLMClient):
        self._config = config
        self._llm = llm_client

    # ---- public entry point ------------------------------------------------

    async def classify(
        self,
        request: str,
        history: Optional[List[HistoryRecord]] = None,
        file_refs: Optional[List[str]] = None,
        task_type_names: Optional[List[str]] = None,
        code_util_names: Optional[List[str]] = None,
        capabilities: Optional[List[str]] = None,
        interaction_mode: str = "interactive",
    ) -> IntentDecision:
        """Return an IntentDecision for the current turn.

        In ``rpc`` interaction mode the gate is a no-op — every turn is
        forced to ``task`` so published MCP agents behave deterministically.
        """
        if interaction_mode == "rpc":
            return IntentDecision(
                mode="task",
                source="forced_rpc",
                rationale="interaction_mode=rpc forces task routing",
            )

        if not self._config.enabled:
            return IntentDecision(
                mode="task",
                source="heuristic",
                rationale="intent_gate disabled; defaulting to task",
            )

        task_type_names = task_type_names or []
        code_util_names = code_util_names or []
        file_refs = file_refs or []

        # ── Stage 1 — heuristic ────────────────────────────────────────────
        stage1 = self._heuristic(
            request=request,
            file_refs=file_refs,
            task_type_names=task_type_names,
            code_util_names=code_util_names,
        )
        if stage1 is not None:
            decision, confidence = stage1
            if confidence >= self._config.heuristic_confidence_threshold:
                return decision

        # ── Stage 2 — LLM classifier (ambiguous cases only) ────────────────
        try:
            decision = await asyncio.wait_for(
                self._llm_classify(
                    request=request,
                    history=history or [],
                    task_type_names=task_type_names,
                    code_util_names=code_util_names,
                    capabilities=capabilities or [],
                ),
                timeout=self._config.timeout_seconds,
            )
            return decision
        except asyncio.TimeoutError:
            logger.warning("IntentGate: LLM classifier timed out — defaulting to task")
        except Exception as e:  # pragma: no cover — defensive
            logger.warning("IntentGate: LLM classifier failed (%s) — defaulting to task", e)

        # Safe fallback: treat ambiguous as task. Over-working a chat turn
        # is annoying; under-working a task is worse.
        return IntentDecision(
            mode="task",
            source="heuristic_fallback",
            rationale="classifier unavailable; defaulted to task",
        )

    # ---- stage 1 -----------------------------------------------------------

    def _heuristic(
        self,
        request: str,
        file_refs: List[str],
        task_type_names: List[str],
        code_util_names: List[str],
    ) -> Optional[tuple]:
        """Return ``(decision, confidence)`` or ``None`` if undecidable."""
        text = (request or "").strip()
        if not text:
            # Empty input — treat as chat; framework will surface an error if needed.
            return (
                IntentDecision(
                    mode="chat", source="heuristic",
                    rationale="empty request",
                ),
                0.95,
            )

        if file_refs:
            return (
                IntentDecision(
                    mode="task", source="heuristic",
                    rationale="file attachment implies task",
                ),
                0.9,
            )

        toks = _tokens(text)
        if not toks:
            return (
                IntentDecision(
                    mode="chat", source="heuristic",
                    rationale="no word tokens (punctuation only)",
                ),
                0.9,
            )

        # Very short + greeting-only token → chat.
        if len(toks) <= 3 and all(t in _GREETING_TOKENS for t in toks):
            return (
                IntentDecision(
                    mode="chat", source="heuristic",
                    rationale="greeting/ack lexicon",
                ),
                0.95,
            )

        # Explicit match to an existing task-type or persisted code utility.
        known_names = {n.lower() for n in task_type_names} | {
            n.lower() for n in code_util_names
        }
        if known_names and any(t in known_names for t in toks):
            return (
                IntentDecision(
                    mode="task", source="heuristic",
                    rationale="references known task type / code utility",
                ),
                0.85,
            )

        # Task verb within the first three tokens.
        if any(t in _TASK_VERBS for t in toks[:3]):
            return (
                IntentDecision(
                    mode="task", source="heuristic",
                    rationale="leading task verb",
                ),
                0.8,
            )

        # Single token, no verb, no greeting → probably chat/noise.
        if len(toks) == 1:
            return (
                IntentDecision(
                    mode="chat", source="heuristic",
                    rationale="single non-task token",
                ),
                0.75,
            )

        return None  # hand off to LLM classifier

    # ---- stage 2 -----------------------------------------------------------

    async def _llm_classify(
        self,
        request: str,
        history: List[HistoryRecord],
        task_type_names: List[str],
        code_util_names: List[str],
        capabilities: List[str],
    ) -> IntentDecision:
        history_snippet = ""
        if history:
            last = history[-3:]
            history_snippet = "\n".join(
                f"- Prior request: {h.original_request[:180]} | "
                f"Response summary: {h.response_summary[:180]}"
                for h in last
            )

        known_tasks = ", ".join(task_type_names) if task_type_names else "(none)"
        known_scripts = ", ".join(code_util_names) if code_util_names else "(none)"
        known_caps = ", ".join(capabilities) if capabilities else "(none)"

        system = (
            "You classify a single user turn for an agent that can either "
            "chat or execute tasks. Return STRICT JSON only (no prose, no "
            "markdown) matching this schema:\n"
            '{"mode":"chat|task|hybrid","needs_clarify":bool,'
            '"clarify_q":string|null,"scout_hint":[string,...],'
            '"rationale":string}\n\n'
            "Guidance:\n"
            "- Prefer 'chat' for greetings, acknowledgements, small talk, and "
            "questions about the agent itself (what can you do, who are you).\n"
            "- Prefer 'task' when the user wants something done — search, "
            "fetch, summarise, create, analyse, etc.\n"
            "- Use 'hybrid' when the turn mixes chat with a real task "
            "(\"hi, can you also search for X?\").\n"
            "- Set needs_clarify=true ONLY when the turn is so ambiguous that "
            "neither chat nor task can proceed at all. This is a last resort. "
            "A short vague turn should default to chat; a vague instruction "
            "should default to task with a best-guess scout_hint.\n"
            "- scout_hint: list up to 3 capability names from the known "
            "capabilities that the task would likely use. Empty list for chat.\n"
            "- rationale: one short sentence."
        )

        user = (
            f"Known task types: {known_tasks}\n"
            f"Known scripts: {known_scripts}\n"
            f"Known capabilities: {known_caps}\n"
            f"Recent history:\n{history_snippet or '(none)'}\n\n"
            f"Current turn:\n{request}"
        )

        resp = await self._llm.complete(
            messages=[{"role": "user", "content": user}],
            system=system,
            provider_name=self._config.llm_provider or "default",
            max_tokens=300,
        )
        return self._parse_llm_output(resp.content)

    @staticmethod
    def _parse_llm_output(raw: str) -> IntentDecision:
        """Extract a JSON object from the model's response.

        Tolerant of surrounding markdown fences; falls back to task on any
        parse error so an ill-formed classifier response never strands a user.
        """
        text = (raw or "").strip()
        # Strip ```json fences if present.
        fence = re.match(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if fence:
            text = fence.group(1)
        # Extract the first top-level JSON object.
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        if brace:
            text = brace.group(0)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("IntentGate: could not parse classifier JSON: %r", raw[:200])
            return IntentDecision(
                mode="task", source="heuristic_fallback",
                rationale="classifier returned invalid JSON",
            )

        mode = str(data.get("mode", "task")).lower()
        if mode not in ("chat", "task", "hybrid"):
            mode = "task"
        needs_clarify = bool(data.get("needs_clarify", False))
        clarify_q = data.get("clarify_q") or None
        if not needs_clarify:
            clarify_q = None
        scout_hint = data.get("scout_hint") or []
        if not isinstance(scout_hint, list):
            scout_hint = []
        scout_hint = [str(h) for h in scout_hint][:3]
        rationale = str(data.get("rationale", ""))[:300]

        return IntentDecision(
            mode=mode,
            needs_clarify=needs_clarify,
            clarify_q=clarify_q if needs_clarify else None,
            scout_hint=scout_hint,
            source="llm",
            rationale=rationale,
        )
