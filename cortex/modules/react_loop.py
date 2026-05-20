"""ReactLoop — drives the reason -> act -> observe cycle for one sub-agent task.

A ``GenericMCPAgent`` builds a ``ReactLoop`` per non-scripted task. The loop
owns the reasoning conversation; the agent owns action execution and passes in
an ``execute_action`` callback. Each iteration:

  1. Ask the task's LLM for the next step (a single JSON object).
  2. If the model returns ``action: "finish"``, stop and keep ``final_answer``.
  3. Otherwise run the named action and feed its observation back in — together
     with the model's own stated *expectation* for that step, so the next
     reasoning turn always sees both what happened and what was meant to.

The loop terminates as soon as the sub-agent decides the task is done. A
``max_iterations`` safety cap forces a best-effort final answer if it does not.
"""
import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional

from cortex.llm.context import TokenUsage
from cortex.prompts import (
    REACT_COMPACTION_HEADER,
    REACT_FORCE_FINAL,
    REACT_OBSERVATION,
    REACT_UNKNOWN_ACTION,
)

logger = logging.getLogger(__name__)


@dataclass
class ActionResult:
    """Outcome of executing one ReAct action.

    ``observation`` is the text fed back into the loop. The remaining fields
    are aggregated across the run and surfaced on the final ``ResultEnvelope``.
    """
    observation: str
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    produced_files: List[str] = field(default_factory=list)
    generated_script: Optional[str] = None
    forged_server_path: Optional[str] = None
    tool_trace: List[str] = field(default_factory=list)


@dataclass
class ReactResult:
    """Final outcome of a full ReAct loop run."""
    final_answer: str
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    produced_files: List[str] = field(default_factory=list)
    generated_script: Optional[str] = None
    forged_server_path: Optional[str] = None
    tool_trace: List[str] = field(default_factory=list)
    steps: int = 0


def _parse_json_object(raw: str) -> Optional[dict]:
    """Extract the first balanced JSON object from an LLM response.

    Tolerates markdown fences and trailing/leading prose. Respects string
    literals so braces inside a value do not confuse brace matching. Returns
    ``None`` if no object can be parsed.
    """
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("```")
        ).strip()

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except (ValueError, TypeError):
        pass

    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                    return obj if isinstance(obj, dict) else None
                except (ValueError, TypeError):
                    return None
    return None


def _truncate(text: str, max_tokens: int) -> str:
    """Bound an observation to roughly ``max_tokens`` (~4 chars/token)."""
    if not text:
        return "(empty result)"
    max_chars = max(400, max_tokens * 4)
    if len(text) <= max_chars:
        return text
    return (
        text[:max_chars].rstrip()
        + f"\n... [observation truncated — {len(text) - max_chars} more characters]"
    )


def _oneline(text: str, limit: int) -> str:
    """Collapse whitespace and clip to ``limit`` characters for digests."""
    s = " ".join((text or "").split())
    return s if len(s) <= limit else s[:limit].rstrip() + "…"


