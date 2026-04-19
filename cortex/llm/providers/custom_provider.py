"""Custom provider — delegates to user-defined functions in llm_providers.py."""
import importlib
from typing import AsyncIterator, Dict, List, Optional

from cortex.config.schema import LLMProviderConfig
from cortex.exceptions import CortexLLMError
from cortex.llm.context import LLMResponse


class CustomProvider:
    """
    Delegates LLM calls to user-defined async functions.
    The function dotted path is specified in config.function.

    Expected function signature:
        async def my_llm(messages, system, config) -> AsyncIterator[str]  (for stream)
        async def my_llm_complete(messages, system, config) -> LLMResponse  (for complete)
    """

    def __init__(self, config: LLMProviderConfig):
        self._config = config
        if not config.function:
            raise CortexLLMError("Custom provider requires 'function' to be set.", provider="custom")
        self._stream_fn = self._load_function(config.function)
        # Try to load a complete variant
        complete_fn_path = config.function.rsplit(".", 1)
        if len(complete_fn_path) == 2:
            module_path, fn_name = complete_fn_path
            try:
                module = importlib.import_module(module_path)
                self._complete_fn = getattr(module, f"{fn_name}_complete", None)
            except (ImportError, AttributeError):
                self._complete_fn = None
        else:
            self._complete_fn = None

    def _load_function(self, dotted_path: str):
        parts = dotted_path.rsplit(".", 1)
        if len(parts) != 2:
            raise CortexLLMError(f"Invalid function path: {dotted_path}. Use 'module.function'.", provider="custom")
        module_path, fn_name = parts
        try:
            module = importlib.import_module(module_path)
        except ImportError as e:
            raise CortexLLMError(f"Cannot import module '{module_path}': {e}", provider="custom")
        fn = getattr(module, fn_name, None)
        if fn is None:
            raise CortexLLMError(f"Function '{fn_name}' not found in '{module_path}'.", provider="custom")
        return fn

    async def stream(
        self,
        messages: List[Dict],
        system: str,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        try:
            async for token in self._stream_fn(messages, system, self._config):
                yield token
        except Exception as e:
            raise CortexLLMError(f"Custom provider stream error: {e}", provider="custom")

    async def complete(
        self,
        messages: List[Dict],
        system: str,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        if self._complete_fn:
            try:
                return await self._complete_fn(messages, system, self._config)
            except Exception as e:
                raise CortexLLMError(f"Custom provider complete error: {e}", provider="custom")
        # Fallback: collect stream
        tokens = []
        async for token in self.stream(messages, system, max_tokens):
            tokens.append(token)
        return LLMResponse(content="".join(tokens), model=self._config.model, provider="custom")

    async def verify(self) -> bool:
        try:
            tokens = []
            async for t in self.stream([{"role": "user", "content": "Hi"}], system="", max_tokens=200):
                tokens.append(t)
                if len(tokens) > 0:
                    return True
            return True
        except Exception:
            return False
