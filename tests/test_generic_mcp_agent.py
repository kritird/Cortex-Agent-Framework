"""Tests for GenericMCPAgent."""
import pytest
import tempfile
from cortex.modules.generic_mcp_agent import _extract_content_summary


def test_extract_content_summary_short():
    content = "This is a short response."
    result = _extract_content_summary(content, max_tokens=100)
    assert result == content


def test_extract_content_summary_truncates():
    content = "Sentence one. Sentence two. Sentence three. " * 50
    result = _extract_content_summary(content, max_tokens=10)
    assert len(result) <= 10 * 4 + 10  # approx bound


def test_extract_content_summary_no_mid_sentence():
    content = "First sentence here. Second sentence with more content. Third one."
    result = _extract_content_summary(content, max_tokens=10)
    # Should end at a sentence boundary
    assert not result.endswith(" with") and not result.endswith(" more")
