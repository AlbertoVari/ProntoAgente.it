"""API-key authentication and role authorization for v2."""

import hashlib
import hmac
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from prontoagente.config import Settings, get_settings
from prontoagente.db import get_session
from prontoagente.errors import AuthenticationError, AuthorizationError
from prontoagente.v2.models import ApiKey, Principal, Tenant

ROLE_OWNER = "owner"
ROLE_BUILDER = "builder"
ROLE_OPERATOR = "operator"
ROLE_APPROVER = "approver"
ROLE_AUDITOR = "auditor"
ALLOWED_ROLES = frozenset(
    {ROLE_OWNER, ROLE_BUILDER, ROLE_OPERATOR, ROLE_APPROVER, ROLE_AUDITOR}
)
_TOKEN_PATTERN = re.compile(r"^Bearer pa2_([A-Za-z0-9]{8,32})\.([A-Za-z0-9_-]{32,128})$")


@dataclass(frozen=True, slots=True)
class AuthContext:
    tenant_id: str
    principal_id: str
    subject: str
    display_name: str
    roles: frozenset[str]
    api_key_id: str


def _pepper(settings: Settings) -> bytes:
    if settings.app_env == "production" and settings.api_key_pepper == "dev-only-change-me":
        raise RuntimeError("API_KEY_PEPPER must be configured in production")
    if not settings.api_key_pepper:
        raise RuntimeError("API_KEY_PEPPER must not be empty")
    return settings.api_key_pepper.encode("utf-8")


def secret_digest(*, key_id: str, secret: str, settings: Settings | None = None) -> str:
    resolved = settings or get_settings()
    message = f"{key_id}.{secret}".encode()
    return hmac.new(_pepper(resolved), message, hashlib.sha256).hexdigest()


def create_api_key(
    session: Session,
    *,
    tenant_id: str,
    principal_id: str,
    expires_at: datetime | None = None,
    settings: Settings | None = None,
) -> tuple[ApiKey, str]:
    """Create a credential and return its one-time plaintext representation."""

    key_id = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    token = f"pa2_{key_id}.{secret}"
    record = ApiKey(
        id=str(uuid4()),
        tenant_id=tenant_id,
        principal_id=principal_id,
        key_id=key_id,
        prefix=f"pa2_{key_id}",
        secret_digest=secret_digest(key_id=key_id, secret=secret, settings=settings),
        expires_at=expires_at,
    )
    session.add(record)
    return record, token


def _unauthorized() -> AuthenticationError:
    return AuthenticationError("invalid_api_key", "valid bearer credentials are required")


def authenticate_api_key(
    session: Session,
    authorization: str | None,
    *,
    settings: Settings | None = None,
) -> AuthContext:
    match = _TOKEN_PATTERN.fullmatch(authorization or "")
    if match is None:
        raise _unauthorized()
    key_id, secret = match.groups()
    record = session.scalar(select(ApiKey).where(ApiKey.key_id == key_id))
    if record is None:
        raise _unauthorized()
    calculated = secret_digest(key_id=key_id, secret=secret, settings=settings)
    if not hmac.compare_digest(calculated, record.secret_digest):
        raise _unauthorized()

    now = datetime.now(UTC)
    expires_at = record.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if record.revoked_at is not None or (expires_at is not None and expires_at <= now):
        raise _unauthorized()

    principal = session.get(Principal, record.principal_id)
    tenant = session.get(Tenant, record.tenant_id)
    if (
        principal is None
        or tenant is None
        or principal.tenant_id != record.tenant_id
        or principal.status != "active"
        or tenant.status != "active"
    ):
        raise _unauthorized()
    roles = frozenset(principal.roles)
    if not roles or not roles.issubset(ALLOWED_ROLES):
        raise _unauthorized()
    return AuthContext(
        tenant_id=record.tenant_id,
        principal_id=principal.id,
        subject=principal.subject,
        display_name=principal.display_name,
        roles=roles,
        api_key_id=record.id,
    )


SessionDependency = Annotated[Session, Depends(get_session)]
bearer_scheme = HTTPBearer(auto_error=False, scheme_name="ProntoAgenteV2ApiKey")


def authenticated_context(
    session: SessionDependency,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
) -> AuthContext:
    authorization = None
    if credentials is not None:
        authorization = f"{credentials.scheme} {credentials.credentials}"
    return authenticate_api_key(session, authorization)


def require_roles(*allowed_roles: str) -> Callable[[AuthContext], AuthContext]:
    allowed = frozenset(allowed_roles)
    if not allowed or not allowed.issubset(ALLOWED_ROLES):
        raise ValueError("require_roles needs one or more known roles")

    def authorize(
        context: Annotated[AuthContext, Depends(authenticated_context)],
    ) -> AuthContext:
        if context.roles.isdisjoint(allowed):
            raise AuthorizationError(
                "insufficient_role", "the authenticated principal cannot perform this action"
            )
        return context

    return authorize
