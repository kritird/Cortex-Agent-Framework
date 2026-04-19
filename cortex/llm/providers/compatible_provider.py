"""Anthropic-compatible provider for gateways and proxies."""
import os
from typing import AsyncIterator, Dict, List, Optional

import anthropic

from cortex.config.schema import LLMProviderConfig
from cortex.exceptions import CortexLLMError
from cortex.llm.context import LLMResponse, TokenUsage


class CompatibleProvider:
    """
    Uses Anthropic SDK pointed at a compatible base_url.
    Useful for LiteLLM, OpenRouter, or internal AI gateways.
    """

    def __init__(self, config: LLMProviderConfig):
        self._config = config
        api_key = None
        if config.api_key_env_var:
            api_key = os.environ.get(config.api_key_env_var)
        if not api_key:
            api_key = os.environ.get("ANTHROPIC_API_KEY", "dummy-key")
        if not config.base_url:
            raise CortexLLMError(
                "anthropic_compatible provider requires base_url to be set.",
                provider="anthropic_compatible",
            )
        kwargs = {
            "api_key": api_key,
            "base_url": config.base_url,
        }
        if config.headers:
            kwargs["default_headers"] = config.headers
        self._client = anthropic.AsyncAnthropic(**kwargs)

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
        except anthropic.APIStatusError as e:
            raise CortexLLMError(
                f"Compatible provider error: {e.message}",
                provider="anthropic_compatible",
                status_code=e.status_code,
            )

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
        return LLMResponse(content=content, model=response.model, usage=usage, provider="anthropic_compatible")

    async def verify(self) -> bool:
        try:
            await self._client.messages.create(
                model=self._config.model,
                max_tokens=200,
                messages=[{"role": "user", "content": "Hi"}],
            )
            return True
        except Exception:
            return False
