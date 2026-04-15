"""SSE event generator with reconnection buffer."""
import asyncio
import time
from collections import deque
from typing import AsyncIterator, Deque, Optional

from cortex.streaming.status_events import StatusEvent, ClarificationEvent, ResultEvent


class SSEBuffer:
    """
    Reconnection buffer: stores last N events so clients can replay on reconnect.
    Keyed by event_id. Thread-safe via asyncio.
    """

    def __init__(self, max_size: int = 50):
        self._buffer: Deque[tuple[int, str]] = deque(maxlen=max_size)
        self._event_id: int = 0
        self._lock = asyncio.Lock()

    async def add(self, sse_data: str) -> int:
        async with self._lock:
            self._event_id += 1
            self._buffer.append((self._event_id, sse_data))
            return self._event_id

    async def replay_from(self, last_event_id: int) -> list[str]:
        """Return all events after last_event_id for reconnection replay."""
        async with self._lock:
            return [
                f"id: {eid}\n{data}"
                for eid, data in self._buffer
                if eid > last_event_id
            ]

    async def get_last_id(self) -> int:
        async with self._lock:
            return self._event_id


class SSEGenerator:
    """
    Generates Server-Sent Events from an asyncio.Queue.
    Handles keepalives and reconnection buffering.
    """

    def __init__(
        self,
        event_queue: asyncio.Queue,
        buffer: Optional[SSEBuffer] = None,
        keepalive_interval: float = 15.0,
        min_delivery_interval_ms: int = 200,
    ):
        self._queue = event_queue
        self._buffer = buffer or SSEBuffer()
        self._keepalive_interval = keepalive_interval
        self._min_interval = min_delivery_interval_ms / 1000.0
        self._last_delivery = 0.0

    async def generate(self, last_event_id: int = 0) -> AsyncIterator[str]:
        """
        Async generator yielding SSE-formatted strings.
        Replays buffered events for reconnecting clients.
        Emits keepalives when idle.
        """
        # Replay missed events on reconnect
        if last_event_id > 0:
            replayed = await self._buffer.replay_from(last_event_id)
            for event in replayed:
                yield event

        while True:
            try:
                # Wait for next event with keepalive timeout
                event = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=self._keepalive_interval,
                )

                # Respect minimum delivery interval
                now = time.monotonic()
                wait = self._min_interval - (now - self._last_delivery)
                if wait > 0:
                    await asyncio.sleep(wait)

                # Serialize event
                if isinstance(event, (StatusEvent, ClarificationEvent, ResultEvent)):
                    sse_data = event.to_sse()
                elif isinstance(event, str):
                    sse_data = f"data: {event}\n\n"
                elif event is None:
                    # Sentinel: end of stream
                    yield "event: done\ndata: {}\n\n"
                    return
                else:
                    import json
                    sse_data = f"data: {json.dumps(event)}\n\n"

                # Buffer and yield
                event_id = await self._buffer.add(sse_data)
                self._last_delivery = time.monotonic()
                yield f"id: {event_id}\n{sse_data}"

            except asyncio.TimeoutError:
                # Keepalive comment
                yield ": keepalive\n\n"
