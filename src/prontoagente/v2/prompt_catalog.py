"""Tenant prompt lifecycle. Published content is sealed by ORM and database triggers."""

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from prontoagente.ai.prompts import EMAIL_ORDER_EXTRACT_V1, _prompt
from prontoagente.errors import ConflictError, NotFoundError
from prontoagente.v2.auth import AuthContext
from prontoagente.v2.models import Prompt, PromptVersion
from prontoagente.v2.schemas import (
    PromptCreate,
    PromptVersionCreate,
    PromptVersionUpdate,
    PublishRequest,
)


def _body(prompt: Prompt) -> dict[str, Any]:
    return dict(
        id=prompt.id,
        slug=prompt.slug,
        name=prompt.name,
        description=prompt.description,
        created_by=prompt.created_by,
        created_at=prompt.created_at,
    )


def _version_body(prompt: Prompt, version: PromptVersion) -> dict[str, Any]:
    return dict(
        id=version.id,
        prompt_id=prompt.id,
        identifier=f"{prompt.slug}/v{version.version}",
        version=version.version,
        status=version.status,
        system_prompt=version.system_prompt,
        tool_name=version.tool_name,
        prompt_hash=version.prompt_hash,
        lock_version=version.lock_version,
        created_by=version.created_by,
        created_at=version.created_at,
        published_at=version.published_at,
    )


def _prompt_row(session: Session, context: AuthContext, prompt_id: str) -> Prompt:
    row = session.scalar(
        select(Prompt).where(Prompt.tenant_id == context.tenant_id, Prompt.id == prompt_id)
    )
    if row is None:
        raise NotFoundError("prompt_not_found", "prompt not found")
    return row


def _version(
    session: Session, context: AuthContext, prompt_id: str, version_id: str
) -> PromptVersion:
    row = session.scalar(
        select(PromptVersion).where(
            PromptVersion.tenant_id == context.tenant_id,
            PromptVersion.prompt_id == prompt_id,
            PromptVersion.id == version_id,
        )
    )
    if row is None:
        raise NotFoundError("prompt_version_not_found", "prompt version not found")
    return row


def list_prompts(session: Session, context: AuthContext) -> list[dict[str, Any]]:
    return [
        _body(row)
        for row in session.scalars(
            select(Prompt).where(Prompt.tenant_id == context.tenant_id).order_by(Prompt.created_at)
        ).all()
    ]


def create_prompt(session: Session, context: AuthContext, request: PromptCreate) -> dict[str, Any]:
    if request.slug == EMAIL_ORDER_EXTRACT_V1.name:
        raise ConflictError("reserved_prompt", "built-in prompt name is reserved")
    row = Prompt(
        id=str(uuid4()),
        tenant_id=context.tenant_id,
        slug=request.slug,
        name=request.name,
        description=request.description,
        created_by=context.principal_id,
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ConflictError("prompt_slug_exists", "prompt slug already exists") from exc
    return _body(row)


def get_prompt_catalog(session: Session, context: AuthContext, prompt_id: str) -> dict[str, Any]:
    row = _prompt_row(session, context, prompt_id)
    versions = session.scalars(
        select(PromptVersion)
        .where(PromptVersion.tenant_id == context.tenant_id, PromptVersion.prompt_id == prompt_id)
        .order_by(PromptVersion.version)
    ).all()
    return {**_body(row), "versions": [_version_body(row, v) for v in versions]}


def create_prompt_version(
    session: Session, context: AuthContext, prompt_id: str, request: PromptVersionCreate
) -> dict[str, Any]:
    row = _prompt_row(session, context, prompt_id)
    current = session.scalar(
        select(func.max(PromptVersion.version)).where(
            PromptVersion.tenant_id == context.tenant_id, PromptVersion.prompt_id == prompt_id
        )
    )
    version = PromptVersion(
        id=str(uuid4()),
        tenant_id=context.tenant_id,
        prompt_id=prompt_id,
        version=int(current or 0) + 1,
        status="draft",
        system_prompt=request.system_prompt,
        tool_name=request.tool_name,
        created_by=context.principal_id,
    )
    session.add(version)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ConflictError("version_conflict", "prompt version claimed concurrently") from exc
    return _version_body(row, version)


def update_prompt_version(
    session: Session,
    context: AuthContext,
    prompt_id: str,
    version_id: str,
    request: PromptVersionUpdate,
) -> dict[str, Any]:
    row = _prompt_row(session, context, prompt_id)
    version = _version(session, context, prompt_id, version_id)
    if version.status != "draft":
        raise ConflictError("published_version_immutable", "published versions are immutable")
    if version.lock_version != request.lock_version:
        raise ConflictError("optimistic_lock_conflict", "prompt version changed; reload and retry")
    version.system_prompt = request.system_prompt
    version.tool_name = request.tool_name
    try:
        session.commit()
    except StaleDataError as exc:
        session.rollback()
        raise ConflictError(
            "optimistic_lock_conflict", "prompt version changed; reload and retry"
        ) from exc
    return _version_body(row, version)


def publish_prompt_version(
    session: Session, context: AuthContext, prompt_id: str, version_id: str, request: PublishRequest
) -> dict[str, Any]:
    row = _prompt_row(session, context, prompt_id)
    version = _version(session, context, prompt_id, version_id)
    digest = _prompt(
        name=row.slug,
        version=f"v{version.version}",
        system_prompt=version.system_prompt,
        tool_name=version.tool_name,
    ).prompt_hash
    # The conditional update is atomic across workers and defeats publish/edit races.
    result = cast(
        CursorResult[Any],
        session.execute(
            update(PromptVersion)
            .where(
                PromptVersion.id == version.id,
                PromptVersion.tenant_id == context.tenant_id,
                PromptVersion.status == "draft",
                PromptVersion.lock_version == request.lock_version,
            )
            .values(
                status="published",
                published_at=datetime.now(UTC),
                prompt_hash=digest,
                lock_version=PromptVersion.lock_version + 1,
            )
        ),
    )
    if result.rowcount != 1:
        session.rollback()
        raise ConflictError("optimistic_lock_conflict", "prompt version changed; reload and retry")
    session.commit()
    session.refresh(version)
    return _version_body(row, version)
