"""Adaptive LLM concurrency gate — AIMD self-tuning.

A resizable async concurrency limiter. Starts conservative, climbs additively
on sustained clean calls *under saturation* (real demand for more capacity),
and halves multiplicatively on a bad signal (timeout, connection error, empty
response, or a sharp latency spike vs the best observed baseline).

On a single-stream backend (one local Ollama), the gate converges down to ~1.
On a parallel-capable backend (cloud API, vLLM batching, multi-GPU), it climbs
toward the configured ceiling. No probing or configuration tuning required —
the gate learns from real LLM-call outcomes.

Used by cortex.llm.client.LLMClient; the queue-wait credit mechanism in
cortex.modules.generic_mcp_agent is unchanged — both layers cooperate to keep
tasks from false-timing-out while the gate self-tunes.
"""
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class AdaptiveLLMGate:
    """Async resizable concurrency limiter with AIMD self-tuning.

    State is mutated only through acquire() / release() / record_outcome();
    the current size is exposed read-only via the limit property for
    observability.
    """

    def __init__(
        self,
        initial: int = 2,
        min_limit: int = 1,
        max_limit: int = 2,
        increase_after: int = 8,
        cooldown_calls: int = 4,
        latency_spike_factor: float = 2.5,
    ) -> None:
        self._min = max(1, int(min_limit))
        self._max = max(self._min, int(max_limit))
        self._limit = max(self._min, min(int(initial), self._max))
        self._in_flight = 0
        self._waiters = 0
        self._cond = asyncio.Condition()
        # Adaptation knobs / state
        self._increase_after = max(1, int(increase_after))
        self._cooldown_calls = max(0, int(cooldown_calls))
        self._latency_spike_factor = float(latency_spike_factor)
        self._clean_streak = 0
        self._calls_since_decrease = self._cooldown_calls  # start ready
        self._best_latency: Optional[float] = None

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def acquire(self) -> None:
        """Wait until a slot is free, then take it."""
        async with self._cond:
            self._waiters += 1
            try:
                while self._in_flight >= self._limit:
                    await self._cond.wait()
            finally:
                self._waiters -= 1
            self._in_flight += 1

    async def release(self) -> None:
        """Free a slot and wake one waiter, if any."""
        async with self._cond:
            self._in_flight = max(0, self._in_flight - 1)
            self._cond.notify(1)

    async def record_outcome(
        self,
        *,
        ok: bool,
        empty: bool,
        latency: float,
    ) -> None:
        """Feed AIMD logic after every LLM HTTP call.

        Bad signals (not ok, empty response, or a sharp latency spike vs the
        best observed) trigger multiplicative decrease. Sustained clean calls
        while saturated and past cooldown trigger additive increase.
        """
        async with self._cond:
            self._calls_since_decrease += 1
            bad = (not ok) or empty
            reason = "empty/error" if bad else None

            # Latency-gradient soft signal — catches backend-internal
            # contention (e.g. Ollama serializing) before it becomes errors.
            if ok and not empty and latency > 0:
                if self._best_latency is None or latency < self._best_latency:
                    self._best_latency = latency
                elif latency > self._best_latency * self._latency_spike_factor:
                    bad = True
                    reason = (
                        f"latency spike ({latency:.1f}s vs best "
                        f"{self._best_latency:.1f}s)"
                    )

            if bad:
                new_limit = max(self._min, self._limit // 2)
                if new_limit != self._limit:
                    logger.info(
                        "AdaptiveLLMGate: %d -> %d (backoff: %s)",
                        self._limit, new_limit, reason,
                    )
                    self._limit = new_limit
                self._clean_streak = 0
                self._calls_since_decrease = 0
                return

            # Clean call. Only consider growing under real demand.
            self._clean_streak += 1
            saturated = (self._waiters > 0) or (self._in_flight >= self._limit)
            if (
                saturated
                and self._clean_streak >= self._increase_after
                and self._calls_since_decrease >= self._cooldown_calls
                and self._limit < self._max
            ):
                self._limit += 1
                logger.info(
                    "AdaptiveLLMGate: %d -> %d (probe-up: %d clean+saturated calls)",
                    self._limit - 1, self._limit, self._clean_streak,
                )
                self._clean_streak = 0
                self._cond.notify(1)

    async def __aenter__(self) -> "AdaptiveLLMGate":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.release()
