"""Tenant-scoped catalog application service."""

from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from prontoagente.errors import ConflictError, NotFoundError
from prontoagente.v2.auth import AuthContext
from prontoagente.v2.connectors import validate_connector_action, validate_public_config
from prontoagente.v2.models import Agent, AgentVersion, Workflow, WorkflowVersion
from prontoagente.v2.schemas import (
    AgentCreate,
    AgentDetailResponse,
    AgentResponse,
    AgentVersionCreate,
    AgentVersionResponse,
    AgentVersionUpdate,
    PublishRequest,
    WorkflowCreate,
    WorkflowDetailResponse,
    WorkflowResponse,
    WorkflowVersionCreate,
    WorkflowVersionResponse,
    WorkflowVersionUpdate,
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return None if value is None else _utc(value)


def agent_body(agent: Agent) -> dict[str, Any]:
    return AgentResponse(
        id=agent.id,
        slug=agent.slug,
        name=agent.name,
        description=agent.description,
        created_by=agent.created_by,
        created_at=_utc(agent.created_at),
    ).model_dump(mode="json")


def agent_version_body(version: AgentVersion) -> dict[str, Any]:
    return AgentVersionResponse(
        id=version.id,
        agent_id=version.agent_id,
        version=version.version,
        status=cast(Literal["draft", "published"], version.status),
        definition=version.definition,
        lock_version=version.lock_version,
        created_by=version.created_by,
        created_at=_utc(version.created_at),
        published_at=_optional_utc(version.published_at),
    ).model_dump(mode="json")


def workflow_body(workflow: Workflow) -> dict[str, Any]:
    return WorkflowResponse(
        id=workflow.id,
        slug=workflow.slug,
        name=workflow.name,
        description=workflow.description,
        created_by=workflow.created_by,
        created_at=_utc(workflow.created_at),
    ).model_dump(mode="json")


def workflow_version_body(version: WorkflowVersion) -> dict[str, Any]:
    return WorkflowVersionResponse(
        id=version.id,
        workflow_id=version.workflow_id,
        version=version.version,
        status=cast(Literal["draft", "published"], version.status),
        agent_version_id=version.agent_version_id,
        connector=version.connector,
        action=version.action,
        config=version.config,
        input_schema=version.input_schema,
        approval_required=version.approval_required,
        lock_version=version.lock_version,
        created_by=version.created_by,
        created_at=_utc(version.created_at),
        published_at=_optional_utc(version.published_at),
    ).model_dump(mode="json")


def _agent(session: Session, context: AuthContext, agent_id: str) -> Agent:
    agent = session.scalar(
        select(Agent).where(Agent.tenant_id == context.tenant_id, Agent.id == agent_id)
    )
    if agent is None:
        raise NotFoundError("agent_not_found", "agent not found")
    return agent


def _agent_version(
    session: Session, context: AuthContext, agent_id: str, version_id: str
) -> AgentVersion:
    version = session.scalar(
        select(AgentVersion).where(
            AgentVersion.tenant_id == context.tenant_id,
            AgentVersion.agent_id == agent_id,
            AgentVersion.id == version_id,
        )
    )
    if version is None:
        raise NotFoundError("agent_version_not_found", "agent version not found")
    return version


def _workflow(session: Session, context: AuthContext, workflow_id: str) -> Workflow:
    workflow = session.scalar(
        select(Workflow).where(
            Workflow.tenant_id == context.tenant_id, Workflow.id == workflow_id
        )
    )
    if workflow is None:
        raise NotFoundError("workflow_not_found", "workflow not found")
    return workflow


def _workflow_version(
    session: Session, context: AuthContext, workflow_id: str, version_id: str
) -> WorkflowVersion:
    version = session.scalar(
        select(WorkflowVersion).where(
            WorkflowVersion.tenant_id == context.tenant_id,
            WorkflowVersion.workflow_id == workflow_id,
            WorkflowVersion.id == version_id,
        )
    )
    if version is None:
        raise NotFoundError("workflow_version_not_found", "workflow version not found")
    return version


def list_agents(session: Session, context: AuthContext) -> list[dict[str, Any]]:
    agents = session.scalars(
        select(Agent).where(Agent.tenant_id == context.tenant_id).order_by(Agent.created_at)
    ).all()
    return [agent_body(agent) for agent in agents]


def create_agent(session: Session, context: AuthContext, request: AgentCreate) -> dict[str, Any]:
    agent = Agent(
        id=str(uuid4()),
        tenant_id=context.tenant_id,
        slug=request.slug,
        name=request.name,
        description=request.description,
        created_by=context.principal_id,
    )
    session.add(agent)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ConflictError("agent_slug_exists", "an agent with this slug already exists") from exc
    return agent_body(agent)


def get_agent(session: Session, context: AuthContext, agent_id: str) -> dict[str, Any]:
    agent = _agent(session, context, agent_id)
    versions = session.scalars(
        select(AgentVersion)
        .where(AgentVersion.tenant_id == context.tenant_id, AgentVersion.agent_id == agent.id)
        .order_by(AgentVersion.version)
    ).all()
    return AgentDetailResponse(
        **agent_body(agent),
        versions=[AgentVersionResponse(**agent_version_body(item)) for item in versions],
    ).model_dump(mode="json")


def create_agent_version(
    session: Session, context: AuthContext, agent_id: str, request: AgentVersionCreate
) -> dict[str, Any]:
    _agent(session, context, agent_id)
    current = session.scalar(
        select(func.max(AgentVersion.version)).where(
            AgentVersion.tenant_id == context.tenant_id, AgentVersion.agent_id == agent_id
        )
    )
    version = AgentVersion(
        id=str(uuid4()),
        tenant_id=context.tenant_id,
        agent_id=agent_id,
        version=int(current or 0) + 1,
        status="draft",
        definition=request.definition,
        created_by=context.principal_id,
    )
    session.add(version)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ConflictError(
            "version_conflict", "agent version number was claimed concurrently"
        ) from exc
    return agent_version_body(version)


def update_agent_version(
    session: Session,
    context: AuthContext,
    agent_id: str,
    version_id: str,
    request: AgentVersionUpdate,
) -> dict[str, Any]:
    version = _agent_version(session, context, agent_id, version_id)
    if version.status != "draft":
        raise ConflictError("published_version_immutable", "published versions are immutable")
    if version.lock_version != request.lock_version:
        raise ConflictError("optimistic_lock_conflict", "agent version changed; reload and retry")
    version.definition = request.definition
    session.commit()
    return agent_version_body(version)


def publish_agent_version(
    session: Session,
    context: AuthContext,
    agent_id: str,
    version_id: str,
    request: PublishRequest,
) -> dict[str, Any]:
    version = _agent_version(session, context, agent_id, version_id)
    if version.status != "draft":
        raise ConflictError("published_version_immutable", "published versions are immutable")
    if version.lock_version != request.lock_version:
        raise ConflictError("optimistic_lock_conflict", "agent version changed; reload and retry")
    version.status = "published"
    version.published_at = datetime.now(UTC)
    session.commit()
    return agent_version_body(version)


def list_workflows(session: Session, context: AuthContext) -> list[dict[str, Any]]:
    workflows = session.scalars(
        select(Workflow)
        .where(Workflow.tenant_id == context.tenant_id)
        .order_by(Workflow.created_at)
    ).all()
    return [workflow_body(workflow) for workflow in workflows]


def create_workflow(
    session: Session, context: AuthContext, request: WorkflowCreate
) -> dict[str, Any]:
    workflow = Workflow(
        id=str(uuid4()),
        tenant_id=context.tenant_id,
        slug=request.slug,
        name=request.name,
        description=request.description,
        created_by=context.principal_id,
    )
    session.add(workflow)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ConflictError(
            "workflow_slug_exists", "a workflow with this slug already exists"
        ) from exc
    return workflow_body(workflow)


def get_workflow(session: Session, context: AuthContext, workflow_id: str) -> dict[str, Any]:
    workflow = _workflow(session, context, workflow_id)
    versions = session.scalars(
        select(WorkflowVersion)
        .where(
            WorkflowVersion.tenant_id == context.tenant_id,
            WorkflowVersion.workflow_id == workflow.id,
        )
        .order_by(WorkflowVersion.version)
    ).all()
    return WorkflowDetailResponse(
        **workflow_body(workflow),
        versions=[WorkflowVersionResponse(**workflow_version_body(item)) for item in versions],
    ).model_dump(mode="json")


def _validate_workflow_version(
    session: Session, context: AuthContext, request: WorkflowVersionCreate
) -> None:
    validate_connector_action(request.connector, request.action)
    validate_public_config(request.connector, request.config)
    agent_version = session.scalar(
        select(AgentVersion).where(
            AgentVersion.tenant_id == context.tenant_id,
            AgentVersion.id == request.agent_version_id,
            AgentVersion.status == "published",
        )
    )
    if agent_version is None:
        raise NotFoundError(
            "published_agent_version_not_found", "published agent version not found"
        )


def create_workflow_version(
    session: Session,
    context: AuthContext,
    workflow_id: str,
    request: WorkflowVersionCreate,
) -> dict[str, Any]:
    _workflow(session, context, workflow_id)
    try:
        _validate_workflow_version(session, context, request)
    except ValueError as exc:
        raise ConflictError("invalid_connector_configuration", str(exc)) from exc
    current = session.scalar(
        select(func.max(WorkflowVersion.version)).where(
            WorkflowVersion.tenant_id == context.tenant_id,
            WorkflowVersion.workflow_id == workflow_id,
        )
    )
    version = WorkflowVersion(
        id=str(uuid4()),
        tenant_id=context.tenant_id,
        workflow_id=workflow_id,
        version=int(current or 0) + 1,
        status="draft",
        agent_version_id=request.agent_version_id,
        connector=request.connector,
        action=request.action,
        config=request.config,
        input_schema=request.input_schema,
        approval_required=request.approval_required,
        created_by=context.principal_id,
    )
    session.add(version)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ConflictError(
            "version_conflict", "workflow version number was claimed concurrently"
        ) from exc
    return workflow_version_body(version)


def update_workflow_version(
    session: Session,
    context: AuthContext,
    workflow_id: str,
    version_id: str,
    request: WorkflowVersionUpdate,
) -> dict[str, Any]:
    version = _workflow_version(session, context, workflow_id, version_id)
    if version.status != "draft":
        raise ConflictError("published_version_immutable", "published versions are immutable")
    if version.lock_version != request.lock_version:
        raise ConflictError(
            "optimistic_lock_conflict", "workflow version changed; reload and retry"
        )
    try:
        _validate_workflow_version(session, context, request)
    except ValueError as exc:
        raise ConflictError("invalid_connector_configuration", str(exc)) from exc
    version.agent_version_id = request.agent_version_id
    version.connector = request.connector
    version.action = request.action
    version.config = request.config
    version.input_schema = request.input_schema
    version.approval_required = request.approval_required
    session.commit()
    return workflow_version_body(version)


def publish_workflow_version(
    session: Session,
    context: AuthContext,
    workflow_id: str,
    version_id: str,
    request: PublishRequest,
) -> dict[str, Any]:
    version = _workflow_version(session, context, workflow_id, version_id)
    if version.status != "draft":
        raise ConflictError("published_version_immutable", "published versions are immutable")
    if version.lock_version != request.lock_version:
        raise ConflictError(
            "optimistic_lock_conflict", "workflow version changed; reload and retry"
        )
    version.status = "published"
    version.published_at = datetime.now(UTC)
    session.commit()
    return workflow_version_body(version)
