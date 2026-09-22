"""Version-one HTTP routes."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from prontoagente.api.dependencies import MutationContext, mutation_context
from prontoagente.db import get_session
from prontoagente.schemas import (
    ApprovalRequest,
    AuditEventResponse,
    DryRunRequest,
    HealthResponse,
    RejectionRequest,
    SimulationOperationResponse,
    WorkflowResponse,
)
from prontoagente.services import (
    approve_proposal,
    create_dry_run,
    get_simulation_operation,
    get_workflow,
    list_audit_events,
    reject_proposal,
    simulate_approved_proposal,
)

router = APIRouter()
SessionDependency = Annotated[Session, Depends(get_session)]
MutationDependency = Annotated[MutationContext, Depends(mutation_context)]


@router.get("/health", response_model=HealthResponse, tags=["operations"])
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.post(
    "/erp-drafts/dry-run",
    response_model=WorkflowResponse,
    status_code=201,
    tags=["erp-drafts"],
)
def dry_run(
    payload: DryRunRequest,
    session: SessionDependency,
    context: MutationDependency,
) -> Any:
    status, body = create_dry_run(
        session,
        request=payload,
        actor_id=context.actor_id,
        idempotency_key=context.idempotency_key,
    )
    return JSONResponse(status_code=status, content=body)


@router.post(
    "/erp-drafts/{workflow_id}/approve",
    response_model=WorkflowResponse,
    tags=["erp-drafts"],
)
def approve(
    workflow_id: str,
    payload: ApprovalRequest,
    session: SessionDependency,
    context: MutationDependency,
) -> Any:
    status, body = approve_proposal(
        session,
        workflow_id=workflow_id,
        request=payload,
        actor_id=context.actor_id,
        idempotency_key=context.idempotency_key,
    )
    return JSONResponse(status_code=status, content=body)


@router.post(
    "/erp-drafts/{workflow_id}/reject",
    response_model=WorkflowResponse,
    tags=["erp-drafts"],
)
def reject(
    workflow_id: str,
    payload: RejectionRequest,
    session: SessionDependency,
    context: MutationDependency,
) -> Any:
    status, body = reject_proposal(
        session,
        workflow_id=workflow_id,
        request=payload,
        actor_id=context.actor_id,
        idempotency_key=context.idempotency_key,
    )
    return JSONResponse(status_code=status, content=body)


@router.post(
    "/erp-drafts/{workflow_id}/simulate",
    response_model=SimulationOperationResponse,
    responses={
        202: {"model": SimulationOperationResponse},
        502: {"model": SimulationOperationResponse},
    },
    tags=["erp-drafts"],
)
def simulate(
    workflow_id: str,
    session: SessionDependency,
    context: MutationDependency,
) -> Any:
    status, body = simulate_approved_proposal(
        session,
        workflow_id=workflow_id,
        actor_id=context.actor_id,
        idempotency_key=context.idempotency_key,
    )
    return JSONResponse(
        status_code=status,
        content=body,
        headers={"Location": f"/v1/simulation-operations/{body['operation_id']}"},
    )


@router.get(
    "/simulation-operations/{operation_id}",
    response_model=SimulationOperationResponse,
    tags=["erp-drafts"],
)
def read_simulation_operation(operation_id: str, session: SessionDependency) -> Any:
    return get_simulation_operation(session, operation_id)


@router.get(
    "/erp-drafts/{workflow_id}", response_model=WorkflowResponse, tags=["erp-drafts"]
)
def read_workflow(workflow_id: str, session: SessionDependency) -> Any:
    return get_workflow(session, workflow_id)


@router.get(
    "/erp-drafts/{workflow_id}/audit-events",
    response_model=list[AuditEventResponse],
    tags=["erp-drafts"],
)
def read_audit_events(workflow_id: str, session: SessionDependency) -> Any:
    return list_audit_events(session, workflow_id)
