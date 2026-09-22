"""Idempotently bootstrap one tenant, owner principal, and first API key."""

import argparse
import json
from uuid import uuid4

from sqlalchemy import select

from prontoagente.db import SessionLocal
from prontoagente.v2.auth import ROLE_OWNER, create_api_key
from prontoagente.v2.models import ApiKey, Principal, Tenant


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-slug", required=True)
    parser.add_argument("--tenant-name", required=True)
    parser.add_argument("--owner-subject", required=True)
    parser.add_argument("--owner-name", required=True)
    args = parser.parse_args()

    with SessionLocal() as session:
        tenant = session.scalar(select(Tenant).where(Tenant.slug == args.tenant_slug))
        tenant_created = tenant is None
        if tenant is None:
            tenant = Tenant(
                id=str(uuid4()),
                slug=args.tenant_slug,
                name=args.tenant_name,
                status="active",
            )
            session.add(tenant)
            session.flush()

        principal = session.scalar(
            select(Principal).where(
                Principal.tenant_id == tenant.id,
                Principal.subject == args.owner_subject,
            )
        )
        principal_created = principal is None
        if principal is None:
            principal = Principal(
                id=str(uuid4()),
                tenant_id=tenant.id,
                subject=args.owner_subject,
                display_name=args.owner_name,
                roles=[ROLE_OWNER],
                status="active",
            )
            session.add(principal)
            session.flush()

        existing_key = session.scalar(
            select(ApiKey).where(
                ApiKey.tenant_id == tenant.id,
                ApiKey.principal_id == principal.id,
                ApiKey.revoked_at.is_(None),
            )
        )
        token: str | None = None
        if existing_key is None:
            existing_key, token = create_api_key(
                session, tenant_id=tenant.id, principal_id=principal.id
            )
        session.commit()

        result = {
            "tenant_id": tenant.id,
            "principal_id": principal.id,
            "api_key_prefix": existing_key.prefix,
            "tenant_created": tenant_created,
            "principal_created": principal_created,
            "api_key_created": token is not None,
        }
        if token is not None:
            result["api_key"] = token
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

