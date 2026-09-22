"""Tenant-scoped policy, budget reservation, and AI preparation enqueueing."""

from datetime import UTC, date, datetime
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from prontoagente.ai.budget import provider_input_token_upper_bound
from prontoagente.ai.crypto import CryptoConfigurationError, seal_json
from prontoagente.ai.models import (
    AiInvocation,
    AiOutboxEvent,
    AiPreparationOperation,
    AiTenantPolicy,
    AiUsageBucket,
)
from prontoagente.ai.prompts import (
    EMAIL_ORDER_EXTRACT_V1,
    get_prompt,
    render_untrusted_email_data,
)
from prontoagente.ai.schemas import (
    AiPolicyResponse,
    AiPolicyUpdate,
    AiPreparationRequest,
    AiPreparationResponse,
    AiUsageResponse,
)
from prontoagente.ai.tooling import (
    ORDER_EXTRACTION_TOOL_SCHEMA,
    TOOL_DESCRIPTION,
    TOOL_NAME,
)
from prontoagente.config import Settings, get_settings
from prontoagente.errors import ConflictError, NotFoundError
from prontoagente.v2.auth import AuthContext
from prontoagente.v2.demo_connectors import source_reference_hash
from prontoagente.v2.models import OrderSourceClaim, V2AuditEvent
from prontoagente.v2.runs import (
    _find_replay,
    _published_stack,
    _remember,
    _request_hash,
)
from prontoagente.v2.schemas import AgentAiDefinition

ResponseBody = dict[str, Any]


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return None if value is None else _utc(value)


def preparation_body(
    operation: AiPreparationOperation, invocation: AiInvocation | None = None
) -> ResponseBody:
    return AiPreparationResponse(
        id=operation.id,
        status=cast(
            Literal["queued", "processing", "completed", "failed", "unknown"],
            operation.status,
        ),
        correlation_id=operation.correlation_id,
        workflow_id=operation.workflow_id,
        provider=cast(Literal["fake", "openai"], operation.provider),
        model=operation.model,
        prompt_id=operation.prompt_id,
        prompt_hash=operation.prompt_hash,
        tool_name=operation.tool_name,
        input_hash=None if invocation is None else invocation.input_hash,
        output_hash=None if invocation is None else invocation.output_hash,
        result_run_id=operation.result_run_id,
        input_tokens=operation.input_tokens,
        output_tokens=operation.output_tokens,
        cost_microusd=operation.cost_microusd,
        duration_ms=operation.duration_ms,
        error_code=operation.error_code,
        created_at=_utc(operation.created_at),
        started_at=_optional_utc(operation.started_at),
        completed_at=_optional_utc(operation.completed_at),
    ).model_dump(mode="json")


def _current_preparation_body(
    session: Session, operation: AiPreparationOperation
) -> ResponseBody:
    invocation = session.scalar(
        select(AiInvocation).where(
            AiInvocation.tenant_id == operation.tenant_id,
            AiInvocation.preparation_id == operation.id,
        )
    )
    return preparation_body(operation, invocation)


def policy_body(policy: AiTenantPolicy) -> ResponseBody:
    return AiPolicyResponse(
        enabled=policy.enabled,
        provider=cast(Literal["fake", "openai"], policy.provider),
        model=policy.model,
        network_enabled=policy.network_enabled,
        max_input_tokens=policy.max_input_tokens,
        max_output_tokens=policy.max_output_tokens,
        max_run_microusd=policy.max_run_microusd,
        daily_input_tokens=policy.daily_input_tokens,
        daily_output_tokens=policy.daily_output_tokens,
        daily_microusd=policy.daily_microusd,
        lock_version=policy.lock_version,
        created_at=_utc(policy.created_at),
        updated_at=_utc(policy.updated_at),
    ).model_dump(mode="json")


def _policy(session: Session, context: AuthContext) -> AiTenantPolicy:
    policy = session.get(AiTenantPolicy, context.tenant_id)
    if policy is None:
        raise NotFoundError("ai_policy_not_found", "AI policy is not configured")
    return policy


def get_policy(session: Session, context: AuthContext) -> ResponseBody:
    return policy_body(_policy(session, context))


