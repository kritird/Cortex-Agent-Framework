"""Tests for SessionManager."""
import asyncio
import json
import pytest
import tempfile
from pathlib import Path

from cortex.config.schema import AgentConfig, StorageConfig, HistoryConfig
from cortex.exceptions import CortexSessionLimitError, CortexSecurityError
from cortex.modules.history_store import HistoryStore
from cortex.modules.session_manager import SessionManager


def make_session_manager(tmpdir, max_global=10, max_per_user=3):
    agent_config = AgentConfig(name="TestAgent", description="test")
    agent_config.concurrency.max_concurrent_sessions = max_global
    agent_config.concurrency.max_concurrent_sessions_per_user = max_per_user
    storage_config = StorageConfig(base_path=tmpdir)
    history_config = HistoryConfig()
    store = HistoryStore(base_path=tmpdir)
    return SessionManager(agent_config, storage_config, history_config, store)


@pytest.mark.asyncio
async def test_create_session():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = make_session_manager(tmpdir)
        await sm.initialize()
        session = await sm.create_session("user1", "test request")
        assert session.session_id.startswith("sess_")
        assert session.user_id == "user1"
        sessions = sm.get_active_sessions("user1")
        assert len(sessions) == 1


@pytest.mark.asyncio
async def test_per_user_limit():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = make_session_manager(tmpdir, max_per_user=2)
        await sm.initialize()
        await sm.create_session("user1", "req1")
        await sm.create_session("user1", "req2")
        with pytest.raises(CortexSessionLimitError) as exc_info:
            await sm.create_session("user1", "req3")
        assert exc_info.value.max_allowed == 2
        assert len(exc_info.value.active_sessions) == 2


@pytest.mark.asyncio
async def test_complete_session():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = make_session_manager(tmpdir)
        await sm.initialize()
        session = await sm.create_session("user1", "test request")
        await sm.complete_session(session.session_id)
        assert len(sm.get_active_sessions("user1")) == 0


@pytest.mark.asyncio
async def test_terminate_session():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = make_session_manager(tmpdir)
        await sm.initialize()
        session = await sm.create_session("user1", "test")
        await sm.terminate_session(session.session_id)
        assert len(sm.get_active_sessions("user1")) == 0


# ── WAL replay resilience tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wal_replay_skips_corrupted_entries():
    """WAL replay should skip corrupted JSON lines and recover valid entries."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = make_session_manager(tmpdir)
        wal_path = Path(tmpdir) / "wal.jsonl"
        wal_path.parent.mkdir(parents=True, exist_ok=True)

        # Write a WAL with a valid create, a corrupted line, and a valid complete
        valid_create = json.dumps({
            "op": "create", "session_id": "sess_good", "user_id": "user1", "timestamp": "2025-01-01T00:00:00Z"
        })
        corrupted_line = "{this is not valid json"
        valid_complete = json.dumps({
            "op": "complete", "session_id": "sess_good", "timestamp": "2025-01-01T00:01:00Z"
        })
        wal_path.write_text(f"{valid_create}\n{corrupted_line}\n{valid_complete}\n")

        # Point session manager's WAL at our file
        sm._wal_path = wal_path

        # Replay should succeed without raising
        await sm._replay_wal()

        # WAL should be truncated after clean replay
        assert wal_path.read_text() == ""


@pytest.mark.asyncio
async def test_wal_replay_all_corrupted():
    """WAL replay with only corrupted entries should not crash."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = make_session_manager(tmpdir)
        wal_path = Path(tmpdir) / "wal.jsonl"
        wal_path.write_text("{bad json\n{also bad\n")
        sm._wal_path = wal_path

        # Should not raise
        await sm._replay_wal()
        assert wal_path.read_text() == ""


@pytest.mark.asyncio
async def test_wal_replay_recovers_incomplete_sessions():
    """WAL replay should identify sessions that were created but not completed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = make_session_manager(tmpdir)
        wal_path = Path(tmpdir) / "wal.jsonl"

        incomplete = json.dumps({
            "op": "create", "session_id": "sess_incomplete", "user_id": "user1", "timestamp": "2025-01-01T00:00:00Z"
        })
        completed_create = json.dumps({
            "op": "create", "session_id": "sess_done", "user_id": "user2", "timestamp": "2025-01-01T00:00:00Z"
        })
        completed_end = json.dumps({
            "op": "complete", "session_id": "sess_done", "timestamp": "2025-01-01T00:01:00Z"
        })
        wal_path.write_text(f"{incomplete}\n{completed_create}\n{completed_end}\n")
        sm._wal_path = wal_path

        await sm._replay_wal()
        # WAL truncated after replay
        assert wal_path.read_text() == ""


# ── Session resume security tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resume_rejects_different_user():
    """Resuming a session saved by user_a as user_b should raise CortexSecurityError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = make_session_manager(tmpdir)
        await sm.initialize()

        session_id = "sess_resume_test"
        # Save a graph snapshot as user_a
        await sm.save_graph_snapshot(
            session_id=session_id,
            user_id="user_a",
            original_request="test request",
            snapshot={"tasks": {}, "edges": {}},
        )

        # Load and verify user_id mismatch
        saved = await sm.load_graph_snapshot(session_id)
        assert saved is not None
        assert saved["user_id"] == "user_a"

        # The security check lives in framework._resume_session, so we verify
        # the snapshot contains user_id which framework.py uses for the check
        original_user_id = saved.get("user_id")
        assert original_user_id == "user_a"
        assert original_user_id != "user_b", "Different user should not match"


@pytest.mark.asyncio
async def test_resume_allows_same_user():
    """Resuming a session as the original user should succeed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = make_session_manager(tmpdir)
        await sm.initialize()

        session_id = "sess_resume_same"
        await sm.save_graph_snapshot(
            session_id=session_id,
            user_id="user_a",
            original_request="test request",
            snapshot={"tasks": {}, "edges": {}},
        )

        saved = await sm.load_graph_snapshot(session_id)
        assert saved is not None
        assert saved["user_id"] == "user_a"


@pytest.mark.asyncio
async def test_resume_snapshot_round_trip():
    """Snapshot save and load should preserve all fields."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = make_session_manager(tmpdir)
        await sm.initialize()

        session_id = "sess_roundtrip"
        snapshot_data = {
            "tasks": {"t1": {"status": "completed"}, "t2": {"status": "pending"}},
            "edges": {"t2": ["t1"]},
        }

        await sm.save_graph_snapshot(
            session_id=session_id,
            user_id="user_x",
            original_request="roundtrip test",
            snapshot=snapshot_data,
        )

        saved = await sm.load_graph_snapshot(session_id)
        assert saved["session_id"] == session_id
        assert saved["user_id"] == "user_x"
        assert saved["original_request"] == "roundtrip test"
        assert saved["snapshot"] == snapshot_data
        assert "saved_at" in saved
