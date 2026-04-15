"""Tests for OpenAI-compatible providers (OpenAI, Gemini, Grok, Mistral, DeepSeek)."""
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from cortex.config.schema import LLMProviderConfig
from cortex.exceptions import CortexLLMError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(provider: str, model: str = "test-model", api_key_env: str = None):
    return LLMProviderConfig(
        provider=provider,
        model=model,
        max_tokens=100,
        api_key_env_var=api_key_env,
    )


def _sse_lines(chunks):
    """Build raw SSE byte lines from a list of content strings."""
    lines = []
    for content in chunks:
        payload = json.dumps({
            "choices": [{"delta": {"content": content}}],
        })
        lines.append(f"data: {payload}\n".encode())
    lines.append(b"data: [DONE]\n")
    return lines


def _complete_response(content="Hello!", model="test-model"):
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "model": model,
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }


# ---------------------------------------------------------------------------
# Provider instantiation — missing key raises
# ---------------------------------------------------------------------------

PROVIDERS = [
    ("openai", "cortex.llm.providers.openai_provider", "OpenAIProvider", "OPENAI_API_KEY"),
    ("gemini", "cortex.llm.providers.gemini_provider", "GeminiProvider", "GEMINI_API_KEY"),
    ("grok", "cortex.llm.providers.grok_provider", "GrokProvider", "XAI_API_KEY"),
    ("mistral", "cortex.llm.providers.mistral_provider", "MistralProvider", "MISTRAL_API_KEY"),
    ("deepseek", "cortex.llm.providers.deepseek_provider", "DeepSeekProvider", "DEEPSEEK_API_KEY"),
]


@pytest.mark.parametrize("provider_name,module_path,class_name,env_var", PROVIDERS)
def test_missing_api_key_raises(provider_name, module_path, class_name, env_var, monkeypatch):
    """Each provider should raise CortexLLMError when the API key env var is unset."""
    monkeypatch.delenv(env_var, raising=False)
    monkeypatch.delenv("api_key_env_var", raising=False)
    import importlib
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    config = _make_config(provider_name)
    with pytest.raises(CortexLLMError):
        cls(config)


@pytest.mark.parametrize("provider_name,module_path,class_name,env_var", PROVIDERS)
def test_init_with_api_key(provider_name, module_path, class_name, env_var, monkeypatch):
    """Provider initialises successfully when the API key is set."""
    monkeypatch.setenv(env_var, "test-key-12345")
    import importlib
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    config = _make_config(provider_name)
    provider = cls(config)
    assert provider.PROVIDER_NAME == provider_name
    assert provider._api_key == "test-key-12345"


@pytest.mark.parametrize("provider_name,module_path,class_name,env_var", PROVIDERS)
def test_init_with_custom_env_var(provider_name, module_path, class_name, env_var, monkeypatch):
    """Provider reads from a custom env var specified in api_key_env_var."""
    monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("MY_CUSTOM_KEY", "custom-key-999")
    import importlib
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    config = _make_config(provider_name, api_key_env="MY_CUSTOM_KEY")
    provider = cls(config)
    assert provider._api_key == "custom-key-999"


# ---------------------------------------------------------------------------
# _build_provider routing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("provider_name,module_path,class_name,env_var", PROVIDERS)
def test_build_provider_routes_correctly(provider_name, module_path, class_name, env_var, monkeypatch):
    """_build_provider should return the correct provider class."""
    monkeypatch.setenv(env_var, "test-key")
    from cortex.llm.client import _build_provider
    config = _make_config(provider_name)
    provider = _build_provider(config)
    assert type(provider).__name__ == class_name


# ---------------------------------------------------------------------------
# Base class: _build_body
# ---------------------------------------------------------------------------