def _validate_deployment_limits(request: AiPolicyUpdate, settings: Settings) -> None:
    comparisons = (
        (request.max_input_tokens, settings.ai_hard_max_input_tokens),
        (request.max_output_tokens, settings.ai_hard_max_output_tokens),
        (request.max_run_microusd, settings.ai_hard_max_run_microusd),
        (request.daily_input_tokens, settings.ai_hard_daily_input_tokens),
        (request.daily_output_tokens, settings.ai_hard_daily_output_tokens),
        (request.daily_microusd, settings.ai_hard_daily_microusd),
    )
    if any(value > hard_cap for value, hard_cap in comparisons):
        raise ConflictError(
            "ai_policy_exceeds_deployment_cap",
            "AI policy exceeds a deployment hard cap",
        )
    if request.provider == "fake" and (
        request.network_enabled or request.model != "fake-pa1-v1"
    ):
        raise ConflictError(
            "invalid_ai_policy",
            "the offline fake provider requires model fake-pa1-v1 and no network",
        )
    if request.provider == "openai":
        endpoint = urlsplit(settings.ai_openai_responses_url or "")
        if (
            not settings.ai_network_enabled
            or not request.network_enabled
            or not settings.ai_openai_api_key
            or endpoint.scheme != "https"
            or endpoint.hostname not in settings.ai_openai_allowed_hosts
            or endpoint.username is not None
            or endpoint.password is not None
            or bool(endpoint.query)
            or bool(endpoint.fragment)
            or request.model not in settings.ai_openai_allowed_models
            or settings.ai_openai_input_microusd_per_million is None
            or settings.ai_openai_output_microusd_per_million is None
        ):
            raise ConflictError(
                "openai_provider_not_configured",
                "the opt-in OpenAI provider gates are not fully configured",
            )


def put_policy(
    session: Session,
    context: AuthContext,
    request: AiPolicyUpdate,
    *,
    idempotency_key: str,
    settings: Settings | None = None,
) -> tuple[int, ResponseBody]:
    resolved = settings or get_settings()
    operation_name = "ai.policy.put"
    request_hash = _request_hash(request.model_dump(mode="json"))
    replay = _find_replay(
        session,
        context,
        operation=operation_name,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    if replay is not None:
        return replay
    _validate_deployment_limits(request, resolved)
    policy = session.get(AiTenantPolicy, context.tenant_id)
    try:
        if policy is None:
            if request.lock_version != 1:
                raise ConflictError(
                    "optimistic_lock_conflict",
                    "AI policy does not exist at that version",
                )
            policy = AiTenantPolicy(
                tenant_id=context.tenant_id,
                enabled=request.enabled,
                provider=request.provider,
                model=request.model,
                network_enabled=request.network_enabled,
                max_input_tokens=request.max_input_tokens,
                max_output_tokens=request.max_output_tokens,
                max_run_microusd=request.max_run_microusd,
                daily_input_tokens=request.daily_input_tokens,
                daily_output_tokens=request.daily_output_tokens,
                daily_microusd=request.daily_microusd,
            )
            session.add(policy)
            session.flush()
        else:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(AiTenantPolicy)
                    .where(
                        AiTenantPolicy.tenant_id == context.tenant_id,
                        AiTenantPolicy.lock_version == request.lock_version,
                    )
                    .values(
                        enabled=request.enabled,
                        provider=request.provider,
                        model=request.model,
                        network_enabled=request.network_enabled,
                        max_input_tokens=request.max_input_tokens,
                        max_output_tokens=request.max_output_tokens,
                        max_run_microusd=request.max_run_microusd,
                        daily_input_tokens=request.daily_input_tokens,
                        daily_output_tokens=request.daily_output_tokens,
                        daily_microusd=request.daily_microusd,
                        lock_version=AiTenantPolicy.lock_version + 1,
                        updated_at=datetime.now(UTC),
                    )
                    .execution_options(synchronize_session=False)
                ),
            )
            if result.rowcount != 1:
                session.rollback()
                raise ConflictError(
                    "optimistic_lock_conflict", "AI policy changed; reload and retry"
                )
            session.expire(policy)
            session.refresh(policy)
        body = policy_body(policy)
        _remember(
            session,
            context,
            operation=operation_name,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            status=200,
            body=body,
            resource_id=context.tenant_id,
        )
        session.commit()
    except IntegrityError as exc:
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
        if session.get(AiTenantPolicy, context.tenant_id) is not None:
            raise ConflictError(
                "optimistic_lock_conflict", "AI policy changed; reload and retry"
            ) from exc
        raise
    return 200, body


