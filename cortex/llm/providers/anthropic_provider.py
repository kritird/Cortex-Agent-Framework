"""Direct Anthropic API provider."""
import os
from typing import AsyncIterator, Dict, List, Optional

import anthropic

from cortex.config.schema import LLMProviderConfig
from cortex.exceptions import CortexLLMError
from cortex.llm.context import LLMResponse, TokenUsage


class AnthropicProvider:
    """Calls the Anthropic API directly using the anthropic SDK."""

    def __init__(self, config: LLMProviderConfig):
        self._config = config
        api_key = None
        if config.api_key_env_var:
            api_key = os.environ.get(config.api_key_env_var)
        if not api_key:
            api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise CortexLLMError(
                "Anthropic API key not found. Set ANTHROPIC_API_KEY or configure api_key_env_var.",
                provider="anthropic",
            )
        kwargs = {"api_key": api_key}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        if config.headers:
            kwargs["default_headers"] = config.headers
        self._client = anthropic.AsyncAnthropic(**kwargs)

    async def stream(
        self,
        messages: List[Dict],
        system: str,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        """Stream tokens from Anthropic API."""
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
                f"Anthropic API error: {e.message}",
                provider="anthropic",
                status_code=e.status_code,
            )
        except anthropic.APIConnectionError as e:
            raise CortexLLMError(f"Anthropic connection error: {e}", provider="anthropic")

    async def complete(
        self,
        messages: List[Dict],
        system: str,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """Non-streaming completion."""
        max_tok = max_tokens or self._config.max_tokens
        try:
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
            return LLMResponse(
                content=content,
                model=response.model,
                usage=usage,
                stop_reason=response.stop_reason or "end_turn",
                provider="anthropic",
            )
        except anthropic.APIStatusError as e:
            raise CortexLLMError(
                f"Anthropic API error: {e.message}",
                provider="anthropic",
                status_code=e.status_code,
            )

    async def verify(self) -> bool:
        """Test call to verify provider is working."""
        try:
            response = await self._client.messages.create(
                model=self._config.model,
                max_tokens=5,
                messages=[{"role": "user", "content": "Hi"}],
            )
            return bool(response.content)
        except Exception:
            return False
