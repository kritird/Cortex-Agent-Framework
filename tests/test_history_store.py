"""Tests for HistoryStore."""
import asyncio
import pytest
import tempfile
from datetime import datetime, timezone
from cortex.modules.history_store import (
    HistoryStore, HistoryRecord, TaskCompletion, TokenUsageByRole
)


def make_record(session_id="sess_test01", user_id="user_1") -> HistoryRecord:
    return HistoryRecord(
        session_id=session_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        user_id=user_id,
        original_request="Test request",
        response_summary="Test response",
        task_completion=TaskCompletion(total_tasks=2, completed_tasks=2),
        validation_score=0.85,
        validation_passed=True,
        user_consent="positive",
        token_usage=TokenUsageByRole(total_tokens=100),
        persisted_files=[],
        duration_seconds=3.5,
    )


@pytest.mark.asyncio
async def test_write_and_read():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = HistoryStore(base_path=tmpdir)
        record = make_record()
        await store.write_session(record)
        loaded = await store.read_session_detail("user_1", "sess_test01")
        assert loaded is not None
        assert loaded.session_id == "sess_test01"
        assert loaded.validation_score == 0.85


@pytest.mark.asyncio
async def test_search_history():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = HistoryStore(base_path=tmpdir)
        await store.write_session(make_record("sess_a", "user_2"))
        await store.write_session(make_record("sess_b", "user_2"))
        results = await store.search_history("user_2", "Test request")
        assert len(results) == 2


@pytest.mark.asyncio
async def test_delete_user_history():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = HistoryStore(base_path=tmpdir)
        await store.write_session(make_record("sess_x", "user_3"))
        await store.delete_user_history("user_3")
        result = await store.read_session_detail("user_3", "sess_x")
        assert result is None


@pytest.mark.asyncio
async def test_pagination():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = HistoryStore(base_path=tmpdir)
        for i in range(5):
            await store.write_session(make_record(f"sess_{i:02d}", "user_4"))
        page = await store.read_user_history("user_4", page=1, page_size=3)
        assert len(page.records) == 3
        assert page.total_records == 5
        assert page.total_pages == 2
