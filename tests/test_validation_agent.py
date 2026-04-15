"""Tests for ValidationAgent."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from cortex.config.schema import ValidationConfig
from cortex.exceptions import CortexConfigError
from cortex.modules.validation_agent import ValidationAgent, ValidationReport, _parse_validation_response


def test_parse_validation_response_scores():
    text = """
INTENT_MATCH_SCORE: 0.9
COMPLETENESS_SCORE: 0.8
COHERENCE_SCORE: 0.7
FINDINGS:
- dimension: completeness | issue: Missing details | suggestion: Add more detail
RECOMMENDATION: Good response overall
"""
    intent, completeness, coherence, findings, rec = _parse_validation_response(text)
    assert intent == 0.9
    assert completeness == 0.8
    assert coherence == 0.7
    assert len(findings) == 1
    assert findings[0].dimension == "completeness"
    assert "Good" in rec


def test_threshold_floor_enforcement():
    from cortex.llm.client import LLMClient
    mock_llm = MagicMock(spec=LLMClient)
    config = ValidationConfig(threshold=0.5)  # Below floor
    with pytest.raises(CortexConfigError):
        ValidationAgent(mock_llm, config)


def test_threshold_at_floor_ok():
    from cortex.llm.client import LLMClient
    mock_llm = MagicMock(spec=LLMClient)
    config = ValidationConfig(threshold=0.60)
    agent = ValidationAgent(mock_llm, config)  # Should not raise
    assert agent is not None
