"""DeepSeek provider — via the OpenAI-compatible DeepSeek API."""
from cortex.config.schema import LLMProviderConfig
from cortex.llm.providers.openai_base import OpenAICompatibleBase


class DeepSeekProvider(OpenAICompatibleBase):
    """First-class DeepSeek provider. Set DEEPSEEK_API_KEY to use."""

    PROVIDER_NAME = "deepseek"
    DEFAULT_BASE_URL = "https://api.deepseek.com"
    DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"
    DEFAULT_MODEL = "deepseek-chat"

    def __init__(self, config: LLMProviderConfig):
        super().__init__(config)
