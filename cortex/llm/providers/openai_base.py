"""Base provider for OpenAI-compatible chat completion APIs."""
import json
import os
import ssl
from typing import AsyncIterator, Dict, List, Optional

import aiohttp

from cortex.config.schema import LLMProviderConfig
from cortex.exceptions import CortexLLMError
from cortex.llm.context import LLMResponse, TokenUsage


def _build_ssl_context() -> ssl.SSLContext:
    """Build an SSL context using certifi certificates as a fallback.

    On macOS with python.org installers, the default certificate store is
    often empty.  Using certifi guarantees a working CA bundle.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class OpenAICompatibleBase:
    """
    Base class for providers that implement the OpenAI chat completions API.

    Subclasses only need to set:
        PROVIDER_NAME  — identifier string (e.g. "openai", "gemini")
        DEFAULT_BASE_URL — API base URL
        DEFAULT_API_KEY_ENV — environment variable name for the API key
        DEFAULT_MODEL — fallback model name
    """

    PROVIDER_NAME: str = "openai_compatible"
    DEFAULT_BASE_URL: str = ""
    DEFAULT_API_KEY_ENV: str = ""
    DEFAULT_MODEL: str = ""

    def __init__(self, config: LLMProviderConfig):
        self._config = config
        self._api_key = self._resolve_api_key(config)
        self._base_url = (config.base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._model = config.model or self.DEFAULT_MODEL
        self._ssl_context = _build_ssl_context()
        self._headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            **(config.headers or {}),
        }

    def _resolve_api_key(self, config: LLMProviderConfig) -> str:
        api_key = None
        if config.api_key_env_var:
            api_key = os.environ.get(config.api_key_env_var)
        if not api_key:
            api_key = os.environ.get(self.DEFAULT_API_KEY_ENV)
        if not api_key:
            raise CortexLLMError(
                f"{self.PROVIDER_NAME} API key not found. "
                f"Set {self.DEFAULT_API_KEY_ENV} or configure api_key_env_var.",
                provider=self.PROVIDER_NAME,
            )
        return api_key

    def _build_body(
        self,
        messages: List[Dict],
        system: str,
        max_tokens: Optional[int],
        stream: bool,
    ) -> Dict:
        oai_messages = []
        if system:
            oai_messages.append({"role": "system", "content": system})
        oai_messages.extend(messages)
        body: Dict = {
            "model": self._model,
            "messages": oai_messages,
            "stream": stream,
        }
        max_tok = max_tokens or self._config.max_tokens
        if max_tok:
            body["max_tokens"] = max_tok
        return body

    async def stream(
        self,
        messages: List[Dict],
        system: str,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        body = self._build_body(messages, system, max_tokens, stream=True)
        url = f"{self._base_url}/chat/completions"
        connector = aiohttp.TCPConnector(ssl=self._ssl_context)
        try:
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    url, json=body, headers=self._headers, timeout=aiohttp.ClientTimeout(total=300)
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        raise CortexLLMError(
                            f"{self.PROVIDER_NAME} API error (HTTP {resp.status}): {text[:500]}",
                            provider=self.PROVIDER_NAME,
                            status_code=resp.status,
                        )
                    async for line in resp.content:
                        decoded = line.decode("utf-8").strip()
                        if not decoded or not decoded.startswith("data:"):
                            continue
                        data = decoded[len("data:"):].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content")
                            if content:
                                yield content
                        except (json.JSONDecodeError, IndexError, KeyError):
                            continue
        except aiohttp.ClientError as e:
            raise CortexLLMError(
                f"{self.PROVIDER_NAME} connection error: {e}",
                provider=self.PROVIDER_NAME,
            )

    async def complete(
        self,
        messages: List[Dict],
        system: str,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        body = self._build_body(messages, system, max_tokens, stream=False)
        url = f"{self._base_url}/chat/completions"
        connector = aiohttp.TCPConnector(ssl=self._ssl_context)
        try:
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    url, json=body, headers=self._headers, timeout=aiohttp.ClientTimeout(total=300)
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        raise CortexLLMError(
                            f"{self.PROVIDER_NAME} API error (HTTP {resp.status}): {text[:500]}",
                            provider=self.PROVIDER_NAME,
                            status_code=resp.status,
                        )
                    data = await resp.json()
        except aiohttp.ClientError as e:
            raise CortexLLMError(
                f"{self.PROVIDER_NAME} connection error: {e}",
                provider=self.PROVIDER_NAME,
            )

        choice = data.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content", "")
        usage_data = data.get("usage", {})
        usage = TokenUsage(
            input_tokens=usage_data.get("prompt_tokens", 0),
            output_tokens=usage_data.get("completion_tokens", 0),
            total_tokens=usage_data.get("total_tokens", 0),
        )
        return LLMResponse(
            content=content,
            model=data.get("model", self._model),
            usage=usage,
            stop_reason=choice.get("finish_reason", "stop"),
            provider=self.PROVIDER_NAME,
        )

    async def verify(self) -> bool:
        try:
            response = await self.complete(
                [{"role": "user", "content": "Hi"}],
                system="",
                max_tokens=200,
            )
            return bool(response.content)
        except Exception:
            return False
