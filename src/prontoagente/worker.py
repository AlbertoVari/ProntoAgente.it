"""Lease-based outbox worker for v2 execution operations."""

import argparse
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from prontoagente.config import Settings, get_settings
from prontoagente.db import SessionLocal
from prontoagente.v2.connectors import (
    ConnectorExecutionError,
    ConnectorNotConfiguredError,
    RetryableConnectorError,
    RuntimeConnector,
    runtime_connector,
)
from prontoagente.v2.models import (
    ExecutionOperation,
    OutboxEvent,
    Run,
    V2AuditEvent,
    V2IdempotencyRecord,
)
from prontoagente.v2.runs import operation_body

ConnectorFactory = Callable[[str], RuntimeConnector]


def _claim_one(
    session: Session, *, worker_id: str, settings: Settings, now: datetime
) -> str | None:
    claimable = or_(
        and_(OutboxEvent.status.in_(("pending", "retry")), OutboxEvent.available_at <= now),
        and_(OutboxEvent.status == "processing", OutboxEvent.lease_until < now),
    )
    candidate = session.scalar(
        select(OutboxEvent).where(claimable).order_by(OutboxEvent.created_at).limit(1)
    )
    if candidate is None:
        return None
    lease_until = now + timedelta(seconds=settings.outbox_lease_seconds)
    result = cast(
        CursorResult[Any],
        session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == candidate.id, claimable)
            .values(
                status="processing",
                attempts=OutboxEvent.attempts + 1,
                lease_owner=worker_id,
                lease_until=lease_until,
            )
            .execution_options(synchronize_session=False)
        )
    )
    if result.rowcount != 1:
        session.rollback()
        return None
    operation = session.get(ExecutionOperation, candidate.operation_id)
    if operation is None:
        session.rollback()
        return None
    if operation.status == "pending":
        operation.status = "executing"
    session.commit()
    return candidate.id


def _update_execute_replay(
    session: Session, operation: ExecutionOperation, *, status_code: int
) -> None:
    record = session.scalar(
        select(V2IdempotencyRecord).where(
            V2IdempotencyRecord.tenant_id == operation.tenant_id,
            V2IdempotencyRecord.operation == "run.execute",
            V2IdempotencyRecord.resource_id == operation.id,
        )
    )
    if record is not None:
        record.response_status = status_code
        record.response_body = operation_body(operation)


def _audit(
    session: Session,
    *,
    run: Run,
    operation: ExecutionOperation,
    event_type: str,
    payload: dict[str, object],
) -> None:
    session.add(
        V2AuditEvent(
            tenant_id=run.tenant_id,
            run_id=run.id,
            entity_type="execution_operation",
            entity_id=operation.id,
            event_type=event_type,
            actor_id="system-worker",
            idempotency_key=None,
            payload=payload,
        )
    )


def _fence_outbox(
    session: Session,
    *,
    outbox_id: str,
    worker_id: str,
    values: dict[str, object],
) -> bool:
    """Finalize only while this worker still owns the durable lease."""

    result = cast(
        CursorResult[Any],
        session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.id == outbox_id,
                OutboxEvent.status == "processing",
                OutboxEvent.lease_owner == worker_id,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        ),
    )
    return result.rowcount == 1


def _finalize_success(
    factory: sessionmaker[Session],
    *,
    outbox_id: str,
    worker_id: str,
    result: dict[str, object],
) -> None:
    with factory() as session:
        outbox = session.get(OutboxEvent, outbox_id)
        if (
            outbox is None
            or outbox.status != "processing"
            or outbox.lease_owner != worker_id
        ):
            return
        operation = session.get(ExecutionOperation, outbox.operation_id)
        if operation is None:
            return
        run = session.get(Run, operation.run_id)
        if run is None:
            return
        now = datetime.now(UTC)
        if not _fence_outbox(
            session,
            outbox_id=outbox.id,
            worker_id=worker_id,
            values={"status": "processed", "lease_owner": None, "lease_until": None},
        ):
            session.rollback()
            return
        operation.status = "executed"
        operation.result = result
        operation.completed_at = now
        run.status = "executed"
        _audit(
            session,
            run=run,
            operation=operation,
            event_type="execution_completed",
            payload={"connector": operation.connector, "operation_id": operation.id},
        )
        session.flush()
        _update_execute_replay(session, operation, status_code=200)
        session.commit()


