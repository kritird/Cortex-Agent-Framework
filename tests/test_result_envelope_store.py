"""Tests for ResultEnvelopeStore."""
import asyncio
import pytest
import tempfile
from cortex.modules.result_envelope_store import ResultEnvelope, ResultEnvelopeStore


@pytest.mark.asyncio
async def test_write_and_read():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = ResultEnvelopeStore(base_path=tmpdir)
        envelope = ResultEnvelope(
            task_id="sess_01/000_test",
            session_id="sess_01",
            status="complete",
            output_value="Hello world",
            content_summary="Hello",
        )
        await store.write_envelope(envelope)
        loaded = await store.read_envelope("sess_01", "sess_01/000_test")
        assert loaded is not None
        assert loaded.status == "complete"
        assert loaded.output_value == "Hello world"


@pytest.mark.asyncio
async def test_read_all_session_envelopes():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = ResultEnvelopeStore(base_path=tmpdir)
        for i in range(3):
            env = ResultEnvelope(
                task_id=f"sess_02/{i:03d}_task",
                session_id="sess_02",
                status="complete",
            )
            await store.write_envelope(env)
        envelopes = await store.read_all_session_envelopes("sess_02")
        assert len(envelopes) == 3


@pytest.mark.asyncio
async def test_cleanup_session():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = ResultEnvelopeStore(base_path=tmpdir)
        env = ResultEnvelope(task_id="sess_03/000_task", session_id="sess_03", status="complete")
        await store.write_envelope(env)
        store.cleanup_session("sess_03")
        loaded = await store.read_envelope("sess_03", "sess_03/000_task")
        assert loaded is None