def get_usage(
    session: Session, context: AuthContext, *, usage_date: date | None = None
) -> ResponseBody:
    resolved_date = usage_date or datetime.now(UTC).date()
    bucket = session.get(AiUsageBucket, (context.tenant_id, resolved_date))
    return AiUsageResponse(
        usage_date=resolved_date,
        used_input_tokens=0 if bucket is None else bucket.used_input_tokens,
        used_output_tokens=0 if bucket is None else bucket.used_output_tokens,
        used_microusd=0 if bucket is None else bucket.used_microusd,
        reserved_input_tokens=0 if bucket is None else bucket.reserved_input_tokens,
        reserved_output_tokens=0 if bucket is None else bucket.reserved_output_tokens,
        reserved_microusd=0 if bucket is None else bucket.reserved_microusd,
    ).model_dump(mode="json")


def get_preparation(
    session: Session, context: AuthContext, preparation_id: str
) -> ResponseBody:
    operation = session.scalar(
        select(AiPreparationOperation).where(
            AiPreparationOperation.tenant_id == context.tenant_id,
            AiPreparationOperation.id == preparation_id,
        )
    )
    if operation is None:
        raise NotFoundError("ai_preparation_not_found", "AI preparation not found")
    return _current_preparation_body(session, operation)


def _pricing(provider: str, settings: Settings) -> tuple[int, int]:
    if provider == "fake":
        return (
            settings.ai_fake_input_microusd_per_million,
            settings.ai_fake_output_microusd_per_million,
        )
    if (
        provider == "openai"
        and settings.ai_openai_input_microusd_per_million is not None
        and settings.ai_openai_output_microusd_per_million is not None
    ):
        return (
            settings.ai_openai_input_microusd_per_million,
            settings.ai_openai_output_microusd_per_million,
        )
    raise ConflictError(
        "ai_pricing_unavailable", "provider pricing must be configured before reservation"
    )


def _cost(tokens: int, rate_microusd_per_million: int) -> int:
    if tokens == 0 or rate_microusd_per_million == 0:
        return 0
    return (tokens * rate_microusd_per_million + 999_999) // 1_000_000


def _ensure_usage_bucket(session: Session, tenant_id: str, usage_date: date) -> None:
    values = {
        "tenant_id": tenant_id,
        "usage_date": usage_date,
        "used_input_tokens": 0,
        "used_output_tokens": 0,
        "used_microusd": 0,
        "reserved_input_tokens": 0,
        "reserved_output_tokens": 0,
        "reserved_microusd": 0,
    }
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        session.execute(
            sqlite_insert(AiUsageBucket)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["tenant_id", "usage_date"])
        )
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as postgresql_insert

        session.execute(
            postgresql_insert(AiUsageBucket)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["tenant_id", "usage_date"])
        )
    elif session.get(AiUsageBucket, (tenant_id, usage_date)) is None:
        session.add(AiUsageBucket(**values))
        session.flush()


def _agent_ai_limits(
    definition: dict[str, Any], policy: AiTenantPolicy, settings: Settings
) -> tuple[str, str, int, int]:
    raw = definition.get("ai")
    if raw is None:
        return (
            EMAIL_ORDER_EXTRACT_V1.identifier,
            TOOL_NAME,
            min(policy.max_input_tokens, settings.ai_hard_max_input_tokens),
            min(policy.max_output_tokens, settings.ai_hard_max_output_tokens),
        )
    try:
        ai = AgentAiDefinition.model_validate(raw)
    except ValueError as exc:
        raise ConflictError(
            "agent_ai_definition_invalid", "pinned AI definition is invalid"
        ) from exc
    return (
        ai.prompt_id,
        ai.tool_name,
        min(ai.max_input_tokens, policy.max_input_tokens, settings.ai_hard_max_input_tokens),
        min(
            ai.max_output_tokens,
            policy.max_output_tokens,
            settings.ai_hard_max_output_tokens,
        ),
    )


