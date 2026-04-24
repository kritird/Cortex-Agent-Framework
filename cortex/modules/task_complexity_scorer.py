"""TaskComplexityScorer — deterministic runtime complexity scoring.

Produces a single 0.0–1.0 float per session by combining observable signals
from executed envelopes plus the decomposed-task graph. Used by the
autonomic learning gate to decide whether an ad-hoc task is worth staging
as a new task type in ``cortex.yaml``.

The scorer is a pure, weighted sum — no LLM call, fully auditable, and
cheap enough to run on every completed session. Weights are fixed (not
user-tunable) to keep learning behaviour predictable across deployments.

Signals (see ``score()``):

    generated_script            0.35   code synthesis is the strongest
                                       indicator that a new capability was
                                       discovered this session.
    tool_trace_count            0.20   more tool calls → richer workflow
                                       worth capturing (cap 8).
    decomposed_task_count       0.15   multi-task sessions exercise the
                                       DAG (cap 5).
    has_dependencies            0.15   presence of any ``depends_on`` edge
                                       → non-trivial topology.
    total_tokens                0.10   LLM effort proxy (cap 10 000).
    duration_ms                 0.05   wall-clock proxy (cap 60 000 ms).

Each signal contributes at most its weight; the sum is clamped to
``[0.0, 1.0]``. Only envelopes with ``status == "complete"`` contribute.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional


# Weights — fixed to keep complexity scoring deterministic across deployments.
# Changing these is a breaking change to the learning gate.
_W_SCRIPT = 0.35
_W_TOOLS = 0.20
_W_TASKS = 0.15
_W_DEPS = 0.15
_W_TOKENS = 0.10
_W_DURATION = 0.05

# Normalisation caps: values at/above the cap saturate that signal to 1.0.
_CAP_TOOLS = 8
_CAP_TASKS = 5
_CAP_TOKENS = 10_000
_CAP_DURATION_MS = 60_000


@dataclass
class ComplexityBreakdown:
    """Per-signal breakdown of a complexity score, exposed for observability."""
    score: float
    has_generated_script: bool
    tool_trace_count: int
    decomposed_task_count: int
    has_dependencies: bool
    total_tokens: int
    duration_ms: int

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 3),
            "has_generated_script": self.has_generated_script,
            "tool_trace_count": self.tool_trace_count,
            "decomposed_task_count": self.decomposed_task_count,
            "has_dependencies": self.has_dependencies,
            "total_tokens": self.total_tokens,
            "duration_ms": self.duration_ms,
        }


class TaskComplexityScorer:
    """Stateless scorer that turns envelope + graph signals into a 0.0–1.0 float.

    The scorer only inspects *completed* envelopes. Failed or timed-out work
    shouldn't inflate the complexity of a session that never produced usable
    output.

    Typical call site (see ``CortexFramework.run_session``)::

        scorer = TaskComplexityScorer()
        breakdown = scorer.score(
            envelopes=adhoc_envelopes,
            decomposed_tasks=decomposed_tasks,
        )
        if breakdown.score >= cfg.learning.complexity_threshold:
            ...
    """

    def score(
        self,
        envelopes: Iterable,
        decomposed_tasks: Optional[Iterable] = None,
    ) -> ComplexityBreakdown:
        """Score a completed session.

        Args:
            envelopes: iterable of ``ResultEnvelope``; only ``status=="complete"``
                contribute to tool/script/token/duration counters.
            decomposed_tasks: iterable of ``DecomposedTask`` (or anything exposing
                ``task_name`` and ``depends_on``). Used for task-count and
                dependency signals. Falsy → those signals contribute 0.

        Returns:
            ``ComplexityBreakdown`` with the final ``score`` plus the per-signal
            counters used to compute it.
        """
        completed: List = [
            e for e in envelopes or [] if getattr(e, "status", None) == "complete"
        ]

        has_script = any(
            bool(getattr(e, "generated_script", None)) for e in completed
        )
        tool_trace_count = sum(
            len(getattr(e, "tool_trace", []) or []) for e in completed
        )
        total_tokens = 0
        for e in completed:
            usage = getattr(e, "token_usage", None)
            if usage is not None:
                total_tokens += int(getattr(usage, "total_tokens", 0) or 0)
        duration_ms = sum(
            int(getattr(e, "duration_ms", 0) or 0) for e in completed
        )

        tasks_list = list(decomposed_tasks or [])
        task_count = len(tasks_list)
        has_deps = any(
            bool(getattr(t, "depends_on", None)) for t in tasks_list
        )

        # Weighted contribution per signal.
        s_script = _W_SCRIPT if has_script else 0.0
        s_tools = _W_TOOLS * min(tool_trace_count, _CAP_TOOLS) / _CAP_TOOLS
        s_tasks = _W_TASKS * min(task_count, _CAP_TASKS) / _CAP_TASKS
        s_deps = _W_DEPS if has_deps else 0.0
        s_tokens = _W_TOKENS * min(total_tokens, _CAP_TOKENS) / _CAP_TOKENS
        s_duration = (
            _W_DURATION * min(duration_ms, _CAP_DURATION_MS) / _CAP_DURATION_MS
        )

        total = s_script + s_tools + s_tasks + s_deps + s_tokens + s_duration
        total = max(0.0, min(1.0, total))

        return ComplexityBreakdown(
            score=total,
            has_generated_script=has_script,
            tool_trace_count=tool_trace_count,
            decomposed_task_count=task_count,
            has_dependencies=has_deps,
            total_tokens=total_tokens,
            duration_ms=duration_ms,
        )