def test_build_body_includes_system_message(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    from cortex.llm.providers.openai_provider import OpenAIProvider
    config = _make_config("openai")
    provider = OpenAIProvider(config)
    body = provider._build_body(
        [{"role": "user", "content": "Hello"}],
        system="You are helpful.",
        max_tokens=50,
        stream=False,
    )
    assert body["messages"][0] == {"role": "system", "content": "You are helpful."}
    assert body["messages"][1] == {"role": "user", "content": "Hello"}
    assert body["max_tokens"] == 50
    assert body["stream"] is False


def test_build_body_empty_system(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    from cortex.llm.providers.openai_provider import OpenAIProvider
    config = _make_config("openai")
    provider = OpenAIProvider(config)
    body = provider._build_body(
        [{"role": "user", "content": "Hi"}],
        system="",
        max_tokens=None,
        stream=True,
    )
    # No system message when empty
    assert body["messages"][0]["role"] == "user"
    assert body["stream"] is True
    assert body["max_tokens"] == 100  # falls back to config


# ---------------------------------------------------------------------------
# Default models and base URLs
# ---------------------------------------------------------------------------

def test_default_models_and_urls(monkeypatch):
    """Each provider should have sensible defaults."""
    for provider_name, module_path, class_name, env_var in PROVIDERS:
        monkeypatch.setenv(env_var, "test-key")
        import importlib
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        assert cls.DEFAULT_MODEL != "", f"{class_name} has no DEFAULT_MODEL"
        assert cls.DEFAULT_BASE_URL != "", f"{class_name} has no DEFAULT_BASE_URL"
        assert cls.DEFAULT_API_KEY_ENV == env_var


# ---------------------------------------------------------------------------
# Base URL override
# ---------------------------------------------------------------------------

def test_base_url_override(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    from cortex.llm.providers.openai_provider import OpenAIProvider
    config = LLMProviderConfig(
        provider="openai",
        model="gpt-4o",
        max_tokens=100,
        base_url="https://my-proxy.example.com/v1",
    )
    provider = OpenAIProvider(config)
    assert provider._base_url == "https://my-proxy.example.com/v1"


# ---------------------------------------------------------------------------
# complete() — mocked HTTP
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_success(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    from cortex.llm.providers.openai_provider import OpenAIProvider
    config = _make_config("openai")
    provider = OpenAIProvider(config)

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=_complete_response("Hi there!", "gpt-4o"))
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await provider.complete(
            [{"role": "user", "content": "Hello"}],
            system="Be helpful",
        )

    assert result.content == "Hi there!"
    assert result.model == "gpt-4o"
    assert result.provider == "openai"
    assert result.usage.input_tokens == 5
    assert result.usage.output_tokens == 3
    assert result.usage.total_tokens == 8


@pytest.mark.asyncio
async def test_complete_http_error(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    from cortex.llm.providers.openai_provider import OpenAIProvider
    config = _make_config("openai")
    provider = OpenAIProvider(config)

    mock_resp = AsyncMock()
    mock_resp.status = 429
    mock_resp.text = AsyncMock(return_value="Rate limit exceeded")
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(CortexLLMError) as exc_info:
            await provider.complete(
                [{"role": "user", "content": "Hello"}],
                system="",
            )
    assert exc_info.value.status_code == 429


# ---------------------------------------------------------------------------
# stream() — mocked HTTP with SSE lines
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_success(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    from cortex.llm.providers.openai_provider import OpenAIProvider
    config = _make_config("openai")
    provider = OpenAIProvider(config)

    sse_data = _sse_lines(["Hello", " world", "!"])

    async def mock_content_iter():
        for line in sse_data:
            yield line

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.content = mock_content_iter()
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    tokens = []
    with patch("aiohttp.ClientSession", return_value=mock_session):
        async for token in provider.stream(
            [{"role": "user", "content": "Hello"}],
            system="",
        ):
            tokens.append(token)

    assert tokens == ["Hello", " world", "!"]


@pytest.mark.asyncio
async def test_stream_http_error(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    from cortex.llm.providers.openai_provider import OpenAIProvider
    config = _make_config("openai")
    provider = OpenAIProvider(config)

    mock_resp = AsyncMock()
    mock_resp.status = 500
    mock_resp.text = AsyncMock(return_value="Internal server error")
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(CortexLLMError) as exc_info:
            async for _ in provider.stream(
                [{"role": "user", "content": "Hello"}],
                system="",
            ):
                pass
    assert exc_info.value.status_code == 500
