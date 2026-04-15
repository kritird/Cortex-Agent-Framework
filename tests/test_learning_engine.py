"""Tests for LearningEngine."""
import asyncio
import pytest
import tempfile
import yaml
from cortex.config.schema import LearningConfig
from cortex.modules.history_store import TaskCompletion
from cortex.modules.learning_engine import LearningEngine, DeltaProposal, SessionConfirmation
from cortex.modules.validation_agent import ValidationReport


@pytest.mark.asyncio
async def test_evaluate_session_no_consent():
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = LearningEngine(delta_path=tmpdir, config=LearningConfig(consent_enabled=True))
        report = ValidationReport(composite_score=0.9, passed=True)
        result = await engine.evaluate_session(
            session_id="sess_01",
            user_id="user_1",
            user_consent="none",
            validation_report=report,
            task_completion=TaskCompletion(total_tasks=2, completed_tasks=2),
        )
        assert result is None


@pytest.mark.asyncio
async def test_evaluate_session_failed_validation():
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = LearningEngine(delta_path=tmpdir, config=LearningConfig(consent_enabled=True))
        report = ValidationReport(composite_score=0.5, passed=False)
        result = await engine.evaluate_session(
            session_id="sess_02",
            user_id="user_1",
            user_consent="positive",
            validation_report=report,
            task_completion=TaskCompletion(total_tasks=2, completed_tasks=2),
        )
        assert result is None


@pytest.mark.asyncio
async def test_stage_delta_distinct_users():
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = LearningEngine(delta_path=tmpdir, config=LearningConfig())
        proposal = DeltaProposal(
            task_name="test_task",
            description="A test task",
            learned_from_sessions=[
                SessionConfirmation("sess_1", "user_a", True, 0.9)
            ],
            confirmations=1,
        )
        await engine.stage_delta(proposal, tmpdir)
        # Stage again with same user — should NOT increment
        proposal2 = DeltaProposal(
            task_name="test_task",
            description="A test task",
            learned_from_sessions=[
                SessionConfirmation("sess_2", "user_a", True, 0.85)  # same user
            ],
            confirmations=1,
        )
        await engine.stage_delta(proposal2, tmpdir)
        with open(f"{tmpdir}/pending.yaml") as f:
            pending = yaml.safe_load(f)
        task = pending["task_types"][0]
        assert task["confirmations"] == 1  # Only 1 distinct user


@pytest.mark.asyncio
async def test_confidence_levels():
    proposal = DeltaProposal(task_name="x", description="x")
    proposal.confirmations = 1
    assert proposal.compute_confidence() == "low"
    proposal.confirmations = 3
    assert proposal.compute_confidence() == "medium"
    proposal.confirmations = 5
    assert proposal.compute_confidence() == "high"