class ReactLoop:
    """Reason -> act -> observe driver for a single sub-agent task."""

    def __init__(
        self,
        *,
        llm_client,
        provider_name: str,
        system_prompt: str,
        max_iterations: int,
        observation_max_tokens: int,
        context_char_budget: int,
        valid_actions: List[str],
        execute_action: Callable[[str, str], Awaitable[ActionResult]],
        emit_status: Optional[Callable[[str, dict], Awaitable[None]]] = None,
    ):
        self._llm_client = llm_client
        self._provider_name = provider_name or "default"
        self._system_prompt = system_prompt
        self._max_iterations = max(1, int(max_iterations))
        self._observation_max_tokens = observation_max_tokens
        self._context_char_budget = context_char_budget
        # Executable action names (without "finish", which the loop handles).
        self._valid_actions = list(valid_actions)
        self._execute_action = execute_action
        self._emit_status = emit_status

    async def run(self, initial_user_msg: str) -> ReactResult:
        """Drive the loop until the sub-agent finishes or the cap is hit."""
        conversation: List[Dict[str, str]] = [
            {"role": "user", "content": initial_user_msg}
        ]
        agg = ReactResult(final_answer="")

        for step in range(1, self._max_iterations + 1):
            await self._emit(
                f"Reasoning (step {step}/{self._max_iterations})",
                {"react_step": step},
            )
            resp = await self._reason(conversation, agg)
            if resp is None:
                agg.steps = step
                return agg
            raw = resp

            parsed = _parse_json_object(raw)
            if parsed is None:
                # The model answered in plain prose instead of a JSON step —
                # accept it as the final answer rather than failing the task.
                logger.info(
                    "ReAct step %d: no JSON object in response — treating it "
                    "as the final answer",
                    step,
                )
                agg.final_answer = raw.strip()
                agg.steps = step
                return agg

            action = str(parsed.get("action") or "").strip()

            if action == "finish":
                final = str(parsed.get("final_answer") or "").strip()
                if not final:
                    final = str(parsed.get("thought") or "").strip() or raw.strip()
                agg.final_answer = final
                agg.steps = step
                await self._emit(
                    f"Task complete after {step} step(s)", {"react_step": step}
                )
                return agg

            if action not in self._valid_actions:
                conversation.append({"role": "assistant", "content": raw})
                conversation.append({
                    "role": "user",
                    "content": REACT_UNKNOWN_ACTION.format(
                        step=step,
                        action=action or "(missing)",
                        valid_actions=", ".join(self._valid_actions + ["finish"]),
                    ),
                })
                continue

            action_input = str(parsed.get("action_input") or "").strip()
            expectation = (
                str(parsed.get("expectation") or "").strip()
                or "(no expectation stated)"
            )
            await self._emit(
                f"Step {step}: {action}",
                {"react_step": step, "action": action},
            )

            try:
                result = await self._execute_action(action, action_input)
            except Exception as exc:  # never let one action kill the task
                logger.warning(
                    "ReAct action '%s' raised at step %d: %s", action, step, exc
                )
                result = ActionResult(
                    observation=f"[action '{action}' failed: {exc}]",
                    tool_trace=[f"{action}:error"],
                )

            self._accumulate(agg, result)
            observation = _truncate(result.observation, self._observation_max_tokens)
            conversation.append({"role": "assistant", "content": raw})
            conversation.append({
                "role": "user",
                "content": REACT_OBSERVATION.format(
                    step=step,
                    action=action,
                    expectation=expectation,
                    observation=observation,
                ),
            })
            self._compact(conversation)

        # Safety cap reached without a "finish" — force a best-effort answer.
        return await self._force_final(conversation, agg)

    # ── internals ────────────────────────────────────────────────────────────

    async def _reason(
        self, conversation: List[Dict[str, str]], agg: ReactResult
    ) -> Optional[str]:
        """One reasoning call. Returns the raw response, or None on failure."""
        prompt_chars = sum(len(m["content"]) for m in conversation)
        try:
            resp = await self._llm_client.complete(
                messages=conversation,
                system=self._system_prompt,
                provider_name=self._provider_name,
            )
        except Exception as exc:
            logger.warning("ReAct reasoning call failed: %s", exc)
            if not agg.final_answer:
                agg.final_answer = f"[ReAct loop aborted: reasoning call failed — {exc}]"
            return None
        agg.token_usage += self._usage_from(resp, prompt_chars)
        return resp.content or ""

    async def _force_final(
        self, conversation: List[Dict[str, str]], agg: ReactResult
    ) -> ReactResult:
        """Cap hit: ask once more for a final answer, then return."""
        await self._emit(
            f"Step limit reached ({self._max_iterations}) — forcing final answer",
            {"react_step": self._max_iterations},
        )
        # The last message is always a user turn (observation or error), so
        # merging the directive into it preserves user/assistant alternation.
        conversation[-1]["content"] += "\n\n" + REACT_FORCE_FINAL.format(
            max_iterations=self._max_iterations
        )
        raw = await self._reason(conversation, agg)
        agg.steps = self._max_iterations
        if raw is None:
            agg.final_answer = agg.final_answer or (
                "[ReAct loop hit the step limit and could not synthesise a "
                "final answer]"
            )
            return agg
        parsed = _parse_json_object(raw)
        final = str(parsed.get("final_answer") or "").strip() if parsed else ""
        agg.final_answer = final or raw.strip()
        return agg

    @staticmethod
    def _accumulate(agg: ReactResult, result: ActionResult) -> None:
        """Fold one action's side effects into the running result."""
        agg.token_usage += result.token_usage
        for f in result.produced_files:
            if f not in agg.produced_files:
                agg.produced_files.append(f)
        if result.generated_script:
            agg.generated_script = result.generated_script
        if result.forged_server_path:
            agg.forged_server_path = result.forged_server_path
        agg.tool_trace.extend(result.tool_trace)

    @staticmethod
    def _usage_from(resp, prompt_chars: int) -> TokenUsage:
        """Real provider usage when available, else a ~4 chars/token estimate."""
        usage = getattr(resp, "usage", None)
        if usage is not None and getattr(usage, "total_tokens", 0):
            return TokenUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
            )
        inp = prompt_chars // 4
        out = len(getattr(resp, "content", "") or "") // 4
        return TokenUsage(input_tokens=inp, output_tokens=out, total_tokens=inp + out)

    def _compact(self, conversation: List[Dict[str, str]]) -> None:
        """Digest the oldest steps once the running context exceeds budget.

        The conversation is ``[initial_user] + N*(assistant, user)``. The head
        and the last three step-pairs are kept verbatim; everything between is
        collapsed into a compact bullet digest appended to the head, so strict
        user/assistant alternation is preserved.
        """
        total = sum(len(m["content"]) for m in conversation)
        keep_tail = 6
        if total <= self._context_char_budget or len(conversation) <= 1 + keep_tail:
            return

        head = conversation[0]
        tail = conversation[-keep_tail:]
        middle = conversation[1:-keep_tail]

        digest = [REACT_COMPACTION_HEADER]
        for i in range(0, len(middle) - 1, 2):
            assistant_msg = middle[i].get("content", "")
            user_msg = middle[i + 1].get("content", "")
            parsed = _parse_json_object(assistant_msg)
            if parsed:
                act = parsed.get("action", "?")
                thought = _oneline(str(parsed.get("thought", "")), 200)
                digest.append(f"- did **{act}** — {thought}")
            else:
                digest.append(f"- {_oneline(assistant_msg, 200)}")
            digest.append(f"  observed: {_oneline(user_msg, 220)}")

        conversation[:] = [
            {"role": "user", "content": head["content"] + "\n".join(digest)}
        ] + tail

    async def _emit(self, message: str, metadata: dict) -> None:
        """Best-effort status event — never let streaming break the loop."""
        if self._emit_status is None:
            return
        try:
            await self._emit_status(message, metadata)
        except Exception:
            pass
