"""Azure AI endpoints provider."""
import json
import os
from typing import AsyncIterator, Dict, List, Optional

from cortex.config.schema import LLMProviderConfig
from cortex.exceptions import CortexLLMError
from cortex.llm.context import LLMResponse, TokenUsage


class AzureProvider:
    """Calls Claude via Azure AI using the Anthropic SDK with Azure endpoint."""

    def __init__(self, config: LLMProviderConfig):
        self._config = config
        try:
            import anthropic
            self._anthropic = anthropic
        except ImportError:
            raise CortexLLMError("anthropic package required", provider="azure_ai")

        endpoint = None
        if config.endpoint_env_var:
            endpoint = os.environ.get(config.endpoint_env_var)
        if not endpoint:
            endpoint = os.environ.get("AZURE_AI_ENDPOINT")
        if not endpoint:
            raise CortexLLMError("Azure AI endpoint not configured. Set endpoint_env_var.", provider="azure_ai")

        api_key = None
        if config.api_key_env_var:
            api_key = os.environ.get(config.api_key_env_var)
        if not api_key:
            api_key = os.environ.get("AZURE_AI_API_KEY", "")

        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=endpoint,
            default_headers={
                "api-version": config.api_version or "2024-05-01-preview",
                **(config.headers or {}),
            },
        )

    async def stream(
        self,
        messages: List[Dict],
        system: str,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        max_tok = max_tokens or self._config.max_tokens
        try:
            async with self._client.messages.stream(
                model=self._config.model,
                max_tokens=max_tok,
                system=system,
                messages=messages,
            ) as stream:
                async for text in stream.text_stream:
                    yield text
        except Exception as e:
            raise CortexLLMError(f"Azure AI streaming error: {e}", provider="azure_ai")

    async def complete(
        self,
        messages: List[Dict],
        system: str,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        max_tok = max_tokens or self._config.max_tokens
        response = await self._client.messages.create(
            model=self._config.model,
            max_tokens=max_tok,
            system=system,
            messages=messages,
        )
        content = response.content[0].text if response.content else ""
        usage = TokenUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            total_tokens=response.usage.input_tokens + response.usage.output_tokens,
        )
        return LLMResponse(content=content, model=response.model, usage=usage, provider="azure_ai")

    async def verify(self) -> bool:
        try:
            await self.complete([{"role": "user", "content": "Hi"}], system="", max_tokens=5)
            return True
        except Exception:
            return False
