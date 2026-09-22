"""Strict transport schemas for the v2 API."""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Slug = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=2,
        max_length=80,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    ),
]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MeResponse(BaseModel):
    tenant_id: str
    principal_id: str
    subject: str
    display_name: str
    roles: list[str]


class ConnectorResponse(BaseModel):
    name: str
    capability: Literal["READ", "WRITE"]
    actions: list[str]
    configured: bool


class AgentCreate(StrictModel):
    slug: Slug
    name: Name
    description: str = Field(default="", max_length=500)


class AgentResponse(BaseModel):
    id: str
    slug: str
    name: str
    description: str
    created_by: str
    created_at: datetime


class AgentVersionCreate(StrictModel):
    definition: dict[str, Any]


class AgentVersionUpdate(StrictModel):
    lock_version: int = Field(ge=1)
    definition: dict[str, Any]


class PublishRequest(StrictModel):
    lock_version: int = Field(ge=1)


class AgentVersionResponse(BaseModel):
    id: str
    agent_id: str
    version: int
    status: Literal["draft", "published"]
    definition: dict[str, Any]
    lock_version: int
    created_by: str
    created_at: datetime
    published_at: datetime | None


class AgentDetailResponse(AgentResponse):
    versions: list[AgentVersionResponse]


class WorkflowCreate(StrictModel):
    slug: Slug
    name: Name
    description: str = Field(default="", max_length=500)


class WorkflowResponse(BaseModel):
    id: str
    slug: str
    name: str
    description: str
    created_by: str
    created_at: datetime


class WorkflowVersionCreate(StrictModel):
    agent_version_id: str
    connector: Literal["simulated_erp", "m365_mail_intake_v1"]
    action: Literal["create_sales_order", "list_messages"]
    config: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    approval_required: Literal[True] = True


class WorkflowVersionUpdate(WorkflowVersionCreate):
    lock_version: int = Field(ge=1)


class WorkflowVersionResponse(BaseModel):
    id: str
    workflow_id: str
    version: int
    status: Literal["draft", "published"]
    agent_version_id: str
    connector: str
    action: str
    config: dict[str, Any]
    input_schema: dict[str, Any]
    approval_required: bool
    lock_version: int
    created_by: str
    created_at: datetime
    published_at: datetime | None


class WorkflowDetailResponse(WorkflowResponse):
    versions: list[WorkflowVersionResponse]


class RunDryRunRequest(StrictModel):
    input: dict[str, Any]


class RunApprovalRequest(StrictModel):
    proposal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class RunRejectionRequest(StrictModel):
    reason: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=3, max_length=500),
    ]


class RunResponse(BaseModel):
    id: str
    workflow_id: str
    workflow_version_id: str
    agent_version_id: str
    status: str
    input: dict[str, Any]
    proposal: dict[str, Any]
    proposal_hash: str
    summary: str
    target: str
    payload: dict[str, Any]
    approval_required: bool
    approved_by: str | None
    approved_at: datetime | None
    rejected_by: str | None
    rejected_at: datetime | None
    rejection_reason: str | None
    created_at: datetime
    updated_at: datetime


class ExecutionOperationResponse(BaseModel):
    operation_id: str
    run_id: str
    status: str
    connector: str
    action: str
    result: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None


class V2AuditEventResponse(BaseModel):
    id: int
    run_id: str | None
    entity_type: str
    entity_id: str
    event_type: str
    actor_id: str
    idempotency_key: str | None
    payload: dict[str, Any]
    created_at: datetime
