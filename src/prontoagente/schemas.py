"""Pydantic v2 transport schemas and public enums."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    ),
]
Sku = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$",
    ),
]


class StrictModel(BaseModel):
    """Reject unknown input fields to prevent silently ignored intent."""

    model_config = ConfigDict(extra="forbid")


class ActionType(StrEnum):
    """Explicit allowlist of mutations the workflow understands."""

    CREATE_SALES_ORDER = "create_sales_order"


class WorkflowStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    SIMULATED = "simulated"


class AuditEventType(StrEnum):
    DRAFT_PROPOSED = "draft_proposed"
    PROPOSAL_APPROVED = "proposal_approved"
    PROPOSAL_REJECTED = "proposal_rejected"
    SIMULATION_STARTED = "simulation_started"
    SIMULATION_COMPLETED = "simulation_completed"
    SIMULATION_FAILED = "simulation_failed"


class SimulationOperationStatus(StrEnum):
    SIMULATING = "simulating"
    SIMULATED = "simulated"
    SIMULATION_FAILED = "simulation_failed"


class OrderLine(StrictModel):
    sku: Sku
    quantity: int = Field(gt=0, le=10_000)
    unit_price: Decimal = Field(ge=Decimal("0"), max_digits=12, decimal_places=2)


class OrderInput(StrictModel):
    external_order_id: Identifier
    customer_id: Identifier
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")
    lines: list[OrderLine] = Field(min_length=1, max_length=100)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


class DryRunRequest(StrictModel):
    action_type: ActionType
    order: OrderInput


class ApprovalRequest(StrictModel):
    proposal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class RejectionRequest(StrictModel):
    reason: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=3, max_length=500),
    ]


class WorkflowResponse(BaseModel):
    id: str
    action_type: ActionType
    status: WorkflowStatus
    order: dict[str, Any]
    proposal: dict[str, Any]
    proposal_hash: str
    approved_by: str | None
    approved_at: datetime | None
    rejected_by: str | None
    rejected_at: datetime | None
    rejection_reason: str | None
    simulation_result: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class AuditEventResponse(BaseModel):
    id: int
    workflow_id: str
    event_type: AuditEventType
    actor_id: str
    idempotency_key: str
    payload: dict[str, Any]
    created_at: datetime


class SimulationOperationResponse(BaseModel):
    operation_id: str
    workflow_id: str
    status: SimulationOperationStatus
    proposal_hash: str
    connector: str
    requested_by: str
    result: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None


class HealthResponse(BaseModel):
    status: str
