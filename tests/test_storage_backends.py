"""Tests for storage backends."""
import asyncio
import pytest
import tempfile
from cortex.storage.memory_backend import MemoryBackend
from cortex.storage.sqlite_backend import SQLiteBackend


@pytest.mark.asyncio
async def test_memory_backend_basic():
    backend = MemoryBackend()
    await backend.connect()
    await backend.set("key1", "value1")
    assert await backend.get("key1") == "value1"
    await backend.delete("key1")
    assert await backend.get("key1") is None


@pytest.mark.asyncio
async def test_memory_backend_ttl():
    import time
    backend = MemoryBackend()
    await backend.connect()
    await backend.set("key_ttl", "val", ttl_seconds=1)
    assert await backend.get("key_ttl") == "val"


@pytest.mark.asyncio
async def test_memory_backend_hash():
    backend = MemoryBackend()
    await backend.connect()
    await backend.hset("myhash", "field1", "hello")
    assert await backend.hget("myhash", "field1") == "hello"
    all_fields = await backend.hgetall("myhash")
    assert all_fields == {"field1": "hello"}


@pytest.mark.asyncio
async def test_memory_backend_keys_pattern():
    backend = MemoryBackend()
    await backend.connect()
    await backend.set("sess_abc:task1", "v1")
    await backend.set("sess_abc:task2", "v2")
    await backend.set("other:key", "v3")
    keys = await backend.keys("sess_abc:*")
    assert len(keys) == 2


@pytest.mark.asyncio
async def test_sqlite_backend_basic():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    backend = SQLiteBackend(db_path=db_path)
    await backend.connect()
    await backend.set("key1", {"data": 42})
    val = await backend.get("key1")
    assert val == {"data": 42}
    await backend.delete("key1")
    assert await backend.get("key1") is None
    await backend.disconnect()


@pytest.mark.asyncio
async def test_sqlite_backend_hash():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    backend = SQLiteBackend(db_path=db_path)
    await backend.connect()
    await backend.hset("h1", "f1", "hello")
    assert await backend.hget("h1", "f1") == "hello"
    await backend.hdel("h1", "f1")
    assert await backend.hget("h1", "f1") is None
    await backend.disconnect()
