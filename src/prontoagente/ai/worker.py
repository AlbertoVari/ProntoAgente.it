"""Lease-fenced worker for asynchronous AI preparation outbox events."""

import argparse
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, cast
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from prontoagente.ai.budget import provider_input_token_upper_bound
from prontoagente.ai.crypto import CryptoConfigurationError, SealedInputError, open_json
from prontoagente.ai.models import (
    AiInvocation,
    AiOutboxEvent,
    AiPreparationOperation,
    AiTenantPolicy,
    AiUsageBucket,
)
from prontoagente.ai.prompts import get_prompt, render_untrusted_email_data
from prontoagente.ai.providers import build_provider
from prontoagente.ai.schemas import AiPreparationRequest
from prontoagente.ai.service import preparation_body
from prontoagente.ai.tooling import (
    ORDER_EXTRACTION_TOOL_SCHEMA,
    TOOL_DESCRIPTION,
    ToolValidationFailure,
    to_order_document,
    validate_single_tool_call,
)
from prontoagente.ai.types import AiProvider, ProviderFailure, ProviderRequest
from prontoagente.canonical import sha256_digest
from prontoagente.config import Settings, get_settings
from prontoagente.db import SessionLocal
from prontoagente.v2.auth import AuthContext
from prontoagente.v2.demo_connectors import CATALOG_VERSION, canonical_order, reconcile_order
from prontoagente.v2.models import (
    AgentVersion,
    OrderSourceClaim,
    V2AuditEvent,
    V2IdempotencyRecord,
    Workflow,
    WorkflowVersion,
)
from prontoagente.v2.runs import (
    _new_proposed_run,
    _stage_proposed_run,
    _validate_input_schema,
)

ProviderFactory = Callable[[str], AiProvider]


@dataclass(frozen=True, slots=True)
class WorkItem:
    outbox_id: str
    claim_version: int
    preparation_id: str
    tenant_id: str
    workflow_id: str
    workflow_version_id: str
    agent_version_id: str
    created_by: str
    correlation_id: str
    source_ref_hash: str
    sealed_input: bytes
    encryption_key_id: str
    provider: str
    model: str
    prompt_id: str
    prompt_hash: str
    tool_name: str
    input_token_bound: int
    reserved_input_tokens: int
    reserved_output_tokens: int
    reserved_microusd: int
    input_rate_microusd: int
    output_rate_microusd: int


def _audit(
    session: Session,
    operation: AiPreparationOperation,
    *,
    event_type: str,
    status: str,
    error_code: str | None = None,
    invocation: AiInvocation | None = None,
) -> None:
    payload: dict[str, object] = {
        "correlation_id": operation.correlation_id,
        "preparation_id": operation.id,
        "provider": operation.provider,
        "model": operation.model,
        "prompt_hash": operation.prompt_hash,
        "tool_name": operation.tool_name,
        "status": status,
    }
    if error_code is not None:
        payload["error_code"] = error_code
    if operation.duration_ms is not None:
        payload["duration_ms"] = operation.duration_ms
    if operation.input_tokens is not None:
        payload["input_tokens"] = operation.input_tokens
    if operation.output_tokens is not None:
        payload["output_tokens"] = operation.output_tokens
    if operation.cost_microusd is not None:
        payload["cost_microusd"] = operation.cost_microusd
    if invocation is not None:
        payload["input_hash"] = invocation.input_hash
        if invocation.output_hash is not None:
            payload["output_hash"] = invocation.output_hash
    session.add(
        V2AuditEvent(
            tenant_id=operation.tenant_id,
            run_id=None,
            entity_type="ai_preparation",
            entity_id=operation.id,
            event_type=event_type,
            actor_id="system-ai-worker",
            idempotency_key=None,
            payload=payload,
        )
    )


