"""SQLite storage backend with WAL mode."""
import asyncio
import fnmatch
import json
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

import aiosqlite

from cortex.storage.base import StorageBackend


class SQLiteBackend(StorageBackend):
    """
    SQLite-backed storage with WAL mode for crash resilience.
    Stores JSON-serialized values. Supports TTL via expiry column.
    """

    def __init__(self, db_path: str, wal_mode: bool = True, timeout: float = 5.0):
        self._db_path = db_path
        self._wal_mode = wal_mode
        self._timeout = timeout
        self._db: Optional[aiosqlite.Connection] = None
        self._pubsub_queues: Dict[str, List[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self._db_path, timeout=self._timeout)
        if self._wal_mode:
            await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                expires_at REAL
            )
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS hash_store (
                name TEXT NOT NULL,
                field TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (name, field)
            )
        """)
        await self._db.commit()

    async def disconnect(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def ping(self) -> bool:
        try:
            await self._db.execute("SELECT 1")
            return True
        except Exception:
            return False

    async def _cleanup_expired_key(self, key: str) -> None:
        now = time.time()
        await self._db.execute(
            "DELETE FROM kv_store WHERE key = ? AND expires_at IS NOT NULL AND expires_at < ?",
            (key, now)
        )

    async def set(self, key: str, value: Any, ttl_seconds: Optional[int] = None) -> None:
        expires_at = time.time() + ttl_seconds if ttl_seconds is not None else None
        serialized = json.dumps(value)
        async with self._lock:
            await self._db.execute(
                "INSERT OR REPLACE INTO kv_store (key, value, expires_at) VALUES (?, ?, ?)",
                (key, serialized, expires_at)
            )
            await self._db.commit()

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            now = time.time()
            # Delete expired
            await self._db.execute(
                "DELETE FROM kv_store WHERE key = ? AND expires_at IS NOT NULL AND expires_at < ?",
                (key, now)
            )
            cursor = await self._db.execute(
                "SELECT value FROM kv_store WHERE key = ?", (key,)
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return json.loads(row[0])

    async def delete(self, key: str) -> None:
        async with self._lock:
            await self._db.execute("DELETE FROM kv_store WHERE key = ?", (key,))
            await self._db.commit()

    async def keys(self, pattern: str) -> List[str]:
        now = time.time()
        async with self._lock:
            await self._db.execute(
                "DELETE FROM kv_store WHERE expires_at IS NOT NULL AND expires_at < ?", (now,)
            )
            cursor = await self._db.execute("SELECT key FROM kv_store")
            rows = await cursor.fetchall()
            all_keys = [row[0] for row in rows]
            return [k for k in all_keys if fnmatch.fnmatch(k, pattern)]

    async def exists(self, key: str) -> bool:
        return await self.get(key) is not None

    async def hset(self, name: str, key: str, value: Any) -> None:
        serialized = json.dumps(value)
        async with self._lock:
            await self._db.execute(
                "INSERT OR REPLACE INTO hash_store (name, field, value) VALUES (?, ?, ?)",
                (name, key, serialized)
            )
            await self._db.commit()

    async def hget(self, name: str, key: str) -> Optional[Any]:
        async with self._lock:
            cursor = await self._db.execute(
                "SELECT value FROM hash_store WHERE name = ? AND field = ?", (name, key)
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return json.loads(row[0])

    async def hgetall(self, name: str) -> Dict[str, Any]:
        async with self._lock:
            cursor = await self._db.execute(
                "SELECT field, value FROM hash_store WHERE name = ?", (name,)
            )
            rows = await cursor.fetchall()
            return {row[0]: json.loads(row[1]) for row in rows}

    async def hdel(self, name: str, key: str) -> None:
        async with self._lock:
            await self._db.execute(
                "DELETE FROM hash_store WHERE name = ? AND field = ?", (name, key)
            )
            await self._db.commit()

    async def publish(self, channel: str, message: str) -> None:
        queues = self._pubsub_queues.get(channel, [])
        for q in queues:
            await q.put(message)

    async def subscribe(self, channel: str) -> AsyncGenerator[str, None]:
        q: asyncio.Queue = asyncio.Queue()
        if channel not in self._pubsub_queues:
            self._pubsub_queues[channel] = []
        self._pubsub_queues[channel].append(q)
        try:
            while True:
                msg = await asyncio.wait_for(q.get(), timeout=5.0)
                yield msg
        except asyncio.TimeoutError:
            pass
        finally:
            if channel in self._pubsub_queues and q in self._pubsub_queues[channel]:
                self._pubsub_queues[channel].remove(q)
