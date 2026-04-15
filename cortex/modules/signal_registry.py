"""SignalRegistry — manages asyncio.Event completion signals per (session_id, task_id)."""
import asyncio
import logging
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class SignalRegistry:
    """
    Manages asyncio.Event completion signals per (session_id, task_id).
    For Redis deployments: uses pub/sub for cross-instance signalling.
    For SQLite: uses polling at ~5ms intervals as fallback.
    All signals are co-located in-process for zero-latency fan-in.
    """

    def __init__(self, storage_backend=None):
        # {(session_id, task_id): asyncio.Event}
        self._events: Dict[Tuple[str, str], asyncio.Event] = {}
        # {(session_id, task_id): float} — timestamp of signal, unacknowledged
        self._pending_acks: Dict[Tuple[str, str], float] = {}
        self._storage = storage_backend
        self._lock = asyncio.Lock()
        self._re_fire_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start background re-fire loop."""
        self._re_fire_task = asyncio.create_task(self._re_fire_loop())

    async def stop(self) -> None:
        """Stop background re-fire loop."""
        if self._re_fire_task and not self._re_fire_task.done():
            self._re_fire_task.cancel()
            try:
                await self._re_fire_task
            except asyncio.CancelledError:
                pass

    def register_task(self, session_id: str, task_id: str) -> asyncio.Event:
        """Create and register an Event for this task. Return the Event."""
        key = (session_id, task_id)
        event = asyncio.Event()
        self._events[key] = event
        return event

    def fire_signal(self, session_id: str, task_id: str) -> None:
        """Set the Event for this task. Store signal in pending_acks."""
        key = (session_id, task_id)
        event = self._events.get(key)
        if event:
            event.set()
            self._pending_acks[key] = time.monotonic()
            logger.debug("Signal fired: session=%s task=%s", session_id, task_id)
        else:
            logger.warning("Signal fired for unregistered task: %s/%s", session_id, task_id)

    def acknowledge_signal(self, session_id: str, task_id: str) -> None:
        """PrimaryAgent calls this after receiving signal. Remove from pending_acks."""
        key = (session_id, task_id)
        self._pending_acks.pop(key, None)
        logger.debug("Signal acknowledged: session=%s task=%s", session_id, task_id)

    async def await_signal(
        self,
        session_id: str,
        task_id: str,
        timeout_seconds: float,
    ) -> bool:
        """
        Await the Event with timeout.
        Returns True if signalled, False if timeout.
        """
        key = (session_id, task_id)
        event = self._events.get(key)
        if event is None:
            event = self.register_task(session_id, task_id)

        # If already set (signal arrived before we started waiting)
        if event.is_set():
            self.acknowledge_signal(session_id, task_id)
            return True

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
            self.acknowledge_signal(session_id, task_id)
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "Signal timeout: session=%s task=%s after %.1fs",
                session_id, task_id, timeout_seconds
            )
            return False

    async def await_wave(
        self,
        session_id: str,
        task_ids: List[str],
        deadline: float,
    ) -> Dict[str, bool]:
        """
        Await all signals in a wave concurrently.
        Returns dict of {task_id: completed_bool}.
        deadline is an absolute monotonic timestamp.
        """
        now = time.monotonic()
        remaining = max(0.0, deadline - now)

        async def _await_one(task_id: str) -> Tuple[str, bool]:
            result = await self.await_signal(session_id, task_id, remaining)
            return task_id, result

        results_list = await asyncio.gather(
            *[_await_one(tid) for tid in task_ids],
            return_exceptions=True,
        )

        results: Dict[str, bool] = {}
        for item in results_list:
            if isinstance(item, Exception):
                logger.error("Wave signal error: %s", item)
            else:
                task_id, ok = item
                results[task_id] = ok

        # Any task_ids not in results: timed out
        for tid in task_ids:
            if tid not in results:
                results[tid] = False

        return results

    async def _re_fire_loop(self) -> None:
        """Background: every 500ms, re-fire any signals unacknowledged for >1s."""
        while True:
            try:
                await asyncio.sleep(0.5)
                now = time.monotonic()
                to_refire = [
                    (sid, tid)
                    for (sid, tid), fired_at in list(self._pending_acks.items())
                    if now - fired_at > 1.0
                ]
                for sid, tid in to_refire:
                    logger.debug("Re-firing unacknowledged signal: %s/%s", sid, tid)
                    self.fire_signal(sid, tid)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Re-fire loop error: %s", e)

    async def _redis_subscribe(self, session_id: str) -> None:
        """Redis pub/sub subscriber. Sets local Events when messages arrive."""
        if not self._storage:
            return
        try:
            channel = f"signals:{session_id}"
            async for message in self._storage.subscribe(channel):
                # message format: "task_id"
                task_id = message.strip()
                key = (session_id, task_id)
                event = self._events.get(key)
                if event:
                    event.set()
                    self._pending_acks[key] = time.monotonic()
        except Exception as e:
            logger.error("Redis subscribe error for session %s: %s", session_id, e)

    async def _sqlite_poll(self, session_id: str, task_id: str) -> None:
        """Poll SQLite at 5ms intervals as fallback for non-Redis deployments."""
        if not self._storage:
            return
        key_name = f"signal:{session_id}:{task_id}"
        while True:
            await asyncio.sleep(0.005)
            val = await self._storage.get(key_name)
            if val:
                self.fire_signal(session_id, task_id)
                return

    def cleanup_session(self, session_id: str) -> None:
        """Remove all events and pending_acks for this session."""
        keys_to_remove = [k for k in self._events if k[0] == session_id]
        for k in keys_to_remove:
            del self._events[k]
        ack_keys = [k for k in self._pending_acks if k[0] == session_id]
        for k in ack_keys:
            del self._pending_acks[k]
        logger.debug("SignalRegistry cleaned up session: %s", session_id)
