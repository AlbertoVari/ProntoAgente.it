"""Idempotently create the tenant AI policy used by the M3 prototype."""

import argparse
import json

from sqlalchemy import select

from prontoagente.ai.models import AiTenantPolicy
from prontoagente.config import get_settings
from prontoagente.db import SessionLocal
from prontoagente.v2.models import Tenant


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-slug", required=True)
    parser.add_argument(
        "--enable-fake",
        action="store_true",
        help="enable the deterministic zero-network provider for this tenant",
    )
    args = parser.parse_args()
    settings = get_settings()

    with SessionLocal() as session:
        tenant = session.scalar(select(Tenant).where(Tenant.slug == args.tenant_slug))
        if tenant is None:
            raise SystemExit("tenant not found; run scripts/bootstrap_m2.py first")
        policy = session.get(AiTenantPolicy, tenant.id)
        created = policy is None
        if policy is None:
            policy = AiTenantPolicy(
                tenant_id=tenant.id,
                enabled=args.enable_fake,
                provider="fake",
                model="fake-pa1-v1",
                network_enabled=False,
                max_input_tokens=min(4096, settings.ai_hard_max_input_tokens),
                max_output_tokens=min(512, settings.ai_hard_max_output_tokens),
                max_run_microusd=0,
                daily_input_tokens=min(100_000, settings.ai_hard_daily_input_tokens),
                daily_output_tokens=min(25_000, settings.ai_hard_daily_output_tokens),
                daily_microusd=0,
            )
            session.add(policy)
            session.commit()
        result = {
            "tenant_id": tenant.id,
            "created": created,
            "enabled": policy.enabled,
            "provider": policy.provider,
            "model": policy.model,
            "network_enabled": policy.network_enabled,
            "lock_version": policy.lock_version,
        }
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
