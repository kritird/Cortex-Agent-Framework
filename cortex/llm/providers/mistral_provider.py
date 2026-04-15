"""Mistral AI provider — via the OpenAI-compatible Mistral API."""
from cortex.config.schema import LLMProviderConfig
from cortex.llm.providers.openai_base import OpenAICompatibleBase


class MistralProvider(OpenAICompatibleBase):
    """First-class Mistral provider. Set MISTRAL_API_KEY to use."""

    PROVIDER_NAME = "mistral"
    DEFAULT_BASE_URL = "https://api.mistral.ai/v1"
    DEFAULT_API_KEY_ENV = "MISTRAL_API_KEY"
    DEFAULT_MODEL = "mistral-large-latest"

    def __init__(self, config: LLMProviderConfig):
        super().__init__(config)
