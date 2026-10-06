"""Manual PostgreSQL smoke test. Never collected by pytest or invoked by CI."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGES = ("preflight", "upgrade", "check", "guards", "race")


def worker(stage: str) -> None:
    from sqlalchemy import create_engine, event, inspect, select, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.orm import sessionmaker

    url = make_url(os.environ["POSTGRES_SMOKE_DATABASE_URL"])
    if url.drivername != "postgresql+psycopg" or not url.database:
        raise RuntimeError("unsupported target")
    url = url.update_query_dict(
        {"options": "-c search_path=public -c statement_timeout=15000 -c lock_timeout=10000"}
    )
    engine = create_engine(
        url,
        echo=False,
        connect_args={"connect_timeout": 10},
    )
    try:
        with engine.connect() as conn:
            if conn.scalar(text("SELECT current_schema()")) != "public":
                raise RuntimeError("public schema required")
            if stage == "preflight":
                if inspect(conn).get_table_names(schema="public") or conn.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n "
                        "ON n.oid=c.relnamespace WHERE n.nspname NOT LIKE 'pg_%' "
                        "AND n.nspname <> 'information_schema' "
                        "AND c.relkind IN ('r','v','m','S','f'))"
                    )
                ):
                    raise RuntimeError("empty database required")
                return
        if stage in ("upgrade", "check"):
            env = dict(os.environ, DATABASE_URL=url.render_as_string(hide_password=False))
            # Alembic's ConfigParser requires escaped percent signs (encoded passwords).
            env["DATABASE_URL"] = env["DATABASE_URL"].replace("%", "%%")
            command = ["upgrade", "head"] if stage == "upgrade" else ["check"]
            result = subprocess.run(
                [sys.executable, "-m", "alembic", *command],
                cwd=ROOT,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=90,
            )
            if result.returncode:
                raise RuntimeError("migration command failed")
            return

        from concurrent.futures import ThreadPoolExecutor
        from datetime import UTC, date, datetime
        from threading import Barrier

        from prontoagente.ai.models import AiPreparationOperation
        from prontoagente.errors import ConflictError
        from prontoagente.v2.auth import AuthContext
        from prontoagente.v2.models import (
            Agent,
            AgentVersion,
            Principal,
            Prompt,
            PromptVersion,
            Tenant,
            Workflow,
            WorkflowVersion,
        )
        from prontoagente.v2.prompt_catalog import publish_prompt_version
        from prontoagente.v2.schemas import PublishRequest

        factory = sessionmaker(engine)
        if stage == "guards":
            # Core INSERT/UPDATE bypass ORM guards; only migrated DB triggers can reject them.
            with engine.begin() as conn:
                conn.execute(
                    Tenant.__table__.insert().values(id="pg-smoke", slug="pg-smoke", name="Smoke")
                )
                conn.execute(
                    Principal.__table__.insert().values(
                        id="pg-owner",
                        tenant_id="pg-smoke",
                        subject="smoke",
                        display_name="Smoke",
                        roles=["owner"],
                    )
                )
                conn.execute(
                    Prompt.__table__.insert().values(
                        id="pg-prompt",
                        tenant_id="pg-smoke",
                        slug="smoke",
                        name="Smoke",
                        created_by="pg-owner",
                    )
                )
                for ident, status, number in (
                    ("pg-published", "published", 1),
                    ("pg-race", "draft", 2),
                ):
                    conn.execute(
                        PromptVersion.__table__.insert().values(
                            id=ident,
                            tenant_id="pg-smoke",
                            prompt_id="pg-prompt",
                            version=number,
                        status=status,
                        prompt_hash="sha256:" + "0" * 64 if status == "published" else None,
                        published_at=datetime.now(UTC) if status == "published" else None,
                            system_prompt="Synthetic",
                            tool_name="demo_erp_reconcile_v1",
                            created_by="pg-owner",
                        )
                    )
                conn.execute(
                    Agent.__table__.insert().values(
                        id="pg-agent",
                        tenant_id="pg-smoke",
                        slug="smoke",
                        name="Smoke",
                        created_by="pg-owner",
                    )
                )
                conn.execute(
                    AgentVersion.__table__.insert().values(
                        id="pg-av",
                        tenant_id="pg-smoke",
                        agent_id="pg-agent",
                        version=1,
                        status="published",
                        definition={},
                        created_by="pg-owner",
                    )
                )
                conn.execute(
                    Workflow.__table__.insert().values(
                        id="pg-workflow",
                        tenant_id="pg-smoke",
                        slug="smoke",
                        name="Smoke",
                        created_by="pg-owner",
                    )
                )
                conn.execute(
                    WorkflowVersion.__table__.insert().values(
                        id="pg-wv",
                        tenant_id="pg-smoke",
                        workflow_id="pg-workflow",
                        version=1,
                        status="published",
                        agent_version_id="pg-av",
                        connector="simulated_erp",
                        action="create_sales_order",
                        config={},
                        input_schema={},
                        approval_required=True,
                        created_by="pg-owner",
                    )
                )
                conn.execute(
                    AiPreparationOperation.__table__.insert().values(
                        id="pg-prep",
                        tenant_id="pg-smoke",
                        workflow_id="pg-workflow",
                        workflow_version_id="pg-wv",
                        agent_version_id="pg-av",
                        created_by="pg-owner",
                        correlation_id="pg-correlation",
                        source_ref_hash="synthetic",
                        sealed_input=b"synthetic",
                        encryption_key_id="smoke",
                        provider="fake",
                        model="fake-pa1-v1",
                        prompt_id="smoke/v1",
                        prompt_hash="synthetic",
                        prompt_snapshot={"system_prompt": "Synthetic"},
                        tool_name="demo_erp_reconcile_v1",
                        usage_date=date.today(),
                        input_token_bound=1,
                        reserved_input_tokens=1,
                        reserved_output_tokens=1,
                        reserved_microusd=0,
                        input_rate_microusd=0,
                        output_rate_microusd=0,
                    )
                )

            def rejected(sql: str) -> None:
                with engine.connect() as conn:
                    before_prompt = conn.execute(
                        text("SELECT * FROM prompt_versions WHERE id='pg-published'")
                    ).one()
                    before_prep = conn.execute(
                        text("SELECT * FROM ai_preparation_operations WHERE id='pg-prep'")
                    ).one()
                    try:
                        conn.execute(text(sql))
                    except DBAPIError as exc:
                        # P0001 is RAISE EXCEPTION, not a FK/CHECK or connection failure.
                        if getattr(exc.orig, "sqlstate", None) != "P0001":
                            raise
                        expected = (
                            "immutable record" if "ai_preparation_operations" in sql
                            else "published versions are immutable"
                        )
                        if getattr(exc.orig.diag, "message_primary", None) != expected:
                            raise
                        conn.rollback()
                    else:
                        conn.rollback()
                        raise RuntimeError("mutation accepted")
                    if (
                        conn.execute(
                            text("SELECT * FROM prompt_versions WHERE id='pg-published'")
                        ).one()
                        != before_prompt
                    ):
                        raise RuntimeError("published row changed")
                    if (
                        conn.execute(
                            text("SELECT * FROM ai_preparation_operations WHERE id='pg-prep'")
                        ).one()
                        != before_prep
                    ):
                        raise RuntimeError("preparation changed")

            for table, ident, fields in (
                (
                    "prompt_versions",
                    "pg-published",
                    ("system_prompt", "status", "prompt_hash", "tool_name"),
                ),
                (
                    "ai_preparation_operations",
                    "pg-prep",
                    ("prompt_id", "prompt_hash", "prompt_snapshot", "tool_name"),
                ),
            ):
                for field in fields:
                    value = (
                        "'{}'::json"
                        if field == "prompt_snapshot"
                        else ("'draft'" if field == "status" else "'tamper'")
                    )
                    rejected(f"UPDATE {table} SET {field}={value} WHERE id='{ident}'")
            rejected("DELETE FROM prompt_versions WHERE id='pg-published'")
            # Control: mutable lifecycle columns remain writable.
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE ai_preparation_operations SET status='processing' "
                        "WHERE id='pg-prep'"
                    )
                )
            return

        barrier = Barrier(2, timeout=10)

        def synchronize(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.startswith("UPDATE prompt_versions"):
                barrier.wait()

        event.listen(engine, "before_cursor_execute", synchronize)
        context = AuthContext(
            tenant_id="pg-smoke",
            principal_id="pg-owner",
            subject="smoke",
            display_name="Smoke",
            roles=frozenset({"owner"}),
            api_key_id="smoke",
        )

        def publish() -> str:
            with factory() as session:
                try:
                    publish_prompt_version(
                        session, context, "pg-prompt", "pg-race", PublishRequest(lock_version=1)
                    )
                    return "published"
                except ConflictError:
                    return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: publish(), range(2)))
        event.remove(engine, "before_cursor_execute", synchronize)
        if sorted(results) != ["conflict", "published"]:
            raise RuntimeError("unexpected race result")
        with factory() as session:
            row = session.scalar(select(PromptVersion).where(PromptVersion.id == "pg-race"))
            if row is None or row.status != "published" or row.lock_version != 2:
                raise RuntimeError("unexpected persisted result")
            if not row.prompt_hash or not row.prompt_hash.startswith("sha256:"):
                raise RuntimeError("missing publication hash")
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-disposable-database", action="store_true")
    args = parser.parse_args()
    if not args.confirm_disposable_database:
        print("FAIL confirmation_required")
        return 2
    if os.environ.get("CI") or os.environ.get("APP_ENV", "").lower() == "production":
        print("FAIL manual_nonproduction_only")
        return 2
    if not os.environ.get("POSTGRES_SMOKE_DATABASE_URL"):
        print("FAIL database_url_required")
        return 2
    for stage in STAGES:
        try:
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--worker", stage],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired):
            print(f"FAIL {stage}")
            return 1
        if result.returncode:
            print(f"FAIL {stage}")
            return 1
        print(f"PASS {stage}")
    print("PASS postgres_smoke")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        # Internal child entry cannot bypass the parent's explicit confirmation.
        if os.environ.get("_POSTGRES_SMOKE_CONFIRMED") != "yes" or sys.argv[2] not in STAGES:
            sys.exit(2)
        worker(sys.argv[2])
    else:
        # Children inherit this only after main has checked the command-line opt-in.
        if "--confirm-disposable-database" in sys.argv:
            os.environ["_POSTGRES_SMOKE_CONFIRMED"] = "yes"
        sys.exit(main())
