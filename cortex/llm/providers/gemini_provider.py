"""Google Gemini provider — via the OpenAI-compatible Gemini API."""
from cortex.config.schema import LLMProviderConfig
from cortex.llm.providers.openai_base import OpenAICompatibleBase


class GeminiProvider(OpenAICompatibleBase):
    """First-class Gemini provider. Set GEMINI_API_KEY to use."""

    PROVIDER_NAME = "gemini"
    DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
    DEFAULT_API_KEY_ENV = "GEMINI_API_KEY"
    DEFAULT_MODEL = "gemini-2.5-flash"

    def __init__(self, config: LLMProviderConfig):
        super().__init__(config)
