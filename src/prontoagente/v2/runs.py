"""Run lifecycle, tenant-scoped idempotency, and outbox creation."""

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from prontoagente.canonical import sha256_digest
from prontoagente.config import Settings, get_settings
from prontoagente.errors import ConflictError, NotFoundError
from prontoagente.v2.auth import AuthContext
from prontoagente.v2.connectors import m365_configured
from prontoagente.v2.demo_connectors import (
    CATALOG_VERSION,
    PARSER_VERSION,
    canonical_order,
    parse_order_subject,
    reconcile_order,
    source_reference_hash,
)
from prontoagente.v2.models import (
    AgentVersion,
    ExecutionOperation,
    OrderSourceClaim,
    OutboxEvent,
    Run,
    V2AuditEvent,
    V2IdempotencyRecord,
    Workflow,
    WorkflowVersion,
)
from prontoagente.v2.schemas import (
    ExecutionOperationResponse,
    RunApprovalRequest,
    RunDryRunRequest,
    RunFromMailRequest,
    RunRejectionRequest,
    RunResponse,
    V2AuditEventResponse,
)

ResponseBody = dict[str, Any]


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return None if value is None else _utc(value)


def run_body(run: Run) -> ResponseBody:
    return RunResponse(
        id=run.id,
        workflow_id=run.workflow_id,
        workflow_version_id=run.workflow_version_id,
        agent_version_id=run.agent_version_id,
        status=run.status,
        input=run.input_payload,
        proposal=run.proposal,
        proposal_hash=run.proposal_hash,
        summary=run.summary,
        target=run.target,
        payload=run.payload,
        approval_required=bool(run.workflow_snapshot["approval_required"]),
        approval_eligible=run.approval_eligible,
        approved_by=run.approved_by,
        approved_at=_optional_utc(run.approved_at),
        rejected_by=run.rejected_by,
        rejected_at=_optional_utc(run.rejected_at),
        rejection_reason=run.rejection_reason,
        created_at=_utc(run.created_at),
        updated_at=_utc(run.updated_at),
    ).model_dump(mode="json")


def operation_body(operation: ExecutionOperation) -> ResponseBody:
    return ExecutionOperationResponse(
        operation_id=operation.id,
        run_id=operation.run_id,
        status=operation.status,
        connector=operation.connector,
        action=operation.action,
        result=operation.result,
        error_code=operation.error_code,
        error_message=operation.error_message,
        created_at=_utc(operation.created_at),
        completed_at=_optional_utc(operation.completed_at),
    ).model_dump(mode="json")


def _run(session: Session, context: AuthContext, run_id: str) -> Run:
    run = session.scalar(
        select(Run).where(Run.tenant_id == context.tenant_id, Run.id == run_id)
    )
    if run is None:
        raise NotFoundError("run_not_found", "run not found")
    return run


