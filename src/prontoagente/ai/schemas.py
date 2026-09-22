"""Strict public schemas for Milestone 3 AI preparation."""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AiMailEnvelope(StrictModel):
    source_connector: Literal["demo_mailbox_v1"]
    message_id: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        ),
    ]
    internet_message_id: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=3,
            max_length=192,
            pattern=r"^<[A-Za-z0-9][A-Za-z0-9._@:-]*>$",
        ),
    ]
    sender: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            to_lower=True,
            min_length=3,
            max_length=254,
            pattern=(
                r"^[A-Za-z0-9._%+-]{1,64}@"
                r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
            ),
        ),
    ]
    subject: Annotated[
        str,
        StringConstraints(min_length=1, max_length=512, pattern=r"^[ -~]+$"),
    ]


class AiPreparationRequest(StrictModel):
    envelope: AiMailEnvelope


class AiPreparationResponse(BaseModel):
    id: str
    status: Literal["queued", "processing", "completed", "failed", "unknown"]
    correlation_id: str
    workflow_id: str
    provider: Literal["fake", "openai"]
    model: str
    prompt_id: str
    prompt_hash: str
    tool_name: str
    input_hash: str | None
    output_hash: str | None
    result_run_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_microusd: int | None
    duration_ms: int | None
    error_code: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class AiPolicyUpdate(StrictModel):
    enabled: bool
    provider: Literal["fake", "openai"]
    model: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    network_enabled: bool = False
    max_input_tokens: int = Field(ge=1, le=4096)
    max_output_tokens: int = Field(ge=1, le=1024)
    max_run_microusd: int = Field(ge=0, le=100_000)
    daily_input_tokens: int = Field(ge=1, le=1_000_000)
    daily_output_tokens: int = Field(ge=1, le=250_000)
    daily_microusd: int = Field(ge=0, le=1_000_000)
    lock_version: int = Field(ge=1)


class AiPolicyResponse(BaseModel):
    enabled: bool
    provider: Literal["fake", "openai"]
    model: str
    network_enabled: bool
    max_input_tokens: int
    max_output_tokens: int
    max_run_microusd: int
    daily_input_tokens: int
    daily_output_tokens: int
    daily_microusd: int
    lock_version: int
    created_at: datetime
    updated_at: datetime


class AiUsageResponse(BaseModel):
    usage_date: date
    used_input_tokens: int
    used_output_tokens: int
    used_microusd: int
    reserved_input_tokens: int
    reserved_output_tokens: int
    reserved_microusd: int
