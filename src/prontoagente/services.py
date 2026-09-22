"""Transactional application service for the ERP draft workflow."""

import hmac
from copy import deepcopy
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from prontoagente.canonical import sha256_digest
from prontoagente.connectors import SimulatedConnector
from prontoagente.errors import ConflictError, NotFoundError
from prontoagente.models import (
    AuditEvent,
    ErpDraftWorkflow,
    IdempotencyRecord,
    SimulationOperation,
)
from prontoagente.schemas import (
    ActionType,
    ApprovalRequest,
    AuditEventResponse,
    AuditEventType,
    DryRunRequest,
    RejectionRequest,
    SimulationOperationResponse,
    SimulationOperationStatus,
    WorkflowResponse,
    WorkflowStatus,
)

ResponseBody = dict[str, Any]


def _as_utc(value: datetime) -> datetime:
    """Normalize SQLite's timezone-naive datetimes to explicit UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _as_optional_utc(value: datetime | None) -> datetime | None:
    return None if value is None else _as_utc(value)


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")


def build_proposal(request: DryRunRequest) -> dict[str, Any]:
    """Transform validated order data into a stable, connector-neutral ERP proposal."""

    subtotal = Decimal("0")
    lines: list[dict[str, Any]] = []
    for line_number, line in enumerate(request.order.lines, start=1):
        line_total = line.unit_price * line.quantity
        subtotal += line_total
        lines.append(
            {
                "line_number": line_number,
                "sku": line.sku,
                "quantity": line.quantity,
                "unit_price": _money(line.unit_price),
                "line_total": _money(line_total),
            }
        )

    return {
        "schema_version": "1.0",
        "action_type": request.action_type.value,
        "summary": (
            f"Create sales order {request.order.external_order_id} "
            f"for customer {request.order.customer_id}"
        ),
        "target": {
            "connector": SimulatedConnector.name,
            "resource": "sales_order",
        },
        "document_type": "sales_order",
        "source": {"external_order_id": request.order.external_order_id},
        "customer": {"external_id": request.order.customer_id},
        "currency": request.order.currency,
        "lines": lines,
        "totals": {"net_amount": _money(subtotal)},
        "execution": {"connector": SimulatedConnector.name, "mode": "simulation_only"},
    }


def _workflow_body(workflow: ErpDraftWorkflow) -> ResponseBody:
    response = WorkflowResponse(
        id=workflow.id,
        action_type=ActionType(workflow.action_type),
        status=WorkflowStatus(workflow.status),
        order=workflow.order_payload,
        proposal=workflow.proposal,
        proposal_hash=workflow.proposal_hash,
        approved_by=workflow.approved_by,
        approved_at=_as_optional_utc(workflow.approved_at),
        rejected_by=workflow.rejected_by,
        rejected_at=_as_optional_utc(workflow.rejected_at),
        rejection_reason=workflow.rejection_reason,
        simulation_result=workflow.simulation_result,
        created_at=_as_utc(workflow.created_at),
        updated_at=_as_utc(workflow.updated_at),
    )
    return response.model_dump(mode="json")


def _simulation_operation_body(operation: SimulationOperation) -> ResponseBody:
    response = SimulationOperationResponse(
        operation_id=operation.id,
        workflow_id=operation.workflow_id,
        status=SimulationOperationStatus(operation.status),
        proposal_hash=operation.proposal_hash,
        connector=operation.connector,
        requested_by=operation.requested_by,
        result=operation.result,
        error_code=operation.error_code,
        error_message=operation.error_message,
        created_at=_as_utc(operation.created_at),
        completed_at=_as_optional_utc(operation.completed_at),
    )
    return response.model_dump(mode="json")


def _assert_proposal_integrity(workflow: ErpDraftWorkflow) -> None:
    calculated_hash = sha256_digest(workflow.proposal)
    if not hmac.compare_digest(calculated_hash, workflow.proposal_hash):
        raise ConflictError(
            "proposal_integrity_violation",
            "the persisted proposal does not match its immutable hash",
        )


def _request_hash(value: dict[str, Any]) -> str:
    return sha256_digest(value)


def _find_replay(
    session: Session,
    *,
    operation: str,
    idempotency_key: str,
    actor_id: str,
    request_hash: str,
) -> tuple[int, ResponseBody] | None:
    record = session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.operation == operation,
            IdempotencyRecord.idempotency_key == idempotency_key,
        )
    )
    if record is None:
        return None
    if record.actor_id != actor_id or record.request_hash != request_hash:
        raise ConflictError(
            "idempotency_key_reused",
            "Idempotency-Key was already used with a different actor or request payload",
        )
    return record.response_status, record.response_body


def _remember_response(
    session: Session,
    *,
    operation: str,
    idempotency_key: str,
    actor_id: str,
    request_hash: str,
    response_status: int,
    response_body: ResponseBody,
    resource_id: str,
) -> None:
    session.add(
        IdempotencyRecord(
            operation=operation,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            request_hash=request_hash,
            response_status=response_status,
            response_body=response_body,
            resource_id=resource_id,
        )
    )


def _commit_response(
    session: Session,
    *,
    operation: str,
    idempotency_key: str,
    actor_id: str,
    request_hash: str,
    response_status: int,
    response_body: ResponseBody,
    resource_id: str,
) -> tuple[int, ResponseBody]:
    """Commit atomically, resolving a concurrent idempotent winner as a replay."""

    _remember_response(
        session,
        operation=operation,
        idempotency_key=idempotency_key,
        actor_id=actor_id,
        request_hash=request_hash,
        response_status=response_status,
        response_body=response_body,
        resource_id=resource_id,
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        replay = _find_replay(
            session,
            operation=operation,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            request_hash=request_hash,
        )
        if replay is None:
            raise
        return replay
    return response_status, response_body


def _flush_transition(session: Session) -> None:
    """Turn optimistic-lock failures into an explicit API conflict."""

    try:
        session.flush()
    except StaleDataError as exc:
        session.rollback()
        raise ConflictError(
            "concurrent_modification",
            "the workflow changed concurrently; read its current state and retry",
        ) from exc


def _get_workflow(session: Session, workflow_id: str) -> ErpDraftWorkflow:
    workflow = session.get(ErpDraftWorkflow, workflow_id)
    if workflow is None:
        raise NotFoundError("workflow_not_found", f"ERP draft workflow {workflow_id!r} not found")
    return workflow


def create_dry_run(
    session: Session,
    *,
    request: DryRunRequest,
    actor_id: str,
    idempotency_key: str,
) -> tuple[int, ResponseBody]:
    """Persist a deterministic proposal without constructing or invoking a connector."""

    operation = "erp_draft.dry_run"
    request_value = request.model_dump(mode="json")
    request_hash = _request_hash(request_value)
    replay = _find_replay(
        session,
        operation=operation,
        idempotency_key=idempotency_key,
        actor_id=actor_id,
        request_hash=request_hash,
    )
    if replay is not None:
        return replay

    proposal = build_proposal(request)
    workflow = ErpDraftWorkflow(
        id=str(uuid4()),
        action_type=request.action_type.value,
        status=WorkflowStatus.PROPOSED.value,
        order_payload=request.order.model_dump(mode="json"),
        proposal=proposal,
        proposal_hash=sha256_digest(proposal),
    )
    session.add(workflow)
    session.flush()
    session.add(
        AuditEvent(
            workflow_id=workflow.id,
            event_type=AuditEventType.DRAFT_PROPOSED.value,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            payload={
                "action_type": request.action_type.value,
                "proposal_hash": workflow.proposal_hash,
                "to_status": WorkflowStatus.PROPOSED.value,
            },
        )
    )
    body = _workflow_body(workflow)
    return _commit_response(
        session,
        operation=operation,
        idempotency_key=idempotency_key,
        actor_id=actor_id,
        request_hash=request_hash,
        response_status=201,
        response_body=body,
        resource_id=workflow.id,
    )


def approve_proposal(
    session: Session,
    *,
    workflow_id: str,
    request: ApprovalRequest,
    actor_id: str,
    idempotency_key: str,
) -> tuple[int, ResponseBody]:
    """Approve exactly the proposal hash reviewed by the caller."""

    operation = "erp_draft.approve"
    request_value = {"workflow_id": workflow_id, **request.model_dump(mode="json")}
    request_hash = _request_hash(request_value)
    replay = _find_replay(
        session,
        operation=operation,
        idempotency_key=idempotency_key,
        actor_id=actor_id,
        request_hash=request_hash,
    )
    if replay is not None:
        return replay

    workflow = _get_workflow(session, workflow_id)
    _assert_proposal_integrity(workflow)
    if workflow.status != WorkflowStatus.PROPOSED.value:
        raise ConflictError(
            "invalid_state",
            f"approval requires status 'proposed'; current status is {workflow.status!r}",
        )
    if workflow.proposal_hash != request.proposal_hash:
        raise ConflictError(
            "proposal_hash_mismatch",
            "the supplied proposal_hash does not match the persisted proposal",
        )

    now = datetime.now(UTC)
    workflow.status = WorkflowStatus.APPROVED.value
    workflow.approved_by = actor_id
    workflow.approved_at = now
    _flush_transition(session)
    session.add(
        AuditEvent(
            workflow_id=workflow.id,
            event_type=AuditEventType.PROPOSAL_APPROVED.value,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            payload={
                "from_status": WorkflowStatus.PROPOSED.value,
                "proposal_hash": workflow.proposal_hash,
                "to_status": WorkflowStatus.APPROVED.value,
            },
        )
    )
    body = _workflow_body(workflow)
    return _commit_response(
        session,
        operation=operation,
        idempotency_key=idempotency_key,
        actor_id=actor_id,
        request_hash=request_hash,
        response_status=200,
        response_body=body,
        resource_id=workflow.id,
    )


def _existing_simulation_operation(
    session: Session, workflow_id: str
) -> SimulationOperation | None:
    return session.scalar(
        select(SimulationOperation).where(SimulationOperation.workflow_id == workflow_id)
    )


def _claim_conflict(existing: SimulationOperation) -> ConflictError:
    if existing.status == SimulationOperationStatus.SIMULATING.value:
        return ConflictError(
            "simulation_in_progress",
            "another idempotency key already owns the simulation claim",
        )
    return ConflictError(
        "simulation_already_claimed",
        f"this workflow already has a terminal simulation operation ({existing.status})",
    )


def _commit_simulation_claim(
    session: Session,
    *,
    operation: SimulationOperation,
    actor_id: str,
    idempotency_key: str,
    request_hash: str,
    claim_body: ResponseBody,
) -> tuple[tuple[int, ResponseBody], bool]:
    workflow_id = operation.workflow_id
    _remember_response(
        session,
        operation="erp_draft.simulate",
        idempotency_key=idempotency_key,
        actor_id=actor_id,
        request_hash=request_hash,
        response_status=202,
        response_body=claim_body,
        resource_id=operation.id,
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        replay = _find_replay(
            session,
            operation="erp_draft.simulate",
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay, False
        existing = _existing_simulation_operation(session, workflow_id)
        if existing is not None:
            raise _claim_conflict(existing) from exc
        raise
    return (202, claim_body), True


def _simulation_idempotency_record(
    session: Session,
    *,
    actor_id: str,
    idempotency_key: str,
    request_hash: str,
) -> IdempotencyRecord:
    record = session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.operation == "erp_draft.simulate",
            IdempotencyRecord.idempotency_key == idempotency_key,
        )
    )
    if (
        record is None
        or record.actor_id != actor_id
        or record.request_hash != request_hash
    ):
        raise RuntimeError("durable simulation claim is missing or inconsistent")
    return record


def _finalize_simulation_success(
    session: Session,
    *,
    operation_id: str,
    result: dict[str, Any],
    actor_id: str,
    idempotency_key: str,
    request_hash: str,
) -> tuple[int, ResponseBody]:
    session.expire_all()
    operation = session.get(SimulationOperation, operation_id)
    if operation is None:
        raise RuntimeError("durable simulation operation disappeared")
    workflow = _get_workflow(session, operation.workflow_id)
    if operation.status != SimulationOperationStatus.SIMULATING.value:
        replay = _find_replay(
            session,
            operation="erp_draft.simulate",
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        raise ConflictError("invalid_state", "simulation operation is already terminal")

    completed_at = datetime.now(UTC)
    operation.status = SimulationOperationStatus.SIMULATED.value
    operation.result = result
    operation.completed_at = completed_at
    workflow.status = WorkflowStatus.SIMULATED.value
    workflow.simulation_result = result
    _flush_transition(session)
    session.add(
        AuditEvent(
            workflow_id=workflow.id,
            event_type=AuditEventType.SIMULATION_COMPLETED.value,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            payload={
                "connector": operation.connector,
                "operation_id": operation.id,
                "proposal_hash": operation.proposal_hash,
                "to_status": SimulationOperationStatus.SIMULATED.value,
            },
        )
    )
    body = _simulation_operation_body(operation)
    record = _simulation_idempotency_record(
        session,
        actor_id=actor_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    record.response_status = 200
    record.response_body = body
    session.commit()
    return 200, body


def _finalize_simulation_failure(
    session: Session,
    *,
    operation_id: str,
    error: Exception,
    actor_id: str,
    idempotency_key: str,
    request_hash: str,
) -> tuple[int, ResponseBody]:
    session.expire_all()
    operation = session.get(SimulationOperation, operation_id)
    if operation is None:
        raise RuntimeError("durable simulation operation disappeared")
    if operation.status != SimulationOperationStatus.SIMULATING.value:
        replay = _find_replay(
            session,
            operation="erp_draft.simulate",
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        raise ConflictError("invalid_state", "simulation operation is already terminal")

    operation.status = SimulationOperationStatus.SIMULATION_FAILED.value
    operation.error_code = "simulated_connector_failure"
    error_text = str(error).strip()
    operation.error_message = (error_text or type(error).__name__)[:500]
    operation.completed_at = datetime.now(UTC)
    _flush_transition(session)
    session.add(
        AuditEvent(
            workflow_id=operation.workflow_id,
            event_type=AuditEventType.SIMULATION_FAILED.value,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            payload={
                "connector": operation.connector,
                "error_code": operation.error_code,
                "operation_id": operation.id,
                "proposal_hash": operation.proposal_hash,
                "to_status": SimulationOperationStatus.SIMULATION_FAILED.value,
            },
        )
    )
    body = _simulation_operation_body(operation)
    record = _simulation_idempotency_record(
        session,
        actor_id=actor_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    record.response_status = 502
    record.response_body = body
    session.commit()
    return 502, body


def simulate_approved_proposal(
    session: Session,
    *,
    workflow_id: str,
    actor_id: str,
    idempotency_key: str,
) -> tuple[int, ResponseBody]:
    """Claim durably, call the fake outside a transaction, then finalize atomically."""

    request_hash = _request_hash({"workflow_id": workflow_id})
    replay = _find_replay(
        session,
        operation="erp_draft.simulate",
        idempotency_key=idempotency_key,
        actor_id=actor_id,
        request_hash=request_hash,
    )
    if replay is not None:
        return replay

    workflow = _get_workflow(session, workflow_id)
    _assert_proposal_integrity(workflow)
    if workflow.status == WorkflowStatus.PROPOSED.value:
        raise ConflictError(
            "approval_required", "simulation requires an explicitly approved proposal"
        )
    if workflow.status != WorkflowStatus.APPROVED.value:
        raise ConflictError(
            "invalid_state",
            f"simulation requires status 'approved'; current status is {workflow.status!r}",
        )
    existing = _existing_simulation_operation(session, workflow.id)
    if existing is not None:
        raise _claim_conflict(existing)

    operation = SimulationOperation(
        id=str(uuid4()),
        workflow_id=workflow.id,
        status=SimulationOperationStatus.SIMULATING.value,
        proposal_hash=workflow.proposal_hash,
        connector=SimulatedConnector.name,
        requested_by=actor_id,
        idempotency_key=idempotency_key,
        created_at=datetime.now(UTC),
    )
    session.add(operation)
    session.add(
        AuditEvent(
            workflow_id=workflow.id,
            event_type=AuditEventType.SIMULATION_STARTED.value,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            payload={
                "connector": operation.connector,
                "operation_id": operation.id,
                "proposal_hash": operation.proposal_hash,
                "to_status": SimulationOperationStatus.SIMULATING.value,
            },
        )
    )
    claim_body = _simulation_operation_body(operation)
    claim_response, owns_claim = _commit_simulation_claim(
        session,
        operation=operation,
        actor_id=actor_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        claim_body=claim_body,
    )
    if not owns_claim:
        return claim_response

    proposal = deepcopy(workflow.proposal)
    proposal_hash = workflow.proposal_hash
    connector = SimulatedConnector()
    try:
        result = connector.simulate(proposal, proposal_hash, operation.id)
    except Exception as exc:
        return _finalize_simulation_failure(
            session,
            operation_id=operation.id,
            error=exc,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
    return _finalize_simulation_success(
        session,
        operation_id=operation.id,
        result=result,
        actor_id=actor_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )


def reject_proposal(
    session: Session,
    *,
    workflow_id: str,
    request: RejectionRequest,
    actor_id: str,
    idempotency_key: str,
) -> tuple[int, ResponseBody]:
    """Reject a proposed draft without invoking any connector."""

    operation = "erp_draft.reject"
    request_value = {"workflow_id": workflow_id, **request.model_dump(mode="json")}
    request_hash = _request_hash(request_value)
    replay = _find_replay(
        session,
        operation=operation,
        idempotency_key=idempotency_key,
        actor_id=actor_id,
        request_hash=request_hash,
    )
    if replay is not None:
        return replay

    workflow = _get_workflow(session, workflow_id)
    if workflow.status != WorkflowStatus.PROPOSED.value:
        raise ConflictError(
            "invalid_state",
            f"rejection requires status 'proposed'; current status is {workflow.status!r}",
        )

    workflow.status = WorkflowStatus.REJECTED.value
    workflow.rejected_by = actor_id
    workflow.rejected_at = datetime.now(UTC)
    workflow.rejection_reason = request.reason
    _flush_transition(session)
    session.add(
        AuditEvent(
            workflow_id=workflow.id,
            event_type=AuditEventType.PROPOSAL_REJECTED.value,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            payload={
                "from_status": WorkflowStatus.PROPOSED.value,
                "proposal_hash": workflow.proposal_hash,
                "reason": request.reason,
                "to_status": WorkflowStatus.REJECTED.value,
            },
        )
    )
    body = _workflow_body(workflow)
    return _commit_response(
        session,
        operation=operation,
        idempotency_key=idempotency_key,
        actor_id=actor_id,
        request_hash=request_hash,
        response_status=200,
        response_body=body,
        resource_id=workflow.id,
    )


def get_workflow(session: Session, workflow_id: str) -> ResponseBody:
    return _workflow_body(_get_workflow(session, workflow_id))


def get_simulation_operation(session: Session, operation_id: str) -> ResponseBody:
    operation = session.get(SimulationOperation, operation_id)
    if operation is None:
        raise NotFoundError(
            "simulation_operation_not_found",
            f"simulation operation {operation_id!r} not found",
        )
    return _simulation_operation_body(operation)


def list_audit_events(session: Session, workflow_id: str) -> list[dict[str, Any]]:
    _get_workflow(session, workflow_id)
    events = session.scalars(
        select(AuditEvent)
        .where(AuditEvent.workflow_id == workflow_id)
        .order_by(AuditEvent.id.asc())
    ).all()
    return [
        AuditEventResponse(
            id=item.id,
            workflow_id=item.workflow_id,
            event_type=AuditEventType(item.event_type),
            actor_id=item.actor_id,
            idempotency_key=item.idempotency_key,
            payload=item.payload,
            created_at=_as_utc(item.created_at),
        ).model_dump(mode="json")
        for item in events
    ]
