"""LLMClient — routes calls to configured provider."""
from typing import AsyncIterator, Dict, List, Optional

from cortex.config.schema import LLMAccessConfig, LLMProviderConfig
from cortex.exceptions import CortexProviderError
from cortex.llm.context import LLMResponse, TokenUsage


def _build_provider(config: LLMProviderConfig):
    """Instantiate the appropriate provider from config."""
    provider_type = config.provider
    if provider_type == "anthropic":
        from cortex.llm.providers.anthropic_provider import AnthropicProvider
        return AnthropicProvider(config)
    elif provider_type == "anthropic_compatible":
        from cortex.llm.providers.compatible_provider import CompatibleProvider
        return CompatibleProvider(config)
    elif provider_type == "bedrock":
        from cortex.llm.providers.bedrock_provider import BedrockProvider
        return BedrockProvider(config)
    elif provider_type == "azure_ai":
        from cortex.llm.providers.azure_provider import AzureProvider
        return AzureProvider(config)
    elif provider_type == "openai":
        from cortex.llm.providers.openai_provider import OpenAIProvider
        return OpenAIProvider(config)
    elif provider_type == "local":
        from cortex.llm.providers.local_provider import LocalProvider
        return LocalProvider(config)
    elif provider_type == "gemini":
        from cortex.llm.providers.gemini_provider import GeminiProvider
        return GeminiProvider(config)
    elif provider_type == "grok":
        from cortex.llm.providers.grok_provider import GrokProvider
        return GrokProvider(config)
    elif provider_type == "mistral":
        from cortex.llm.providers.mistral_provider import MistralProvider
        return MistralProvider(config)
    elif provider_type == "deepseek":
        from cortex.llm.providers.deepseek_provider import DeepSeekProvider
        return DeepSeekProvider(config)
    elif provider_type == "custom":
        from cortex.llm.providers.custom_provider import CustomProvider
        return CustomProvider(config)
    else:
        raise CortexProviderError(
            f"Unknown LLM provider type: '{provider_type}'. "
            f"Valid types: anthropic, anthropic_compatible, bedrock, azure_ai, "
            f"openai, local, gemini, grok, mistral, deepseek, custom",
            provider_name=provider_type,
        )


class LLMClient:
    """
    Routes all LLM calls to the configured provider.
    Primary agent and ValidationAgent: always use default provider.
    GenericMCPAgent tasks: use task's llm_provider or default.
    All stream() calls return AsyncIterator[str].
    """

    def __init__(self, config: LLMAccessConfig):
        self._config = config
        self._providers: Dict[str, object] = {}
        # Eagerly build default
        self._providers["default"] = _build_provider(config.default)
        # Build named providers
        for name, prov_config in config.providers.items():
            self._providers[name] = _build_provider(prov_config)

    def _get_provider(self, provider_name: str = "default"):
        if provider_name not in self._providers:
            if provider_name in (self._config.providers or {}):
                self._providers[provider_name] = _build_provider(
                    self._config.providers[provider_name]
                )
            else:
                raise CortexProviderError(
                    f"LLM provider '{provider_name}' is not configured in llm_access.",
                    provider_name=provider_name,
                )
        return self._providers[provider_name]

    async def stream(
        self,
        messages: List[Dict],
        system: str,
        provider_name: str = "default",
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        """Route streaming call to configured provider."""
        provider = self._get_provider(provider_name)
        async for token in provider.stream(messages, system, max_tokens):
            yield token

    async def complete(
        self,
        messages: List[Dict],
        system: str,
        provider_name: str = "default",
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """Non-streaming completion."""
        provider = self._get_provider(provider_name)
        return await provider.complete(messages, system, max_tokens)

    async def verify_provider(self, provider_name: str = "default") -> bool:
        """Test call at startup — returns True if provider responds correctly."""
        try:
            provider = self._get_provider(provider_name)
            return await provider.verify()
        except Exception:
            return False

    async def verify_all(self) -> Dict[str, bool]:
        """Verify all configured providers. Returns {name: ok} dict."""
        results = {}
        for name in self._providers:
            results[name] = await self.verify_provider(name)
        return results
