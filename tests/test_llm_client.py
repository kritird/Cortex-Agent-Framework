"""Tests for LLMClient provider routing."""
import pytest
from unittest.mock import MagicMock, patch
from cortex.config.schema import LLMAccessConfig, LLMProviderConfig
from cortex.exceptions import CortexProviderError


def make_llm_config(provider="anthropic"):
    return LLMAccessConfig(
        default=LLMProviderConfig(provider=provider, model="claude-haiku-4-5-20251001")
    )


def test_unknown_provider_raises():
    from cortex.llm.client import _build_provider
    config = LLMProviderConfig(provider="unknown_provider", model="test")
    with pytest.raises(CortexProviderError):
        _build_provider(config)


def test_custom_provider_requires_function():
    from cortex.llm.client import _build_provider
    from cortex.exceptions import CortexLLMError
    config = LLMProviderConfig(provider="custom", model="test")
    with pytest.raises(CortexLLMError):
        _build_provider(config)
