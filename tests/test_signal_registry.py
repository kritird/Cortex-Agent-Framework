"""Tests for SignalRegistry."""
import asyncio
import pytest
from cortex.modules.signal_registry import SignalRegistry


@pytest.mark.asyncio
async def test_register_and_fire():
    registry = SignalRegistry()
    await registry.start()
    event = registry.register_task("sess_01", "task_01")
    assert not event.is_set()
    registry.fire_signal("sess_01", "task_01")
    assert event.is_set()
    await registry.stop()


@pytest.mark.asyncio
async def test_await_signal_success():
    registry = SignalRegistry()
    await registry.start()
    registry.register_task("sess_02", "task_02")
    async def fire_later():
        await asyncio.sleep(0.05)
        registry.fire_signal("sess_02", "task_02")
    asyncio.create_task(fire_later())
    result = await registry.await_signal("sess_02", "task_02", timeout_seconds=2.0)
    assert result is True
    await registry.stop()


@pytest.mark.asyncio
async def test_await_signal_timeout():
    registry = SignalRegistry()
    await registry.start()
    registry.register_task("sess_03", "task_03")
    result = await registry.await_signal("sess_03", "task_03", timeout_seconds=0.1)
    assert result is False
    await registry.stop()


@pytest.mark.asyncio
async def test_cleanup_session():
    registry = SignalRegistry()
    await registry.start()
    registry.register_task("sess_04", "task_04")
    registry.cleanup_session("sess_04")
    assert ("sess_04", "task_04") not in registry._events
    await registry.stop()