def _claim_one(
    session: Session, *, worker_id: str, settings: Settings, now: datetime
) -> tuple[str, bool, int] | None:
    claimable = or_(
        and_(
            AiOutboxEvent.status.in_(("queued", "retry")),
            AiOutboxEvent.available_at <= now,
        ),
        and_(
            AiOutboxEvent.status == "processing",
            AiOutboxEvent.lease_until < now,
        ),
    )
    candidate = session.scalar(
        select(AiOutboxEvent)
        .where(claimable)
        .order_by(AiOutboxEvent.created_at, AiOutboxEvent.id)
        .limit(1)
    )
    if candidate is None:
        return None
    result = cast(
        CursorResult[Any],
        session.execute(
            update(AiOutboxEvent)
            .where(AiOutboxEvent.id == candidate.id, claimable)
            .values(
                status="processing",
                attempts=AiOutboxEvent.attempts + 1,
                claim_version=AiOutboxEvent.claim_version + 1,
                lease_owner=worker_id,
                lease_until=now + timedelta(seconds=settings.ai_worker_lease_seconds),
            )
            .execution_options(synchronize_session=False)
        ),
    )
    if result.rowcount != 1:
        session.rollback()
        return None
    session.refresh(candidate)
    claim_version = candidate.claim_version
    operation = session.get(AiPreparationOperation, candidate.preparation_id)
    if operation is None:
        session.rollback()
        return None
    existing = session.scalar(
        select(AiInvocation).where(
            AiInvocation.tenant_id == operation.tenant_id,
            AiInvocation.preparation_id == operation.id,
        )
    )
    if existing is not None:
        # A prior worker committed its spend ledger before I/O. Its outcome cannot
        # be inferred safely, so reclaim never invokes the provider a second time.
        if not _fence(
            session,
            outbox_id=candidate.id,
            worker_id=worker_id,
            claim_version=claim_version,
            values={
                "status": "unknown",
                "last_error_code": "prior_invocation_outcome_unknown",
                "lease_owner": None,
                "lease_until": None,
            },
        ):
            session.rollback()
            return None
        operation.status = "unknown"
        operation.error_code = "prior_invocation_outcome_unknown"
        operation.completed_at = now
        existing.status = "unknown"
        existing.error_code = "prior_invocation_outcome_unknown"
        existing.completed_at = now
        _audit(
            session,
            operation,
            event_type="ai_preparation_unknown",
            status="unknown",
            error_code="prior_invocation_outcome_unknown",
            invocation=existing,
        )
        session.commit()
        return candidate.id, False, claim_version

    operation.status = "processing"
    operation.started_at = now
    session.add(
        AiInvocation(
            id=str(uuid4()),
            tenant_id=operation.tenant_id,
            preparation_id=operation.id,
            status="started",
            provider=operation.provider,
            model=operation.model,
            prompt_id=operation.prompt_id,
            prompt_hash=operation.prompt_hash,
            tool_name=operation.tool_name,
            input_hash="sha256:"
            + hashlib.sha256(bytes(operation.sealed_input)).hexdigest(),
        )
    )
    _audit(
        session,
        operation,
        event_type="ai_preparation_started",
        status="processing",
    )
    session.commit()  # Durable spend claim before any provider I/O.
    return candidate.id, True, claim_version


