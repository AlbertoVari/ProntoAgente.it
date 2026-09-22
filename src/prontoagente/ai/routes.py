"""Additive v2 HTTP routes for governed asynchronous AI preparation."""

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from prontoagente.ai.schemas import (
    AiPolicyResponse,
    AiPolicyUpdate,
    AiPreparationRequest,
    AiPreparationResponse,
    AiUsageResponse,
)
from prontoagente.ai.service import (
    enqueue_preparation,
    get_policy,
    get_preparation,
    get_usage,
    put_policy,
)
from prontoagente.db import get_session
from prontoagente.v2.auth import (
    ALLOWED_ROLES,
    ROLE_AUDITOR,
    ROLE_OPERATOR,
    ROLE_OWNER,
    AuthContext,
    require_roles,
)

router = APIRouter()
SessionDependency = Annotated[Session, Depends(get_session)]
ViewerContext = Annotated[AuthContext, Depends(require_roles(*sorted(ALLOWED_ROLES)))]
PreparerContext = Annotated[
    AuthContext, Depends(require_roles(ROLE_OWNER, ROLE_OPERATOR))
]
OwnerContext = Annotated[AuthContext, Depends(require_roles(ROLE_OWNER))]
PolicyReaderContext = Annotated[
    AuthContext, Depends(require_roles(ROLE_OWNER, ROLE_AUDITOR))
]
IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]


@router.post(
    "/workflows/{workflow_id}/runs/from-mail/llm",
    response_model=AiPreparationResponse,
    status_code=202,
)
def prepare_from_mail(
    workflow_id: str,
    payload: AiPreparationRequest,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
    context: PreparerContext,
) -> Any:
    status, body = enqueue_preparation(
        session,
        context,
        workflow_id=workflow_id,
        request=payload,
        idempotency_key=idempotency_key,
    )
    return JSONResponse(
        status_code=status,
        content=body,
        headers={"Location": f"/v2/ai-preparations/{body['id']}"},
    )


@router.get("/ai-preparations/{preparation_id}", response_model=AiPreparationResponse)
def preparation(
    preparation_id: str,
    session: SessionDependency,
    context: ViewerContext,
) -> Any:
    return get_preparation(session, context, preparation_id)


@router.get("/ai/policy", response_model=AiPolicyResponse)
def policy(session: SessionDependency, context: PolicyReaderContext) -> Any:
    return get_policy(session, context)


@router.put("/ai/policy", response_model=AiPolicyResponse)
def configure_policy(
    payload: AiPolicyUpdate,
    idempotency_key: IdempotencyKey,
    session: SessionDependency,
    context: OwnerContext,
) -> Any:
    status, body = put_policy(
        session,
        context,
        payload,
        idempotency_key=idempotency_key,
    )
    return JSONResponse(status_code=status, content=body)


@router.get("/ai/usage", response_model=AiUsageResponse)
def usage(
    session: SessionDependency,
    context: PolicyReaderContext,
    usage_date: Annotated[date | None, Query()] = None,
) -> Any:
    return get_usage(session, context, usage_date=usage_date)