def enqueue_preparation(
    session: Session,
    context: AuthContext,
    *,
    workflow_id: str,
    request: AiPreparationRequest,
    idempotency_key: str,
    settings: Settings | None = None,
) -> tuple[int, ResponseBody]:
    resolved = settings or get_settings()
    if (
        not resolved.ai_preparation_enabled
        or not resolved.enable_demo_connectors
        or resolved.app_env == "production"
    ):
        raise ConflictError(
            "ai_preparation_disabled",
            "AI demo preparation is disabled in this environment",
        )
    operation_name = "ai.preparation.create"
    request_hash = _request_hash(
        {"workflow_id": workflow_id, **request.model_dump(mode="json")}
    )
    replay = _find_replay(
        session,
        context,
        operation=operation_name,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    if replay is not None:
        current = session.get(AiPreparationOperation, replay[1]["id"])
        return 202, replay[1] if current is None else _current_preparation_body(session, current)

    policy = _policy(session, context)
    if not policy.enabled:
        raise ConflictError("ai_policy_disabled", "AI preparation is disabled by tenant policy")
    policy_request = AiPolicyUpdate(
        enabled=policy.enabled,
        provider=cast(Literal["fake", "openai"], policy.provider),
        model=policy.model,
        network_enabled=policy.network_enabled,
        max_input_tokens=policy.max_input_tokens,
        max_output_tokens=policy.max_output_tokens,
        max_run_microusd=policy.max_run_microusd,
        daily_input_tokens=policy.daily_input_tokens,
        daily_output_tokens=policy.daily_output_tokens,
        daily_microusd=policy.daily_microusd,
        lock_version=policy.lock_version,
    )
    _validate_deployment_limits(policy_request, resolved)

    workflow, workflow_version, agent_version = _published_stack(
        session, context, workflow_id
    )
    if (
        workflow_version.connector != "simulated_erp"
        or workflow_version.action != "create_sales_order"
        or not workflow_version.approval_required
    ):
        raise ConflictError(
            "ai_workflow_incompatible",
            "AI preparation requires an approval-first simulated ERP workflow",
        )
    prompt_id, tool_name, reserved_input, reserved_output = _agent_ai_limits(
        agent_version.definition, policy, resolved
    )
    try:
        prompt = get_prompt(prompt_id)
    except ValueError as exc:
        raise ConflictError("prompt_not_allowlisted", "pinned prompt is unavailable") from exc
    if tool_name != prompt.tool_name or tool_name != TOOL_NAME:
        raise ConflictError("tool_not_allowlisted", "pinned tool is unavailable")
    input_rate, output_rate = _pricing(policy.provider, resolved)
    reserved_cost = _cost(reserved_input, input_rate) + _cost(
        reserved_output, output_rate
    )
    if reserved_cost > min(policy.max_run_microusd, resolved.ai_hard_max_run_microusd):
        raise ConflictError("ai_run_budget_exceeded", "AI run reservation exceeds its cap")

    envelope = request.envelope
    user_data = render_untrusted_email_data(envelope.model_dump(mode="json"))
    input_token_bound = provider_input_token_upper_bound(
        system_prompt=prompt.system_prompt,
        user_data=user_data,
        tool_name=tool_name,
        tool_description=TOOL_DESCRIPTION,
        tool_schema=ORDER_EXTRACTION_TOOL_SCHEMA,
    )
    if input_token_bound > reserved_input:
        raise ConflictError(
            "ai_input_budget_exceeded",
            "the conservative provider input bound exceeds the reserved token cap",
        )
    source_hash = source_reference_hash(
        source_connector=envelope.source_connector,
        message_id=envelope.message_id,
        internet_message_id=envelope.internet_message_id,
    )
    existing_claim = session.scalar(
        select(OrderSourceClaim).where(
            OrderSourceClaim.tenant_id == context.tenant_id,
            OrderSourceClaim.source_connector == envelope.source_connector,
            OrderSourceClaim.source_ref_hash == source_hash,
        )
    )
    if existing_claim is not None:
        raise ConflictError(
            "order_source_already_claimed",
            "this source message already has a preparation",
        )
    preparation_id = str(uuid4())
    correlation_id = str(uuid4())
    try:
        key_id, sealed_input = seal_json(
            request.model_dump(mode="json"),
            tenant_id=context.tenant_id,
            operation_id=preparation_id,
            key_id=resolved.ai_encryption_key_id,
            encoded_key=resolved.ai_encryption_key,
        )
    except CryptoConfigurationError as exc:
        raise ConflictError(
            "ai_encryption_not_configured",
            "AI queue encryption is not configured",
        ) from exc

    usage_date = datetime.now(UTC).date()
    _ensure_usage_bucket(session, context.tenant_id, usage_date)
    result = cast(
        CursorResult[Any],
        session.execute(
            update(AiUsageBucket)
            .where(
                AiUsageBucket.tenant_id == context.tenant_id,
                AiUsageBucket.usage_date == usage_date,
                AiUsageBucket.used_input_tokens
                + AiUsageBucket.reserved_input_tokens
                + reserved_input
                <= min(policy.daily_input_tokens, resolved.ai_hard_daily_input_tokens),
                AiUsageBucket.used_output_tokens
                + AiUsageBucket.reserved_output_tokens
                + reserved_output
                <= min(policy.daily_output_tokens, resolved.ai_hard_daily_output_tokens),
                AiUsageBucket.used_microusd
                + AiUsageBucket.reserved_microusd
                + reserved_cost
                <= min(policy.daily_microusd, resolved.ai_hard_daily_microusd),
            )
            .values(
                reserved_input_tokens=AiUsageBucket.reserved_input_tokens
                + reserved_input,
                reserved_output_tokens=AiUsageBucket.reserved_output_tokens
                + reserved_output,
                reserved_microusd=AiUsageBucket.reserved_microusd + reserved_cost,
                updated_at=datetime.now(UTC),
            )
        ),
    )
    if result.rowcount != 1:
        session.rollback()
        raise ConflictError("ai_daily_budget_exceeded", "AI daily budget is exhausted")

    operation = AiPreparationOperation(
        id=preparation_id,
        tenant_id=context.tenant_id,
        workflow_id=workflow.id,
        workflow_version_id=workflow_version.id,
        agent_version_id=agent_version.id,
        created_by=context.principal_id,
        status="queued",
        correlation_id=correlation_id,
        source_ref_hash=source_hash,
        sealed_input=sealed_input,
        encryption_key_id=key_id,
        provider=policy.provider,
        model=policy.model,
        prompt_id=prompt.identifier,
        prompt_hash=prompt.prompt_hash,
        tool_name=tool_name,
        usage_date=usage_date,
        input_token_bound=input_token_bound,
        reserved_input_tokens=reserved_input,
        reserved_output_tokens=reserved_output,
        reserved_microusd=reserved_cost,
        input_rate_microusd=input_rate,
        output_rate_microusd=output_rate,
    )
    try:
        session.add(operation)
        session.flush()
        session.add_all(
            [
                AiOutboxEvent(
                    id=str(uuid4()),
                    tenant_id=context.tenant_id,
                    preparation_id=preparation_id,
                    event_type="ai_preparation.requested",
                    status="queued",
                ),
                OrderSourceClaim(
                    id=str(uuid4()),
                    tenant_id=context.tenant_id,
                    source_connector=envelope.source_connector,
                    source_ref_hash=source_hash,
                    run_id=None,
                    ai_preparation_id=preparation_id,
                ),
            ]
        )
        session.flush()
        body = preparation_body(operation)
        session.add(
            V2AuditEvent(
                tenant_id=context.tenant_id,
                run_id=None,
                entity_type="ai_preparation",
                entity_id=preparation_id,
                event_type="ai_preparation_queued",
                actor_id=context.principal_id,
                idempotency_key=idempotency_key,
                payload={
                    "correlation_id": correlation_id,
                    "provider": policy.provider,
                    "model": policy.model,
                    "prompt_hash": prompt.prompt_hash,
                    "tool_name": tool_name,
                    "status": "queued",
                },
            )
        )
        _remember(
            session,
            context,
            operation=operation_name,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            status=202,
            body=body,
            resource_id=preparation_id,
        )
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        replay = _find_replay(
            session,
            context,
            operation=operation_name,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            current = session.get(AiPreparationOperation, replay[1]["id"])
            return (
                202,
                replay[1]
                if current is None
                else _current_preparation_body(session, current),
            )
        claimed = session.scalar(
            select(OrderSourceClaim).where(
                OrderSourceClaim.tenant_id == context.tenant_id,
                OrderSourceClaim.source_connector == envelope.source_connector,
                OrderSourceClaim.source_ref_hash == source_hash,
            )
        )
        if claimed is not None:
            raise ConflictError(
                "order_source_already_claimed",
                "this source message already has a preparation",
            ) from exc
        raise
    return 202, body