def _cas_run_status(
    session: Session,
    context: AuthContext,
    *,
    run_id: str,
    expected_status: str,
    values: dict[str, Any],
    require_approval_eligible: bool = False,
) -> bool:
    """Change state only if this request still owns the expected transition."""

    predicates = [
        Run.tenant_id == context.tenant_id,
        Run.id == run_id,
        Run.status == expected_status,
    ]
    if require_approval_eligible:
        predicates.append(Run.approval_eligible.is_(True))
    result = cast(
        CursorResult[Any],
        session.execute(
            update(Run)
            .where(*predicates)
            .values(**values, updated_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        ),
    )
    return result.rowcount == 1


def _assert_integrity(run: Run) -> None:
    if run.proposal_hash != sha256_digest(run.proposal):
        raise ConflictError(
            "proposal_integrity_violation", "persisted run proposal does not match its hash"
        )


def _request_hash(value: dict[str, Any]) -> str:
    return sha256_digest(value)


def _find_replay(
    session: Session,
    context: AuthContext,
    *,
    operation: str,
    idempotency_key: str,
    request_hash: str,
) -> tuple[int, ResponseBody] | None:
    record = session.scalar(
        select(V2IdempotencyRecord).where(
            V2IdempotencyRecord.tenant_id == context.tenant_id,
            V2IdempotencyRecord.operation == operation,
            V2IdempotencyRecord.idempotency_key == idempotency_key,
        )
    )
    if record is None:
        return None
    if record.actor_id != context.principal_id or record.request_hash != request_hash:
        raise ConflictError(
            "idempotency_key_reused",
            "Idempotency-Key was already used with a different actor or request payload",
        )
    return record.response_status, record.response_body


def _remember(
    session: Session,
    context: AuthContext,
    *,
    operation: str,
    idempotency_key: str,
    request_hash: str,
    status: int,
    body: ResponseBody,
    resource_id: str,
) -> None:
    session.add(
        V2IdempotencyRecord(
            tenant_id=context.tenant_id,
            operation=operation,
            idempotency_key=idempotency_key,
            actor_id=context.principal_id,
            request_hash=request_hash,
            response_status=status,
            response_body=body,
            resource_id=resource_id,
        )
    )


def _commit_idempotent(
    session: Session,
    context: AuthContext,
    *,
    operation: str,
    idempotency_key: str,
    request_hash: str,
    status: int,
    body: ResponseBody,
    resource_id: str,
) -> tuple[int, ResponseBody]:
    _remember(
        session,
        context,
        operation=operation,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        status=status,
        body=body,
        resource_id=resource_id,
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        replay = _find_replay(
            session,
            context,
            operation=operation,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is None:
            raise
        return replay
    return status, body


def _audit(
    session: Session,
    context: AuthContext,
    *,
    run_id: str,
    event_type: str,
    idempotency_key: str,
    payload: dict[str, Any],
) -> None:
    session.add(
        V2AuditEvent(
            tenant_id=context.tenant_id,
            run_id=run_id,
            entity_type="run",
            entity_id=run_id,
            event_type=event_type,
            actor_id=context.principal_id,
            idempotency_key=idempotency_key,
            payload=payload,
        )
    )


def _validate_input_schema(schema: dict[str, Any], value: dict[str, Any]) -> None:
    required = schema.get("required", [])
    if isinstance(required, list):
        missing = [item for item in required if isinstance(item, str) and item not in value]
        if missing:
            raise ConflictError("input_schema_violation", f"missing required input: {missing[0]}")


def _proposal_for(
    workflow_version: WorkflowVersion, request_input: dict[str, Any]
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    if workflow_version.connector == "simulated_erp":
        target = str(workflow_version.config.get("target", "simulated_erp"))
        summary = str(request_input.get("summary", "Create ERP sales order"))[:500]
        payload = dict(request_input)
    else:
        raw_top = request_input.get("top", workflow_version.config.get("top", 25))
        if not isinstance(raw_top, int) or isinstance(raw_top, bool) or not 1 <= raw_top <= 50:
            raise ConflictError("invalid_input", "top must be an integer between 1 and 50")
        target = "configured-mailbox"
        summary = "Read basic message metadata from the configured M365 folder"
        payload = {"top": raw_top}
    proposal = {
        "schema_version": "2.0",
        "workflow_version_id": workflow_version.id,
        "agent_version_id": workflow_version.agent_version_id,
        "connector": workflow_version.connector,
        "action": workflow_version.action,
        "summary": summary,
        "target": target,
        "payload": payload,
    }
    return summary, target, payload, proposal


def _published_stack(
    session: Session, context: AuthContext, workflow_id: str
) -> tuple[Workflow, WorkflowVersion, AgentVersion]:
    workflow = session.scalar(
        select(Workflow).where(
            Workflow.tenant_id == context.tenant_id, Workflow.id == workflow_id
        )
    )
    if workflow is None:
        raise NotFoundError("workflow_not_found", "workflow not found")
    workflow_version = session.scalar(
        select(WorkflowVersion)
        .where(
            WorkflowVersion.tenant_id == context.tenant_id,
            WorkflowVersion.workflow_id == workflow_id,
            WorkflowVersion.status == "published",
        )
        .order_by(WorkflowVersion.version.desc())
        .limit(1)
    )
    if workflow_version is None:
        raise ConflictError("workflow_not_published", "workflow has no published version")
    agent_version = session.scalar(
        select(AgentVersion).where(
            AgentVersion.tenant_id == context.tenant_id,
            AgentVersion.id == workflow_version.agent_version_id,
            AgentVersion.status == "published",
        )
    )
    if agent_version is None:
        raise ConflictError("agent_version_unavailable", "pinned agent version is unavailable")
    return workflow, workflow_version, agent_version


def _new_proposed_run(
    context: AuthContext,
    *,
    workflow: Workflow,
    workflow_version: WorkflowVersion,
    agent_version: AgentVersion,
    input_payload: dict[str, Any],
    proposal: dict[str, Any],
    summary: str,
    target: str,
    payload: dict[str, Any],
    approval_eligible: bool = True,
) -> Run:
    return Run(
        id=str(uuid4()),
        tenant_id=context.tenant_id,
        workflow_id=workflow.id,
        workflow_version_id=workflow_version.id,
        agent_version_id=agent_version.id,
        workflow_snapshot={
            "version": workflow_version.version,
            "connector": workflow_version.connector,
            "action": workflow_version.action,
            "config": workflow_version.config,
            "input_schema": workflow_version.input_schema,
            "approval_required": workflow_version.approval_required,
        },
        agent_snapshot={
            "version": agent_version.version,
            "definition": agent_version.definition,
        },
        input_payload=input_payload,
        proposal=proposal,
        proposal_hash=sha256_digest(proposal),
        summary=summary,
        target=target,
        payload=payload,
        status="proposed",
        approval_eligible=approval_eligible,
        created_by=context.principal_id,
    )


def _stage_proposed_run(
    session: Session,
    context: AuthContext,
    *,
    run: Run,
    workflow_version: WorkflowVersion,
    idempotency_key: str,
    audit_details: dict[str, Any] | None = None,
) -> ResponseBody:
    session.add(run)
    session.flush()
    audit_payload: dict[str, Any] = {
        "proposal_hash": run.proposal_hash,
        "workflow_version_id": workflow_version.id,
    }
    if audit_details is not None:
        audit_payload.update(audit_details)
    _audit(
        session,
        context,
        run_id=run.id,
        event_type="run_proposed",
        idempotency_key=idempotency_key,
        payload=audit_payload,
    )
    return run_body(run)


def create_dry_run(
    session: Session,
    context: AuthContext,
    *,
    workflow_id: str,
    request: RunDryRunRequest,
    idempotency_key: str,
) -> tuple[int, ResponseBody]:
    operation = "run.dry_run"
    request_hash = _request_hash(
        {"workflow_id": workflow_id, **request.model_dump(mode="json")}
    )
    replay = _find_replay(
        session,
        context,
        operation=operation,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    if replay is not None:
        return replay
    workflow, workflow_version, agent_version = _published_stack(
        session, context, workflow_id
    )
    _validate_input_schema(workflow_version.input_schema, request.input)
    summary, target, payload, proposal = _proposal_for(workflow_version, request.input)
    run = _new_proposed_run(
        context,
        workflow=workflow,
        workflow_version=workflow_version,
        agent_version=agent_version,
        input_payload=request.input,
        proposal=proposal,
        summary=summary,
        target=target,
        payload=payload,
    )
    body = _stage_proposed_run(
        session,
        context,
        run=run,
        workflow_version=workflow_version,
        idempotency_key=idempotency_key,
    )
    return _commit_idempotent(
        session,
        context,
        operation=operation,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        status=201,
        body=body,
        resource_id=run.id,
    )


def create_run_from_mail(
    session: Session,
    context: AuthContext,
    *,
    workflow_id: str,
    request: RunFromMailRequest,
    idempotency_key: str,
    settings: Settings | None = None,
) -> tuple[int, ResponseBody]:
    """Prepare a normal governed run from a strictly synthetic mail envelope."""

    resolved_settings = settings or get_settings()
    if (
        not resolved_settings.enable_demo_connectors
        or resolved_settings.app_env == "production"
    ):
        raise ConflictError(
            "demo_connector_disabled",
            "offline demo preparation is disabled in this environment",
        )

    operation = "run.from_mail"
    request_hash = _request_hash(
        {"workflow_id": workflow_id, **request.model_dump(mode="json")}
    )
    replay = _find_replay(
        session,
        context,
        operation=operation,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    if replay is not None:
        return replay

    envelope = request.envelope
    source_ref_hash = source_reference_hash(
        source_connector=envelope.source_connector,
        message_id=envelope.message_id,
        internet_message_id=envelope.internet_message_id,
    )
    existing_claim = session.scalar(
        select(OrderSourceClaim).where(
            OrderSourceClaim.tenant_id == context.tenant_id,
            OrderSourceClaim.source_connector == envelope.source_connector,
            OrderSourceClaim.source_ref_hash == source_ref_hash,
        )
    )
    if existing_claim is not None:
        raise ConflictError(
            "order_source_already_claimed",
            "this source message already prepared an order run",
        )

    workflow, workflow_version, agent_version = _published_stack(
        session, context, workflow_id
    )
    if (
        workflow_version.connector != "simulated_erp"
        or workflow_version.action != "create_sales_order"
        or not workflow_version.approval_required
    ):
        raise ConflictError(
            "demo_workflow_incompatible",
            "from-mail requires an approval-first simulated_erp/create_sales_order workflow",
        )

    parsed_order = parse_order_subject(envelope.subject)
    order_payload = canonical_order(parsed_order)
    _validate_input_schema(workflow_version.input_schema, order_payload)
    reconciliation = reconcile_order(parsed_order)
    provenance = {
        "source_connector": envelope.source_connector,
        "source_ref_hash": source_ref_hash,
        "parser_version": PARSER_VERSION,
        "catalog_version": CATALOG_VERSION,
    }
    summary = (
        f"Prepare order {parsed_order.order_id}; "
        f"offline reconciliation {reconciliation['outcome']}"
    )
    target = str(workflow_version.config.get("target", "simulated_erp"))
    proposal = {
        "schema_version": "2.0",
        "preparation_type": "demo_mail_order_reconciliation",
        "workflow_version_id": workflow_version.id,
        "agent_version_id": workflow_version.agent_version_id,
        "connector": workflow_version.connector,
        "action": workflow_version.action,
        "summary": summary,
        "target": target,
        "payload": order_payload,
        "normalized_order": order_payload,
        "reconciliation": reconciliation,
        "provenance": provenance,
    }
    run = _new_proposed_run(
        context,
        workflow=workflow,
        workflow_version=workflow_version,
        agent_version=agent_version,
        input_payload={
            "normalized_order": order_payload,
            "reconciliation": reconciliation,
            "provenance": provenance,
        },
        proposal=proposal,
        summary=summary,
        target=target,
        payload=order_payload,
        approval_eligible=reconciliation["outcome"] == "MATCHED",
    )
    body = _stage_proposed_run(
        session,
        context,
        run=run,
        workflow_version=workflow_version,
        idempotency_key=idempotency_key,
        audit_details={
            "preparation_type": "demo_mail_order_reconciliation",
            "source": provenance,
            "reconciliation_outcome": reconciliation["outcome"],
        },
    )
    session.add(
        OrderSourceClaim(
            id=str(uuid4()),
            tenant_id=context.tenant_id,
            source_connector=envelope.source_connector,
            source_ref_hash=source_ref_hash,
            run_id=run.id,
        )
    )
    _remember(
        session,
        context,
        operation=operation,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        status=201,
        body=body,
        resource_id=run.id,
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        replay = _find_replay(
            session,
            context,
            operation=operation,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        claimed = session.scalar(
            select(OrderSourceClaim).where(
                OrderSourceClaim.tenant_id == context.tenant_id,
                OrderSourceClaim.source_connector == envelope.source_connector,
                OrderSourceClaim.source_ref_hash == source_ref_hash,
            )
        )
        if claimed is not None:
            raise ConflictError(
                "order_source_already_claimed",
                "this source message already prepared an order run",
            ) from exc
        raise
    return 201, body


def approve_run(
    session: Session,
    context: AuthContext,
    *,
    run_id: str,
    request: RunApprovalRequest,
    idempotency_key: str,
) -> tuple[int, ResponseBody]:
    operation = "run.approve"
    request_hash = _request_hash({"run_id": run_id, **request.model_dump(mode="json")})
    replay = _find_replay(
        session,
        context,
        operation=operation,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    if replay is not None:
        return replay
    run = _run(session, context, run_id)
    _assert_integrity(run)
    if run.status != "proposed":
        raise ConflictError("invalid_state", "approval requires a proposed run")
    if not run.approval_eligible:
        raise ConflictError(
            "approval_not_eligible",
            "run contains reconciliation blockers and cannot be approved",
        )
    if run.proposal_hash != request.proposal_hash:
        raise ConflictError("proposal_hash_mismatch", "proposal hash does not match")
    if not _cas_run_status(
        session,
        context,
        run_id=run.id,
        expected_status="proposed",
        values={
            "status": "approved",
            "approved_by": context.principal_id,
            "approved_at": datetime.now(UTC),
        },
        require_approval_eligible=True,
    ):
        session.rollback()
        replay = _find_replay(
            session,
            context,
            operation=operation,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        raise ConflictError("invalid_state", "approval requires a proposed run")
    session.refresh(run)
    _audit(
        session,
        context,
        run_id=run.id,
        event_type="run_approved",
        idempotency_key=idempotency_key,
        payload={"proposal_hash": run.proposal_hash},
    )
    body = run_body(run)
    return _commit_idempotent(
        session,
        context,
        operation=operation,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        status=200,
        body=body,
        resource_id=run.id,
    )


def reject_run(
    session: Session,
    context: AuthContext,
    *,
    run_id: str,
    request: RunRejectionRequest,
    idempotency_key: str,
) -> tuple[int, ResponseBody]:
    operation = "run.reject"
    request_hash = _request_hash({"run_id": run_id, **request.model_dump(mode="json")})
    replay = _find_replay(
        session,
        context,
        operation=operation,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    if replay is not None:
        return replay
    run = _run(session, context, run_id)
    _assert_integrity(run)
    if run.status != "proposed":
        raise ConflictError("invalid_state", "rejection requires a proposed run")
    if not _cas_run_status(
        session,
        context,
        run_id=run.id,
        expected_status="proposed",
        values={
            "status": "rejected",
            "rejected_by": context.principal_id,
            "rejected_at": datetime.now(UTC),
            "rejection_reason": request.reason,
        },
    ):
        session.rollback()
        replay = _find_replay(
            session,
            context,
            operation=operation,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        raise ConflictError("invalid_state", "rejection requires a proposed run")
    session.refresh(run)
    _audit(
        session,
        context,
        run_id=run.id,
        event_type="run_rejected",
        idempotency_key=idempotency_key,
        payload={"reason": request.reason},
    )
    body = run_body(run)
    return _commit_idempotent(
        session,
        context,
        operation=operation,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        status=200,
        body=body,
        resource_id=run.id,
    )


def execute_run(
    session: Session,
    context: AuthContext,
    *,
    run_id: str,
    idempotency_key: str,
    settings: Settings | None = None,
) -> tuple[int, ResponseBody]:
    operation_name = "run.execute"
    request_hash = _request_hash({"run_id": run_id})
    replay = _find_replay(
        session,
        context,
        operation=operation_name,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    if replay is not None:
        return replay
    run = _run(session, context, run_id)
    _assert_integrity(run)
    if run.status != "approved":
        code = "approval_required" if run.status == "proposed" else "invalid_state"
        raise ConflictError(code, "run is not executable in its current state")
    connector_name = str(run.workflow_snapshot["connector"])
    if connector_name == "m365_mail_intake_v1" and not m365_configured(
        context.tenant_id, settings or get_settings()
    ):
        raise ConflictError(
            "connector_not_configured", "m365_mail_intake_v1 is disabled or incomplete"
        )
    existing = session.scalar(
        select(ExecutionOperation).where(
            ExecutionOperation.tenant_id == context.tenant_id,
            ExecutionOperation.run_id == run.id,
        )
    )
    if existing is not None:
        raise ConflictError("execution_already_claimed", "run already has an execution operation")
    if not _cas_run_status(
        session,
        context,
        run_id=run.id,
        expected_status="approved",
        values={"status": "execution_pending"},
    ):
        session.rollback()
        replay = _find_replay(
            session,
            context,
            operation=operation_name,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        raise ConflictError("invalid_state", "run is not executable in its current state")
    session.refresh(run)
    execution = ExecutionOperation(
        id=str(uuid4()),
        tenant_id=context.tenant_id,
        run_id=run.id,
        status="pending",
        connector=connector_name,
        action=str(run.workflow_snapshot["action"]),
    )
    outbox = OutboxEvent(
        id=str(uuid4()),
        tenant_id=context.tenant_id,
        operation_id=execution.id,
        event_type="execute_run",
        status="pending",
    )
    session.add_all((execution, outbox))
    session.flush()
    _audit(
        session,
        context,
        run_id=run.id,
        event_type="execution_requested",
        idempotency_key=idempotency_key,
        payload={"operation_id": execution.id, "connector": execution.connector},
    )
    body = operation_body(execution)
    return _commit_idempotent(
        session,
        context,
        operation=operation_name,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        status=202,
        body=body,
        resource_id=execution.id,
    )


def get_run(session: Session, context: AuthContext, run_id: str) -> ResponseBody:
    return run_body(_run(session, context, run_id))


def get_execution_operation(
    session: Session, context: AuthContext, operation_id: str
) -> ResponseBody:
    operation = session.scalar(
        select(ExecutionOperation).where(
            ExecutionOperation.tenant_id == context.tenant_id,
            ExecutionOperation.id == operation_id,
        )
    )
    if operation is None:
        raise NotFoundError("execution_operation_not_found", "execution operation not found")
    return operation_body(operation)


def list_run_audit(
    session: Session, context: AuthContext, run_id: str
) -> list[dict[str, Any]]:
    _run(session, context, run_id)
    events = session.scalars(
        select(V2AuditEvent)
        .where(V2AuditEvent.tenant_id == context.tenant_id, V2AuditEvent.run_id == run_id)
        .order_by(V2AuditEvent.id)
    ).all()
    return [
        V2AuditEventResponse(
            id=event.id,
            run_id=event.run_id,
            entity_type=event.entity_type,
            entity_id=event.entity_id,
            event_type=event.event_type,
            actor_id=event.actor_id,
            idempotency_key=event.idempotency_key,
            payload=event.payload,
            created_at=_utc(event.created_at),
        ).model_dump(mode="json")
        for event in events
    ]
