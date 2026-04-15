"""In-process memory storage backend (default)."""
import asyncio
import fnmatch
import json
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

from cortex.storage.base import StorageBackend


class MemoryBackend(StorageBackend):
    """
    In-process dict-based storage. Zero latency, no persistence.
    Thread-safe via asyncio lock. Supports TTL via expiry timestamps.
    """

    def __init__(self):
        self._store: Dict[str, Any] = {}
        self._expiry: Dict[str, float] = {}
        self._hashes: Dict[str, Dict[str, Any]] = {}
        self._pubsub: Dict[str, List[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    def _is_expired(self, key: str) -> bool:
        exp = self._expiry.get(key)
        if exp is None:
            return False
        return time.monotonic() > exp

    def _cleanup_expired(self, key: str) -> None:
        if self._is_expired(key):
            self._store.pop(key, None)
            self._expiry.pop(key, None)

    async def connect(self) -> None:
        pass  # No-op for in-memory

    async def disconnect(self) -> None:
        pass  # No-op for in-memory

    async def ping(self) -> bool:
        return True

    async def set(self, key: str, value: Any, ttl_seconds: Optional[int] = None) -> None:
        async with self._lock:
            self._store[key] = value
            if ttl_seconds is not None:
                self._expiry[key] = time.monotonic() + ttl_seconds
            else:
                self._expiry.pop(key, None)

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            self._cleanup_expired(key)
            return self._store.get(key)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)
            self._expiry.pop(key, None)

    async def keys(self, pattern: str) -> List[str]:
        async with self._lock:
            # Clean up expired keys first
            expired = [k for k in list(self._store.keys()) if self._is_expired(k)]
            for k in expired:
                self._store.pop(k, None)
                self._expiry.pop(k, None)
            return [k for k in self._store.keys() if fnmatch.fnmatch(k, pattern)]

    async def exists(self, key: str) -> bool:
        async with self._lock:
            self._cleanup_expired(key)
            return key in self._store

    async def hset(self, name: str, key: str, value: Any) -> None:
        async with self._lock:
            if name not in self._hashes:
                self._hashes[name] = {}
            self._hashes[name][key] = value

    async def hget(self, name: str, key: str) -> Optional[Any]:
        async with self._lock:
            return self._hashes.get(name, {}).get(key)

    async def hgetall(self, name: str) -> Dict[str, Any]:
        async with self._lock:
            return dict(self._hashes.get(name, {}))

    async def hdel(self, name: str, key: str) -> None:
        async with self._lock:
            if name in self._hashes:
                self._hashes[name].pop(key, None)

    async def publish(self, channel: str, message: str) -> None:
        async with self._lock:
            queues = self._pubsub.get(channel, [])
            for q in queues:
                await q.put(message)

    async def subscribe(self, channel: str) -> AsyncGenerator[str, None]:
        q: asyncio.Queue = asyncio.Queue()
        async with self._lock:
            if channel not in self._pubsub:
                self._pubsub[channel] = []
            self._pubsub[channel].append(q)
        try:
            while True:
                msg = await q.get()
                yield msg
        finally:
            async with self._lock:
                if channel in self._pubsub and q in self._pubsub[channel]:
                    self._pubsub[channel].remove(q)

    async def clear_session(self, session_id: str) -> None:
        """Remove all keys for a session."""
        async with self._lock:
            to_delete = [k for k in self._store if session_id in k]
            for k in to_delete:
                del self._store[k]
                self._expiry.pop(k, None)
            # Also clean hashes
            to_delete_h = [k for k in self._hashes if session_id in k]
            for k in to_delete_h:
                del self._hashes[k]