def _finalize_error(
    factory: sessionmaker[Session],
    *,
    outbox_id: str,
    worker_id: str,
    error: Exception,
    settings: Settings,
) -> None:
    with factory() as session:
        outbox = session.get(OutboxEvent, outbox_id)
        if (
            outbox is None
            or outbox.status != "processing"
            or outbox.lease_owner != worker_id
        ):
            return
        operation = session.get(ExecutionOperation, outbox.operation_id)
        if operation is None:
            return
        run = session.get(Run, operation.run_id)
        if run is None:
            return
        now = datetime.now(UTC)

        if (
            isinstance(error, RetryableConnectorError)
            and outbox.attempts < settings.outbox_max_attempts
        ):
            if not _fence_outbox(
                session,
                outbox_id=outbox.id,
                worker_id=worker_id,
                values={
                    "status": "retry",
                    "available_at": now + timedelta(seconds=error.retry_after),
                    "last_error": error.code,
                    "lease_owner": None,
                    "lease_until": None,
                },
            ):
                session.rollback()
                return
            operation.status = "pending"
            _audit(
                session,
                run=run,
                operation=operation,
                event_type="execution_retry_scheduled",
                payload={"attempt": outbox.attempts, "error_code": error.code},
            )
            session.commit()
            return

        known_failure = isinstance(
            error, (ConnectorExecutionError, ConnectorNotConfiguredError)
        )
        terminal_status = "failed" if known_failure else "unknown"
        error_code = getattr(error, "code", "connector_outcome_unknown")
        if not _fence_outbox(
            session,
            outbox_id=outbox.id,
            worker_id=worker_id,
            values={
                "status": terminal_status,
                "last_error": error_code,
                "lease_owner": None,
                "lease_until": None,
            },
        ):
            session.rollback()
            return
        operation.status = terminal_status
        operation.error_code = error_code
        operation.error_message = (
            str(error)[:500] if known_failure else "connector outcome requires reconciliation"
        )
        operation.completed_at = now
        run.status = terminal_status
        _audit(
            session,
            run=run,
            operation=operation,
            event_type=f"execution_{terminal_status}",
            payload={"error_code": operation.error_code, "operation_id": operation.id},
        )
        session.flush()
        _update_execute_replay(session, operation, status_code=502)
        session.commit()


def process_once(
    factory: sessionmaker[Session] = SessionLocal,
    *,
    settings: Settings | None = None,
    connector_factory: ConnectorFactory | None = None,
    worker_id: str | None = None,
) -> bool:
    """Claim at most one event, perform I/O outside a transaction, and finalize."""

    resolved = settings or get_settings()
    identity = worker_id or f"worker-{uuid4()}"
    with factory() as session:
        outbox_id = _claim_one(
            session, worker_id=identity, settings=resolved, now=datetime.now(UTC)
        )
    if outbox_id is None:
        return False

    with factory() as session:
        outbox = session.get(OutboxEvent, outbox_id)
        if outbox is None:
            return True
        operation = session.get(ExecutionOperation, outbox.operation_id)
        if operation is None:
            return True
        run = session.get(Run, operation.run_id)
        if run is None:
            return True
        connector_name = operation.connector
        action = operation.action
        payload = dict(run.payload)
        operation_id = operation.id
        tenant_id = operation.tenant_id
        session.rollback()

    factory_function = connector_factory or (
        lambda name: runtime_connector(
            name, prontoagente_tenant_id=tenant_id, settings=resolved
        )
    )
    try:
        connector = factory_function(connector_name)
        result = connector.execute(action=action, payload=payload, operation_id=operation_id)
    except Exception as exc:
        _finalize_error(
            factory,
            outbox_id=outbox_id,
            worker_id=identity,
            error=exc,
            settings=resolved,
        )
    else:
        _finalize_success(
            factory, outbox_id=outbox_id, worker_id=identity, result=result
        )
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Process ProntoAgente outbox events")
    parser.add_argument("--once", action="store_true", help="process at most one event")
    args = parser.parse_args()
    if args.once:
        process_once()
        return
    while True:
        if not process_once():
            time.sleep(1)


if __name__ == "__main__":
    main()
