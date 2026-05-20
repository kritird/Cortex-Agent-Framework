"""LLMClient — routes calls to configured provider."""
import asyncio
import contextvars
import time
from typing import AsyncIterator, Dict, List, Optional, Union

from cortex.config.schema import LLMAccessConfig, LLMProviderConfig
from cortex.exceptions import CortexProviderError
from cortex.llm.adaptive_gate import AdaptiveLLMGate
from cortex.llm.context import LLMResponse


class LLMQueueCredit:
    """Accumulates the seconds a task spent queued for the shared LLM gate.

    A single-stream backend (e.g. one local Ollama) serializes inference, so
    concurrent tasks queue for it. That queue-wait is not the task's own work —
    this credit lets the task timeout exclude it. See
    GenericMCPAgent._run_with_credit_timeout.
    """
    __slots__ = ("seconds",)

    def __init__(self) -> None:
        self.seconds = 0.0


# Per-task credit accumulator: set by the task-timeout wrapper, read and
# updated by LLMClient while it waits to acquire the LLM gate. Unset (None)
# for LLM calls made outside a task (decomposition, synthesis) — those are
# simply not credited.
llm_queue_credit: contextvars.ContextVar = contextvars.ContextVar(
    "llm_queue_credit", default=None
)


def _credit_gate_wait(seconds: float) -> None:
    """Attribute `seconds` of LLM-gate queue-wait to the current task, if any."""
    credit = llm_queue_credit.get()
    if credit is not None:
        credit.seconds += seconds


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

    def __init__(
        self,
        config: LLMAccessConfig,
        max_parallel_llm_calls: int = 1,
        adaptive_llm_concurrency: bool = True,
    ):
        self._config = config
        self._providers: Dict[str, object] = {}
        # Gate on concurrent LLM HTTP requests (see AgentConcurrencyConfig).
        # The framework resolves max_parallel_llm_calls before constructing the
        # client (auto-derived from provider+model via cortex.llm.model_power
        # when the user leaves it unset).
        self._max_parallel_llm_calls = max(1, int(max_parallel_llm_calls))
        self._adaptive = bool(adaptive_llm_concurrency)
        self._llm_gate: Optional[Union[asyncio.Semaphore, AdaptiveLLMGate]] = None
        # Eagerly build default
        self._providers["default"] = _build_provider(config.default)
        # Build named providers
        for name, prov_config in config.providers.items():
            self._providers[name] = _build_provider(prov_config)

    def _gate(self) -> Union[asyncio.Semaphore, AdaptiveLLMGate]:
        """Lazily create the LLM concurrency gate, bound to the running loop.

        Returns an AdaptiveLLMGate when adaptive_llm_concurrency is on (default),
        which self-tunes between 1 and max_parallel_llm_calls based on observed
        latency and errors. Returns a plain asyncio.Semaphore pinned at the
        ceiling otherwise.
        """
        if self._llm_gate is None:
            if self._adaptive:
                self._llm_gate = AdaptiveLLMGate(
                    initial=self._max_parallel_llm_calls,
                    min_limit=1,
                    max_limit=self._max_parallel_llm_calls,
                )
            else:
                self._llm_gate = asyncio.Semaphore(self._max_parallel_llm_calls)
        return self._llm_gate

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
        """Route streaming call to configured provider, gated for concurrency.

        The gate is held for the whole stream; time spent waiting to acquire it
        is credited to the calling task so its timeout excludes queue-wait.
        """
        provider = self._get_provider(provider_name)
        _wait_start = time.monotonic()
        gate = self._gate()
        async with gate:
            _credit_gate_wait(time.monotonic() - _wait_start)
            call_start = time.monotonic()
            ok, saw_token = True, False
            try:
                async for token in provider.stream(messages, system, max_tokens):
                    if token:
                        saw_token = True
                    yield token
            except Exception:
                ok = False
                raise
            finally:
                if isinstance(gate, AdaptiveLLMGate):
                    await gate.record_outcome(
                        ok=ok,
                        empty=not saw_token,
                        latency=time.monotonic() - call_start,
                    )

    async def complete(
        self,
        messages: List[Dict],
        system: str,
        provider_name: str = "default",
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """Non-streaming completion, gated for concurrency."""
        provider = self._get_provider(provider_name)
        _wait_start = time.monotonic()
        gate = self._gate()
        async with gate:
            _credit_gate_wait(time.monotonic() - _wait_start)
            call_start = time.monotonic()
            ok, empty = True, False
            try:
                response = await provider.complete(messages, system, max_tokens)
                empty = not (response and getattr(response, "content", None))
                return response
            except Exception:
                ok = False
                raise
            finally:
                if isinstance(gate, AdaptiveLLMGate):
                    await gate.record_outcome(
                        ok=ok,
                        empty=empty,
                        latency=time.monotonic() - call_start,
                    )

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