def _load_work(
    factory: sessionmaker[Session],
    *,
    outbox_id: str,
    worker_id: str,
    claim_version: int,
) -> tuple[WorkItem, bool, bool] | None:
    with factory() as session:
        outbox = session.scalar(
            select(AiOutboxEvent).where(
                AiOutboxEvent.id == outbox_id,
                AiOutboxEvent.status == "processing",
                AiOutboxEvent.lease_owner == worker_id,
                AiOutboxEvent.claim_version == claim_version,
                AiOutboxEvent.lease_until >= datetime.now(UTC),
            )
        )
        if outbox is None:
            return None
        operation = session.get(AiPreparationOperation, outbox.preparation_id)
        if operation is None:
            return None
        policy = session.get(AiTenantPolicy, operation.tenant_id)
        policy_valid = bool(
            policy is not None
            and policy.enabled
            and policy.provider == operation.provider
            and policy.model == operation.model
        )
        tenant_network_enabled = bool(
            policy is not None and policy_valid and policy.network_enabled
        )
        return (
            WorkItem(
                outbox_id=outbox.id,
                claim_version=outbox.claim_version,
                preparation_id=operation.id,
                tenant_id=operation.tenant_id,
                workflow_id=operation.workflow_id,
                workflow_version_id=operation.workflow_version_id,
                agent_version_id=operation.agent_version_id,
                created_by=operation.created_by,
                correlation_id=operation.correlation_id,
                source_ref_hash=operation.source_ref_hash,
                sealed_input=bytes(operation.sealed_input),
                encryption_key_id=operation.encryption_key_id,
                provider=operation.provider,
                model=operation.model,
                prompt_id=operation.prompt_id,
                prompt_hash=operation.prompt_hash,
                tool_name=operation.tool_name,
                input_token_bound=operation.input_token_bound,
                reserved_input_tokens=operation.reserved_input_tokens,
                reserved_output_tokens=operation.reserved_output_tokens,
                reserved_microusd=operation.reserved_microusd,
                input_rate_microusd=operation.input_rate_microusd,
                output_rate_microusd=operation.output_rate_microusd,
            ),
            policy_valid,
            tenant_network_enabled,
        )


def _cost(tokens: int, rate: int) -> int:
    if tokens == 0 or rate == 0:
        return 0
    return (tokens * rate + 999_999) // 1_000_000


