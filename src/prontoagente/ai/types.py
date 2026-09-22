"""Provider-neutral request and response contracts with no web or persistence coupling."""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    provider: str
    model: str
    correlation_id: str
    prompt_id: str
    prompt_hash: str
    system_prompt: str
    user_data: str
    tool_name: str
    tool_schema: dict[str, Any]
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    provider: str
    model: str
    tool_calls: tuple[ToolCall, ...]
    usage: ProviderUsage
    response_hash: str


class AiProvider(Protocol):
    def invoke(self, request: ProviderRequest) -> ProviderResponse: ...


class ProviderFailure(Exception):
    """Sanitized provider failure safe to store in operational records."""

    def __init__(self, code: str, *, outcome_unknown: bool) -> None:
        super().__init__(code)
        self.code = code
        self.outcome_unknown = outcome_unknown

