"""xAI Grok provider — via the OpenAI-compatible xAI API."""
from cortex.config.schema import LLMProviderConfig
from cortex.llm.providers.openai_base import OpenAICompatibleBase


class GrokProvider(OpenAICompatibleBase):
    """First-class Grok provider. Set XAI_API_KEY to use."""

    PROVIDER_NAME = "grok"
    DEFAULT_BASE_URL = "https://api.x.ai/v1"
    DEFAULT_API_KEY_ENV = "XAI_API_KEY"
    DEFAULT_MODEL = "grok-3"

    def __init__(self, config: LLMProviderConfig):
        super().__init__(config)
