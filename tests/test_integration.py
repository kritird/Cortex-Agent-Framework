"""Integration tests — requires ANTHROPIC_API_KEY set."""
import asyncio
import json
import os
import pytest
import tempfile
import yaml
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cortex.exceptions import CortexSecurityError


SKIP_INTEGRATION = not os.environ.get("ANTHROPIC_API_KEY")
SKIP_GROK_INTEGRATION = not os.environ.get("XAI_API_KEY")


# ── Resume security tests (no API key required) ─────────────────────────────


@pytest.mark.asyncio
async def test_resume_session_rejects_different_user():
    """_resume_session must raise CortexSecurityError when user_id doesn't match snapshot."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = {
            "agent": {"name": "TestAgent", "description": "A test agent"},
            "llm_access": {
                "default": {
                    "provider": "anthropic",
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 256,
                }
            },
            "storage": {"base_path": tmpdir},
        }
        config_path = str(Path(tmpdir) / "cortex.yaml")
        with open(config_path, "w") as f:
            yaml.dump(config, f)

        from cortex.framework import CortexFramework

        # Patch LLMClient to avoid needing a real API key
        with patch("cortex.framework.LLMClient") as MockLLM:
            mock_client = AsyncMock()
            mock_client.verify_all = AsyncMock(return_value={"default": True})
            MockLLM.return_value = mock_client

            framework = CortexFramework(config_path)
            await framework.initialize()

            # Save a snapshot as user_a
            session_id = "sess_security_test"
            await framework._session_manager.save_graph_snapshot(
                session_id=session_id,
                user_id="user_a",
                original_request="original request",
                snapshot={"tasks": {}, "edges": {}},
            )

            # Attempt to resume as user_b — should raise CortexSecurityError
            q = asyncio.Queue()
            with pytest.raises(CortexSecurityError, match="belongs to a different user"):
                await framework._resume_session(
                    resume_session_id=session_id,
                    user_id="user_b",
                    event_queue=q,
                    user_task_types=None,
                    user_consent="none",
                    start_time=0.0,
                )

            await framework.shutdown()


@pytest.mark.asyncio
async def test_resume_session_allows_same_user():
    """_resume_session should not raise CortexSecurityError when user_id matches."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = {
            "agent": {"name": "TestAgent", "description": "A test agent"},
            "llm_access": {
                "default": {
                    "provider": "anthropic",
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 256,
                }
            },
            "storage": {"base_path": tmpdir},
        }
        config_path = str(Path(tmpdir) / "cortex.yaml")
        with open(config_path, "w") as f:
            yaml.dump(config, f)

        from cortex.framework import CortexFramework

        with patch("cortex.framework.LLMClient") as MockLLM:
            mock_client = AsyncMock()
            mock_client.verify_all = AsyncMock(return_value={"default": True})
            MockLLM.return_value = mock_client

            framework = CortexFramework(config_path)
            await framework.initialize()

            session_id = "sess_same_user"
            await framework._session_manager.save_graph_snapshot(
                session_id=session_id,
                user_id="user_a",
                original_request="original request",
                snapshot={"tasks": {}, "edges": {}},
            )

            # Resume as user_a — should pass the security check.
            # It will fail later (no real graph to restore), but must NOT raise
            # CortexSecurityError.
            q = asyncio.Queue()
            try:
                await framework._resume_session(
                    resume_session_id=session_id,
                    user_id="user_a",
                    event_queue=q,
                    user_task_types=None,
                    user_consent="none",
                    start_time=0.0,
                )
            except CortexSecurityError:
                pytest.fail("CortexSecurityError raised for same user — security check too strict")
            except Exception:
                # Other errors (e.g., graph restore failure) are expected
                pass

            await framework.shutdown()


# ── Full integration tests (require ANTHROPIC_API_KEY) ───────────────────────


@pytest.mark.skipif(SKIP_INTEGRATION, reason="ANTHROPIC_API_KEY not set")
@pytest.mark.asyncio
async def test_full_session_no_task_types():
    """Test a session with no task types (direct synthesis fallback)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = {
            "agent": {"name": "TestAgent", "description": "A test agent"},
            "llm_access": {
                "default": {
                    "provider": "anthropic",
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 256,
                }
            },
            "storage": {"base_path": tmpdir},
        }
        config_path = str(Path(tmpdir) / "cortex.yaml")
        with open(config_path, "w") as f:
            yaml.dump(config, f)

        from cortex.framework import CortexFramework
        framework = CortexFramework(config_path)
        await framework.initialize()

        q = asyncio.Queue()
        result = await framework.run_session(
            user_id="test_user",
            request="Say hello in one sentence",
            event_queue=q,
        )
        await framework.shutdown()

        assert result.response is not None
        assert len(result.response) > 0


# ── Grok integration tests (require XAI_API_KEY) ──────────────────────────────


@pytest.mark.skipif(SKIP_GROK_INTEGRATION, reason="XAI_API_KEY not set")
@pytest.mark.asyncio
async def test_grok_full_session_no_task_types():
    """Test a full session using the Grok provider with no task types."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = {
            "agent": {"name": "GrokTestAgent", "description": "A test agent using Grok"},
            "llm_access": {
                "default": {
                    "provider": "grok",
                    "model": "grok-3-mini-fast",
                    "max_tokens": 256,
                }
            },
            "storage": {"base_path": tmpdir},
        }
        config_path = str(Path(tmpdir) / "cortex.yaml")
        with open(config_path, "w") as f:
            yaml.dump(config, f)

        from cortex.framework import CortexFramework
        framework = CortexFramework(config_path)
        await framework.initialize()

        q = asyncio.Queue()
        result = await framework.run_session(
            user_id="test_user",
            request="Say hello in one sentence",
            event_queue=q,
        )
        await framework.shutdown()

        assert result.response is not None
        assert len(result.response) > 0


@pytest.mark.skipif(SKIP_GROK_INTEGRATION, reason="XAI_API_KEY not set")
@pytest.mark.asyncio
async def test_grok_provider_streaming():
    """Test that Grok provider streams tokens correctly."""
    from cortex.llm.providers.grok_provider import GrokProvider
    from cortex.config.schema import LLMProviderConfig

    config = LLMProviderConfig(provider="grok", model="grok-3-mini-fast", max_tokens=50)
    provider = GrokProvider(config)

    tokens = []
    async for token in provider.stream(
        [{"role": "user", "content": "Say hi in one word"}],
        system="You are helpful.",
        max_tokens=20,
    ):
        tokens.append(token)

    full_response = "".join(tokens)
    assert len(full_response) > 0


@pytest.mark.skipif(SKIP_GROK_INTEGRATION, reason="XAI_API_KEY not set")
@pytest.mark.asyncio
async def test_grok_provider_complete():
    """Test that Grok provider non-streaming completion works."""
    from cortex.llm.providers.grok_provider import GrokProvider
    from cortex.config.schema import LLMProviderConfig

    config = LLMProviderConfig(provider="grok", model="grok-3-mini-fast", max_tokens=50)
    provider = GrokProvider(config)

    result = await provider.complete(
        [{"role": "user", "content": "Say hi in one word"}],
        system="You are helpful.",
        max_tokens=20,
    )

    assert result.content is not None
    assert len(result.content) > 0
    assert result.provider == "grok"
    assert result.usage.total_tokens > 0
