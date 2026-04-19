"""AWS Bedrock provider with SigV4 auth."""
import json
import os
from typing import AsyncIterator, Dict, List, Optional

from cortex.config.schema import LLMProviderConfig
from cortex.exceptions import CortexLLMError
from cortex.llm.context import LLMResponse, TokenUsage


class BedrockProvider:
    """Calls Claude models via AWS Bedrock using boto3."""

    def __init__(self, config: LLMProviderConfig):
        self._config = config
        try:
            import boto3
            self._boto3 = boto3
        except ImportError:
            raise CortexLLMError("boto3 required for Bedrock provider. Install: pip install boto3", provider="bedrock")

        region = os.environ.get(config.region_env_var or "AWS_DEFAULT_REGION", "us-east-1")
        session_kwargs: dict = {"region_name": region}

        if config.access_key_env_var:
            session_kwargs["aws_access_key_id"] = os.environ.get(config.access_key_env_var, "")
        if config.secret_key_env_var:
            session_kwargs["aws_secret_access_key"] = os.environ.get(config.secret_key_env_var, "")
        if config.session_token_env_var:
            token = os.environ.get(config.session_token_env_var)
            if token:
                session_kwargs["aws_session_token"] = token

        session = boto3.Session(**session_kwargs)
        self._client = session.client("bedrock-runtime")

    async def stream(
        self,
        messages: List[Dict],
        system: str,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        import asyncio
        max_tok = max_tokens or self._config.max_tokens
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tok,
            "system": system,
            "messages": messages,
        }
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self._client.invoke_model_with_response_stream(
                    modelId=self._config.model,
                    body=json.dumps(body),
                    contentType="application/json",
                    accept="application/json",
                )
            )
            for event in response["body"]:
                chunk = json.loads(event["chunk"]["bytes"].decode())
                if chunk.get("type") == "content_block_delta":
                    delta = chunk.get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield delta.get("text", "")
        except Exception as e:
            raise CortexLLMError(f"Bedrock streaming error: {e}", provider="bedrock")

    async def complete(
        self,
        messages: List[Dict],
        system: str,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        import asyncio
        max_tok = max_tokens or self._config.max_tokens
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tok,
            "system": system,
            "messages": messages,
        }
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self._client.invoke_model(
                modelId=self._config.model,
                body=json.dumps(body),
                contentType="application/json",
                accept="application/json",
            )
        )
        result = json.loads(response["body"].read().decode())
        content = result["content"][0]["text"] if result.get("content") else ""
        usage_data = result.get("usage", {})
        usage = TokenUsage(
            input_tokens=usage_data.get("input_tokens", 0),
            output_tokens=usage_data.get("output_tokens", 0),
            total_tokens=usage_data.get("input_tokens", 0) + usage_data.get("output_tokens", 0),
        )
        return LLMResponse(content=content, model=self._config.model, usage=usage, provider="bedrock")

    async def verify(self) -> bool:
        try:
            await self.complete([{"role": "user", "content": "Hi"}], system="", max_tokens=200)
            return True
        except Exception:
            return False