def _fence(
    session: Session,
    *,
    outbox_id: str,
    worker_id: str,
    claim_version: int,
    values: dict[str, object],
) -> bool:
    result = cast(
        CursorResult[Any],
        session.execute(
            update(AiOutboxEvent)
            .where(
                AiOutboxEvent.id == outbox_id,
                AiOutboxEvent.status == "processing",
                AiOutboxEvent.lease_owner == worker_id,
                AiOutboxEvent.claim_version == claim_version,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        ),
    )
    return result.rowcount == 1


def _record_input_hash(
    factory: sessionmaker[Session],
    *,
    outbox_id: str,
    worker_id: str,
    claim_version: int,
    input_hash: str,
) -> bool:
    """Persist a hash only after a live-lease generation CAS immediately pre-I/O."""

    with factory() as session:
        fenced = cast(
            CursorResult[Any],
            session.execute(
                update(AiOutboxEvent)
                .where(
                    AiOutboxEvent.id == outbox_id,
                    AiOutboxEvent.status == "processing",
                    AiOutboxEvent.lease_owner == worker_id,
                    AiOutboxEvent.claim_version == claim_version,
                    AiOutboxEvent.lease_until >= datetime.now(UTC),
                )
                .values(lease_owner=worker_id)
                .execution_options(synchronize_session=False)
            ),
        )
        if fenced.rowcount != 1:
            session.rollback()
            return False
        outbox = session.get(AiOutboxEvent, outbox_id)
        if outbox is None:
            session.rollback()
            return False
        invocation = session.scalar(
            select(AiInvocation).where(
                AiInvocation.tenant_id == outbox.tenant_id,
                AiInvocation.preparation_id == outbox.preparation_id,
                AiInvocation.status == "started",
            )
        )
        if invocation is None:
            return False
        invocation.input_hash = input_hash
        session.commit()
        return True


def _settle_usage(
    session: Session,
    operation: AiPreparationOperation,
    *,
    input_tokens: int,
    output_tokens: int,
    cost_microusd: int,
) -> bool:
    result = cast(
        CursorResult[Any],
        session.execute(
            update(AiUsageBucket)
            .where(
                AiUsageBucket.tenant_id == operation.tenant_id,
                AiUsageBucket.usage_date == operation.usage_date,
                AiUsageBucket.reserved_input_tokens
                >= operation.reserved_input_tokens,
                AiUsageBucket.reserved_output_tokens
                >= operation.reserved_output_tokens,
                AiUsageBucket.reserved_microusd >= operation.reserved_microusd,
            )
            .values(
                reserved_input_tokens=AiUsageBucket.reserved_input_tokens
                - operation.reserved_input_tokens,
                reserved_output_tokens=AiUsageBucket.reserved_output_tokens
                - operation.reserved_output_tokens,
                reserved_microusd=AiUsageBucket.reserved_microusd
                - operation.reserved_microusd,
                used_input_tokens=AiUsageBucket.used_input_tokens + input_tokens,
                used_output_tokens=AiUsageBucket.used_output_tokens + output_tokens,
                used_microusd=AiUsageBucket.used_microusd + cost_microusd,
                updated_at=datetime.now(UTC),
            )
        ),
    )
    return result.rowcount == 1


def _release_usage(session: Session, operation: AiPreparationOperation) -> bool:
    return _settle_usage(
        session,
        operation,
        input_tokens=0,
        output_tokens=0,
        cost_microusd=0,
    )


def _update_replay(
    session: Session,
    operation: AiPreparationOperation,
    invocation: AiInvocation,
) -> None:
    record = session.scalar(
        select(V2IdempotencyRecord).where(
            V2IdempotencyRecord.tenant_id == operation.tenant_id,
            V2IdempotencyRecord.operation == "ai.preparation.create",
            V2IdempotencyRecord.resource_id == operation.id,
        )
    )
    if record is not None:
        record.response_body = preparation_body(operation, invocation)


def _finalize_failure(
    factory: sessionmaker[Session],
    *,
    outbox_id: str,
    worker_id: str,
    claim_version: int,
    error_code: str,
    outcome_unknown: bool,
    duration_ms: int,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_microusd: int | None = None,
    output_hash: str | None = None,
) -> None:
    with factory() as session:
        outbox = session.get(AiOutboxEvent, outbox_id)
        if (
            outbox is None
            or outbox.status != "processing"
            or outbox.lease_owner != worker_id
            or outbox.claim_version != claim_version
        ):
            return
        operation = session.get(AiPreparationOperation, outbox.preparation_id)
        if operation is None:
            return
        invocation = session.scalar(
            select(AiInvocation).where(
                AiInvocation.tenant_id == operation.tenant_id,
                AiInvocation.preparation_id == operation.id,
            )
        )
        if invocation is None:
            return
        now = datetime.now(UTC)
        terminal = "unknown" if outcome_unknown else "failed"
        if not outcome_unknown:
            if input_tokens is None or output_tokens is None or cost_microusd is None:
                if not _release_usage(session, operation):
                    session.rollback()
                    return
                input_tokens = output_tokens = cost_microusd = 0
            elif not _settle_usage(
                session,
                operation,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_microusd=cost_microusd,
            ):
                session.rollback()
                return
        if not _fence(
            session,
            outbox_id=outbox.id,
            worker_id=worker_id,
            claim_version=claim_version,
            values={
                "status": terminal,
                "last_error_code": error_code,
                "lease_owner": None,
                "lease_until": None,
            },
        ):
            session.rollback()
            return
        operation.status = terminal
        operation.input_tokens = input_tokens
        operation.output_tokens = output_tokens
        operation.cost_microusd = cost_microusd
        operation.duration_ms = duration_ms
        operation.error_code = error_code
        operation.completed_at = now
        invocation.status = terminal
        invocation.output_hash = output_hash
        invocation.input_tokens = input_tokens
        invocation.output_tokens = output_tokens
        invocation.cost_microusd = cost_microusd
        invocation.duration_ms = duration_ms
        invocation.error_code = error_code
        invocation.completed_at = now
        _audit(
            session,
            operation,
            event_type=f"ai_preparation_{terminal}",
            status=terminal,
            error_code=error_code,
            invocation=invocation,
        )
        session.flush()
        _update_replay(session, operation, invocation)
        session.commit()


def _finalize_success(
    factory: sessionmaker[Session],
    *,
    outbox_id: str,
    worker_id: str,
    claim_version: int,
    order_payload: dict[str, Any],
    reconciliation: dict[str, Any],
    input_tokens: int,
    output_tokens: int,
    cost_microusd: int,
    duration_ms: int,
    output_hash: str,
) -> None:
    with factory() as session:
        outbox = session.get(AiOutboxEvent, outbox_id)
        if (
            outbox is None
            or outbox.status != "processing"
            or outbox.lease_owner != worker_id
            or outbox.claim_version != claim_version
        ):
            return
        operation = session.get(AiPreparationOperation, outbox.preparation_id)
        if operation is None:
            return
        invocation = session.scalar(
            select(AiInvocation).where(
                AiInvocation.tenant_id == operation.tenant_id,
                AiInvocation.preparation_id == operation.id,
            )
        )
        workflow = session.scalar(
            select(Workflow).where(
                Workflow.tenant_id == operation.tenant_id,
                Workflow.id == operation.workflow_id,
            )
        )
        workflow_version = session.scalar(
            select(WorkflowVersion).where(
                WorkflowVersion.tenant_id == operation.tenant_id,
                WorkflowVersion.id == operation.workflow_version_id,
                WorkflowVersion.status == "published",
            )
        )
        agent_version = session.scalar(
            select(AgentVersion).where(
                AgentVersion.tenant_id == operation.tenant_id,
                AgentVersion.id == operation.agent_version_id,
                AgentVersion.status == "published",
            )
        )
        if (
            invocation is None
            or workflow is None
            or workflow_version is None
            or agent_version is None
        ):
            session.rollback()
            return
        _validate_input_schema(workflow_version.input_schema, order_payload)
        target = str(workflow_version.config.get("target", "simulated_erp"))
        summary = (
            f"Prepare order {order_payload['order_id']}; "
            f"offline reconciliation {reconciliation['outcome']}"
        )
        provenance = {
            "source_connector": "demo_mailbox_v1",
            "source_ref_hash": operation.source_ref_hash,
            "provider": operation.provider,
            "model": operation.model,
            "prompt_id": operation.prompt_id,
            "prompt_hash": operation.prompt_hash,
            "tool_name": operation.tool_name,
            "catalog_version": CATALOG_VERSION,
        }
        proposal = {
            "schema_version": "2.0",
            "preparation_type": "ai_mail_order_reconciliation",
            "workflow_version_id": workflow_version.id,
            "agent_version_id": agent_version.id,
            "connector": workflow_version.connector,
            "action": workflow_version.action,
            "summary": summary,
            "target": target,
            "payload": order_payload,
            "normalized_order": order_payload,
            "reconciliation": reconciliation,
            "provenance": provenance,
        }
        context = AuthContext(
            tenant_id=operation.tenant_id,
            principal_id=operation.created_by,
            subject="system-ai-worker",
            display_name="AI worker",
            roles=frozenset(),
            api_key_id="system-ai-worker",
        )
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
        _stage_proposed_run(
            session,
            context,
            run=run,
            workflow_version=workflow_version,
            idempotency_key=f"ai:{operation.id}",
            audit_details={
                "preparation_type": "ai_mail_order_reconciliation",
                "preparation_id": operation.id,
                "reconciliation_outcome": reconciliation["outcome"],
            },
        )
        claim = session.scalar(
            select(OrderSourceClaim).where(
                OrderSourceClaim.tenant_id == operation.tenant_id,
                OrderSourceClaim.ai_preparation_id == operation.id,
            )
        )
        if claim is None or claim.run_id is not None:
            session.rollback()
            return
        claim.run_id = run.id
        if not _settle_usage(
            session,
            operation,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microusd=cost_microusd,
        ):
            session.rollback()
            return
        if not _fence(
            session,
            outbox_id=outbox.id,
            worker_id=worker_id,
            claim_version=claim_version,
            values={"status": "processed", "lease_owner": None, "lease_until": None},
        ):
            session.rollback()
            return
        now = datetime.now(UTC)
        operation.status = "completed"
        operation.result_run_id = run.id
        operation.input_tokens = input_tokens
        operation.output_tokens = output_tokens
        operation.cost_microusd = cost_microusd
        operation.duration_ms = duration_ms
        operation.completed_at = now
        invocation.status = "completed"
        invocation.output_hash = output_hash
        invocation.input_tokens = input_tokens
        invocation.output_tokens = output_tokens
        invocation.cost_microusd = cost_microusd
        invocation.duration_ms = duration_ms
        invocation.completed_at = now
        _audit(
            session,
            operation,
            event_type="ai_preparation_completed",
            status="completed",
            invocation=invocation,
        )
        session.flush()
        _update_replay(session, operation, invocation)
        session.commit()


def process_once(
    factory: sessionmaker[Session] = SessionLocal,
    *,
    settings: Settings | None = None,
    provider_factory: ProviderFactory | None = None,
    worker_id: str | None = None,
) -> bool:
    """Claim once, commit the spend ledger, invoke once, then finalize with fencing."""

    resolved = settings or get_settings()
    identity = worker_id or f"ai-worker-{uuid4()}"
    with factory() as session:
        claimed = _claim_one(
            session,
            worker_id=identity,
            settings=resolved,
            now=datetime.now(UTC),
        )
    if claimed is None:
        return False
    outbox_id, should_invoke, claim_version = claimed
    if not should_invoke:
        return True
    loaded = _load_work(
        factory,
        outbox_id=outbox_id,
        worker_id=identity,
        claim_version=claim_version,
    )
    if loaded is None:
        return True
    work, policy_valid, tenant_network_enabled = loaded
    started = monotonic()
    provider: AiProvider | None = None
    try:
        sealed = open_json(
            work.sealed_input,
            tenant_id=work.tenant_id,
            operation_id=work.preparation_id,
            expected_key_id=work.encryption_key_id,
            configured_key_id=resolved.ai_encryption_key_id,
            encoded_key=resolved.ai_encryption_key,
        )
        payload = AiPreparationRequest.model_validate(sealed)
        prompt = get_prompt(work.prompt_id)
        if prompt.prompt_hash != work.prompt_hash or prompt.tool_name != work.tool_name:
            raise ProviderFailure("pinned_prompt_mismatch", outcome_unknown=False)
        user_data = render_untrusted_email_data(
            payload.envelope.model_dump(mode="json")
        )
        input_token_bound = provider_input_token_upper_bound(
            system_prompt=prompt.system_prompt,
            user_data=user_data,
            tool_name=work.tool_name,
            tool_description=TOOL_DESCRIPTION,
            tool_schema=ORDER_EXTRACTION_TOOL_SCHEMA,
        )
        if (
            input_token_bound != work.input_token_bound
            or input_token_bound > work.reserved_input_tokens
        ):
            raise ProviderFailure("provider_input_bound_mismatch", outcome_unknown=False)
        request = ProviderRequest(
            provider=work.provider,
            model=work.model,
            correlation_id=work.correlation_id,
            prompt_id=work.prompt_id,
            prompt_hash=work.prompt_hash,
            system_prompt=prompt.system_prompt,
            user_data=user_data,
            tool_name=work.tool_name,
            tool_schema=ORDER_EXTRACTION_TOOL_SCHEMA,
            max_output_tokens=work.reserved_output_tokens,
        )
        if (
            not resolved.ai_preparation_enabled
            or not resolved.enable_demo_connectors
            or resolved.app_env == "production"
            or not policy_valid
        ):
            raise ProviderFailure("ai_policy_disabled", outcome_unknown=False)
        provider = (
            provider_factory(work.provider)
            if provider_factory is not None
            else build_provider(
                work.provider,
                resolved,
                tenant_network_enabled=tenant_network_enabled,
            )
        )
        if not _record_input_hash(
            factory,
            outbox_id=outbox_id,
            worker_id=identity,
            claim_version=work.claim_version,
            input_hash=sha256_digest(
                {
                    "prompt_hash": work.prompt_hash,
                    "user_data": user_data,
                }
            ),
        ):
            return True
        response = provider.invoke(request)
        if response.provider != work.provider or response.model != work.model:
            raise ProviderFailure("provider_identity_mismatch", outcome_unknown=True)
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        cost_microusd = _cost(input_tokens, work.input_rate_microusd) + _cost(
            output_tokens, work.output_rate_microusd
        )
        if (
            input_tokens > work.reserved_input_tokens
            or output_tokens > work.reserved_output_tokens
            or cost_microusd > work.reserved_microusd
        ):
            _finalize_failure(
                factory,
                outbox_id=outbox_id,
                worker_id=identity,
                claim_version=work.claim_version,
                error_code="provider_usage_exceeded_reservation",
                outcome_unknown=False,
                duration_ms=int((monotonic() - started) * 1000),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_microusd=cost_microusd,
                output_hash=response.response_hash,
            )
            return True
        extraction = validate_single_tool_call(
            tuple((call.name, call.arguments) for call in response.tool_calls)
        )
        order = to_order_document(extraction)
        order_payload = canonical_order(order)
        reconciliation = reconcile_order(order)
    except (CryptoConfigurationError, SealedInputError, ValidationError):
        _finalize_failure(
            factory,
            outbox_id=outbox_id,
            worker_id=identity,
            claim_version=work.claim_version,
            error_code="sealed_input_invalid",
            outcome_unknown=False,
            duration_ms=int((monotonic() - started) * 1000),
        )
    except ValueError:
        _finalize_failure(
            factory,
            outbox_id=outbox_id,
            worker_id=identity,
            claim_version=work.claim_version,
            error_code="pinned_prompt_invalid",
            outcome_unknown=False,
            duration_ms=int((monotonic() - started) * 1000),
        )
    except ToolValidationFailure as exc:
        _finalize_failure(
            factory,
            outbox_id=outbox_id,
            worker_id=identity,
            claim_version=work.claim_version,
            error_code=exc.code,
            outcome_unknown=False,
            duration_ms=int((monotonic() - started) * 1000),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microusd=cost_microusd,
            output_hash=response.response_hash,
        )
    except ProviderFailure as exc:
        _finalize_failure(
            factory,
            outbox_id=outbox_id,
            worker_id=identity,
            claim_version=work.claim_version,
            error_code=exc.code,
            outcome_unknown=exc.outcome_unknown,
            duration_ms=int((monotonic() - started) * 1000),
        )
    except Exception:
        _finalize_failure(
            factory,
            outbox_id=outbox_id,
            worker_id=identity,
            claim_version=work.claim_version,
            error_code="provider_outcome_unknown",
            outcome_unknown=True,
            duration_ms=int((monotonic() - started) * 1000),
        )
    else:
        try:
            _finalize_success(
                factory,
                outbox_id=outbox_id,
                worker_id=identity,
                claim_version=work.claim_version,
                order_payload=order_payload,
                reconciliation=reconciliation,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_microusd=cost_microusd,
                duration_ms=int((monotonic() - started) * 1000),
                output_hash=response.response_hash,
            )
        except Exception:
            _finalize_failure(
                factory,
                outbox_id=outbox_id,
                worker_id=identity,
                claim_version=work.claim_version,
                error_code="run_finalization_failed",
                outcome_unknown=False,
                duration_ms=int((monotonic() - started) * 1000),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_microusd=cost_microusd,
                output_hash=response.response_hash,
            )
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Process AI preparation outbox events")
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
