"""Local provider — OpenAI-compatible endpoint on the developer's machine.

Works with any server that exposes a `/chat/completions` endpoint such as
Ollama, LM Studio, llama.cpp's server, vLLM, text-generation-webui, or
LocalAI. Unlike the hosted `openai` provider, this one does not require an
API key to be configured — local servers typically ignore the
`Authorization` header, so a dummy value is sent when the developer has
not set one.
"""
import os

from cortex.config.schema import LLMProviderConfig
from cortex.llm.providers.openai_base import OpenAICompatibleBase


class LocalProvider(OpenAICompatibleBase):
    """Local OpenAI-compatible LLM (Ollama, LM Studio, llama.cpp, vLLM, ...).

    Config example:

        llm_access:
          default:
            provider: local
            model: llama3.1
            base_url: http://localhost:11434/v1

    An api_key_env_var is optional — some local proxies (e.g. LiteLLM) can
    be configured to require one; if set, it will be used.
    """

    PROVIDER_NAME = "local"
    DEFAULT_BASE_URL = "http://localhost:11434/v1"
    DEFAULT_API_KEY_ENV = "LOCAL_LLM_API_KEY"
    DEFAULT_MODEL = "llama3.1"

    def _resolve_api_key(self, config: LLMProviderConfig) -> str:
        api_key = None
        if config.api_key_env_var:
            api_key = os.environ.get(config.api_key_env_var)
        if not api_key:
            api_key = os.environ.get(self.DEFAULT_API_KEY_ENV)
        return api_key or "not-needed"
