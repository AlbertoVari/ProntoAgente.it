"""Tenant-scoped HTTP API for Milestone 2."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from prontoagente.db import get_session
from prontoagente.v2.auth import (
    ALLOWED_ROLES,
    ROLE_APPROVER,
    ROLE_BUILDER,
    ROLE_OPERATOR,
    ROLE_OWNER,
    AuthContext,
    require_roles,
)
from prontoagente.v2.catalog import (
    create_agent,
    create_agent_version,
    create_workflow,
    create_workflow_version,
    get_agent,
    get_workflow,
    list_agents,
    list_workflows,
    publish_agent_version,
    publish_workflow_version,
    update_agent_version,
    update_workflow_version,
)
from prontoagente.v2.connectors import connector_catalog
from prontoagente.v2.runs import (
    approve_run,
    create_dry_run,
    create_run_from_mail,
    execute_run,
    get_execution_operation,
    get_run,
    list_run_audit,
    reject_run,
)
from prontoagente.v2.schemas import (
    AgentCreate,
    AgentDetailResponse,
    AgentResponse,
    AgentVersionCreate,
    AgentVersionResponse,
    AgentVersionUpdate,
    ConnectorResponse,
    ExecutionOperationResponse,
    MeResponse,
    PublishRequest,
    RunApprovalRequest,
    RunDryRunRequest,
    RunFromMailRequest,
    RunRejectionRequest,
    RunResponse,
    V2AuditEventResponse,
    WorkflowCreate,
    WorkflowDetailResponse,
    WorkflowResponse,
    WorkflowVersionCreate,
    WorkflowVersionResponse,
    WorkflowVersionUpdate,
)

router = APIRouter()
SessionDependency = Annotated[Session, Depends(get_session)]
ViewerContext = Annotated[AuthContext, Depends(require_roles(*sorted(ALLOWED_ROLES)))]
BuilderContext = Annotated[AuthContext, Depends(require_roles(ROLE_OWNER, ROLE_BUILDER))]
OperatorContext = Annotated[
    AuthContext, Depends(require_roles(ROLE_OWNER, ROLE_BUILDER, ROLE_OPERATOR))
]
MailPreparerContext = Annotated[
    AuthContext, Depends(require_roles(ROLE_OWNER, ROLE_OPERATOR))
]
ExecutorContext = Annotated[AuthContext, Depends(require_roles(ROLE_OWNER, ROLE_OPERATOR))]
ApproverContext = Annotated[AuthContext, Depends(require_roles(ROLE_OWNER, ROLE_APPROVER))]
IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]


@router.get("/me", response_model=MeResponse)
def me(context: ViewerContext) -> MeResponse:
    return MeResponse(
        tenant_id=context.tenant_id,
        principal_id=context.principal_id,
        subject=context.subject,
        display_name=context.display_name,
        roles=sorted(context.roles),
    )


@router.get("/connectors", response_model=list[ConnectorResponse])
def connectors(context: ViewerContext) -> list[dict[str, Any]]:
    return connector_catalog(context.tenant_id)


@router.get("/agents", response_model=list[AgentResponse])
def agents(session: SessionDependency, context: ViewerContext) -> Any:
    return list_agents(session, context)


@router.post("/agents", response_model=AgentResponse, status_code=201)
def add_agent(payload: AgentCreate, session: SessionDependency, context: BuilderContext) -> Any:
    return create_agent(session, context, payload)


@router.get("/agents/{agent_id}", response_model=AgentDetailResponse)
def agent(agent_id: str, session: SessionDependency, context: ViewerContext) -> Any:
    return get_agent(session, context, agent_id)


@router.post(
    "/agents/{agent_id}/versions", response_model=AgentVersionResponse, status_code=201
)
def add_agent_version(
    agent_id: str,
    payload: AgentVersionCreate,
    session: SessionDependency,
    context: BuilderContext,
) -> Any:
    return create_agent_version(session, context, agent_id, payload)


@router.patch("/agents/{agent_id}/versions/{version_id}", response_model=AgentVersionResponse)
def edit_agent_version(
    agent_id: str,
    version_id: str,
    payload: AgentVersionUpdate,
    session: SessionDependency,
    context: BuilderContext,
) -> Any:
    return update_agent_version(session, context, agent_id, version_id, payload)


@router.post(
    "/agents/{agent_id}/versions/{version_id}/publish", response_model=AgentVersionResponse
)
def publish_agent(
    agent_id: str,
    version_id: str,
    payload: PublishRequest,
    session: SessionDependency,
    context: BuilderContext,
) -> Any:
    return publish_agent_version(session, context, agent_id, version_id, payload)


@router.get("/workflows", response_model=list[WorkflowResponse])
def workflows(session: SessionDependency, context: ViewerContext) -> Any:
    return list_workflows(session, context)


@router.post("/workflows", response_model=WorkflowResponse, status_code=201)
def add_workflow(
    payload: WorkflowCreate, session: SessionDependency, context: BuilderContext
) -> Any:
    return create_workflow(session, context, payload)


@router.get("/workflows/{workflow_id}", response_model=WorkflowDetailResponse)
def workflow(workflow_id: str, session: SessionDependency, context: ViewerContext) -> Any:
    return get_workflow(session, context, workflow_id)


@router.post(
    "/workflows/{workflow_id}/versions",
    response_model=WorkflowVersionResponse,
    status_code=201,
)
def add_workflow_version(
    workflow_id: str,
    payload: WorkflowVersionCreate,
    session: SessionDependency,
    context: BuilderContext,
) -> Any:
    return create_workflow_version(session, context, workflow_id, payload)


@router.patch(
    "/workflows/{workflow_id}/versions/{version_id}",
    response_model=WorkflowVersionResponse,
)
def edit_workflow_version(
    workflow_id: str,
    version_id: str,
    payload: WorkflowVersionUpdate,
    session: SessionDependency,
    context: BuilderContext,
) -> Any:
    return update_workflow_version(session, context, workflow_id, version_id, payload)


@router.post(
    "/workflows/{workflow_id}/versions/{version_id}/publish",
    response_model=WorkflowVersionResponse,
)
def publish_workflow(
    workflow_id: str,
    version_id: str,
    payload: PublishRequest,
    session: SessionDependency,
    context: BuilderContext,
) -> Any:
    return publish_workflow_version(session, context, workflow_id, version_id, payload)


@router.post(
    "/workflows/{workflow_id}/runs/dry-run", response_model=RunResponse, status_code=201
)
def dry_run(
    workflow_id: str,
    payload: RunDryRunRequest,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
    context: OperatorContext,
) -> Any:
    status, body = create_dry_run(
        session,
        context,
        workflow_id=workflow_id,
        request=payload,
        idempotency_key=idempotency_key,
    )
    return JSONResponse(
        status_code=status,
        content=body,
        headers={"Location": f"/v2/runs/{body['id']}"},
    )


@router.post(
    "/workflows/{workflow_id}/runs/from-mail",
    response_model=RunResponse,
    status_code=201,
)
def from_mail(
    workflow_id: str,
    payload: RunFromMailRequest,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
    context: MailPreparerContext,
) -> Any:
    status, body = create_run_from_mail(
        session,
        context,
        workflow_id=workflow_id,
        request=payload,
        idempotency_key=idempotency_key,
    )
    return JSONResponse(
        status_code=status,
        content=body,
        headers={"Location": f"/v2/runs/{body['id']}"},
    )


@router.get("/runs/{run_id}", response_model=RunResponse)
def run(run_id: str, session: SessionDependency, context: ViewerContext) -> Any:
    return get_run(session, context, run_id)


@router.post("/runs/{run_id}/approve", response_model=RunResponse)
def approve(
    run_id: str,
    payload: RunApprovalRequest,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
    context: ApproverContext,
) -> Any:
    status, body = approve_run(
        session,
        context,
        run_id=run_id,
        request=payload,
        idempotency_key=idempotency_key,
    )
    return JSONResponse(status_code=status, content=body)


@router.post("/runs/{run_id}/reject", response_model=RunResponse)
def reject(
    run_id: str,
    payload: RunRejectionRequest,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
    context: ApproverContext,
) -> Any:
    status, body = reject_run(
        session,
        context,
        run_id=run_id,
        request=payload,
        idempotency_key=idempotency_key,
    )
    return JSONResponse(status_code=status, content=body)


@router.post(
    "/runs/{run_id}/execute", response_model=ExecutionOperationResponse, status_code=202
)
def execute(
    run_id: str,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
    context: ExecutorContext,
) -> Any:
    status, body = execute_run(
        session,
        context,
        run_id=run_id,
        idempotency_key=idempotency_key,
    )
    return JSONResponse(
        status_code=status,
        content=body,
        headers={"Location": f"/v2/execution-operations/{body['operation_id']}"},
    )


@router.get(
    "/execution-operations/{operation_id}", response_model=ExecutionOperationResponse
)
def execution_operation(
    operation_id: str, session: SessionDependency, context: ViewerContext
) -> Any:
    return get_execution_operation(session, context, operation_id)


@router.get("/runs/{run_id}/audit-events", response_model=list[V2AuditEventResponse])
def run_audit(run_id: str, session: SessionDependency, context: ViewerContext) -> Any:
    return list_run_audit(session, context, run_id)
