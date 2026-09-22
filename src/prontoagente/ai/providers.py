"""Network-independent provider adapters for governed order extraction."""

import hashlib
import json
from typing import Any
from urllib.parse import urlsplit

import httpx

from prontoagente.ai.tooling import TOOL_DESCRIPTION
from prontoagente.ai.types import (
    AiProvider,
    ProviderFailure,
    ProviderRequest,
    ProviderResponse,
    ProviderUsage,
    ToolCall,
)
from prontoagente.canonical import canonical_json, sha256_digest
from prontoagente.config import Settings
from prontoagente.v2.demo_connectors import canonical_order, parse_order_subject


def _estimated_tokens(value: str) -> int:
    """Conservative deterministic accounting for the offline fake provider."""

    return max(1, (len(value.encode("utf-8")) + 3) // 4)


class FakeDeterministicProvider:
    """Offline provider that deterministically extracts the synthetic PA1 grammar."""

    name = "fake"

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        if request.provider != self.name:
            raise ProviderFailure("provider_mismatch", outcome_unknown=False)
        try:
            document = json.loads(request.user_data)
            envelope = document["untrusted_email_metadata"]
            if not isinstance(envelope, dict):
                raise TypeError
            subject = envelope["subject"]
            if not isinstance(subject, str):
                raise TypeError
            arguments = canonical_order(parse_order_subject(subject))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderFailure("fake_input_invalid", outcome_unknown=False) from exc

        response_hash = sha256_digest(
            {"tool_calls": [{"name": request.tool_name, "arguments": arguments}]}
        )
        return ProviderResponse(
            provider=self.name,
            model=request.model,
            tool_calls=(ToolCall(name=request.tool_name, arguments=arguments),),
            usage=ProviderUsage(
                input_tokens=_estimated_tokens(request.system_prompt + request.user_data),
                output_tokens=_estimated_tokens(canonical_json(arguments)),
            ),
            response_hash=response_hash,
        )


class OpenAIResponsesProvider:
    """Opt-in adapter for an OpenAI Responses-compatible HTTPS deployment."""

    name = "openai"

    def __init__(
        self,
        settings: Settings,
        *,
        tenant_network_enabled: bool,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        endpoint = settings.ai_openai_responses_url
        model_hosts = set(settings.ai_openai_allowed_hosts)
        if (
            not settings.ai_network_enabled
            or not tenant_network_enabled
            or not endpoint
            or not settings.ai_openai_api_key
        ):
            raise ProviderFailure("provider_network_disabled", outcome_unknown=False)
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in model_hosts
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ProviderFailure("provider_endpoint_not_allowlisted", outcome_unknown=False)
        self._endpoint = endpoint
        self._api_key = settings.ai_openai_api_key
        self._allowed_models = set(settings.ai_openai_allowed_models)
        self._max_response_bytes = settings.ai_max_response_bytes
        self._client = httpx.Client(
            timeout=settings.ai_http_timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )

    def _read_bounded(self, response: httpx.Response) -> bytes:
        length = response.headers.get("content-length")
        if length is not None:
            try:
                if int(length) > self._max_response_bytes:
                    raise ProviderFailure("provider_response_too_large", outcome_unknown=True)
            except ValueError as exc:
                raise ProviderFailure("provider_protocol_invalid", outcome_unknown=True) from exc
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > self._max_response_bytes:
                raise ProviderFailure("provider_response_too_large", outcome_unknown=True)
            chunks.append(chunk)
        return b"".join(chunks)

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        if request.provider != self.name:
            raise ProviderFailure("provider_mismatch", outcome_unknown=False)
        if request.model not in self._allowed_models:
            raise ProviderFailure("provider_model_not_allowlisted", outcome_unknown=False)
        payload: dict[str, Any] = {
            "model": request.model,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": request.system_prompt}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": request.user_data}],
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "name": request.tool_name,
                    "description": TOOL_DESCRIPTION,
                    "parameters": request.tool_schema,
                    "strict": True,
                }
            ],
            "tool_choice": {"type": "function", "name": request.tool_name},
            "parallel_tool_calls": False,
            "max_output_tokens": request.max_output_tokens,
            "store": False,
        }
        try:
            with self._client.stream(
                "POST",
                self._endpoint,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "X-Client-Request-Id": request.correlation_id,
                },
                content=canonical_json(payload).encode(),
            ) as response:
                if not 200 <= response.status_code < 300:
                    raise ProviderFailure("provider_http_error", outcome_unknown=True)
                raw = self._read_bounded(response)
        except ProviderFailure:
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ProviderFailure("provider_transport_error", outcome_unknown=True) from exc

        try:
            data = json.loads(raw)
            output = data["output"]
            usage = data["usage"]
            if not isinstance(output, list) or len(output) != 1:
                raise TypeError
            item = output[0]
            if not isinstance(item, dict) or item.get("type") != "function_call":
                raise TypeError
            name = item["name"]
            arguments = json.loads(item["arguments"])
            input_tokens = usage["input_tokens"]
            output_tokens = usage["output_tokens"]
            if (
                not isinstance(name, str)
                or not isinstance(arguments, dict)
                or not isinstance(input_tokens, int)
                or isinstance(input_tokens, bool)
                or input_tokens < 0
                or not isinstance(output_tokens, int)
                or isinstance(output_tokens, bool)
                or output_tokens < 0
            ):
                raise TypeError
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ProviderFailure("provider_protocol_invalid", outcome_unknown=True) from exc
        return ProviderResponse(
            provider=self.name,
            model=request.model,
            tool_calls=(ToolCall(name=name, arguments=arguments),),
            usage=ProviderUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
            response_hash="sha256:" + hashlib.sha256(raw).hexdigest(),
        )

    def close(self) -> None:
        self._client.close()


def build_provider(
    name: str,
    settings: Settings,
    *,
    tenant_network_enabled: bool,
    transport: httpx.BaseTransport | None = None,
) -> AiProvider:
    if name == "fake":
        return FakeDeterministicProvider()
    if name == "openai":
        return OpenAIResponsesProvider(
            settings,
            tenant_network_enabled=tenant_network_enabled,
            transport=transport,
        )
    raise ProviderFailure("provider_not_allowlisted", outcome_unknown=False)
