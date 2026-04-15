"""OpenAI provider — GPT models via the OpenAI API."""
from cortex.config.schema import LLMProviderConfig
from cortex.llm.providers.openai_base import OpenAICompatibleBase


class OpenAIProvider(OpenAICompatibleBase):
    """First-class OpenAI provider. Set OPENAI_API_KEY to use."""

    PROVIDER_NAME = "openai"
    DEFAULT_BASE_URL = "https://api.openai.com/v1"
    DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"
    DEFAULT_MODEL = "gpt-4o"

    def __init__(self, config: LLMProviderConfig):
        super().__init__(config)
