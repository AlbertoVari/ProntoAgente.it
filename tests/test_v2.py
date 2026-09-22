from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event, Lock
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from prontoagente.config import get_settings
from prontoagente.v2.auth import create_api_key
from prontoagente.v2.models import (
    ApiKey,
    ExecutionOperation,
    OutboxEvent,
    Principal,
    Run,
    Tenant,
    V2AuditEvent,
    V2IdempotencyRecord,
    WorkflowVersion,
)
from prontoagente.worker import process_once


@dataclass(frozen=True)
class Identity:
    tenant_id: str
    principal_id: str
    token: str


def create_identity(
    session_factory: sessionmaker[Session],
    *,
    marker: str,
    roles: list[str],
    tenant_id: str | None = None,
    expires_at: datetime | None = None,
    revoked: bool = False,
) -> Identity:
    with session_factory() as session:
        resolved_tenant_id = tenant_id or str(uuid4())
        tenant = session.get(Tenant, resolved_tenant_id)
        if tenant is None:
            tenant = Tenant(
                id=resolved_tenant_id,
                slug=f"tenant-{marker}",
                name=f"Tenant {marker}",
                status="active",
            )
            session.add(tenant)
            session.flush()
        principal = Principal(
            id=str(uuid4()),
            tenant_id=tenant.id,
            subject=f"{marker}@example.test",
            display_name=marker,
            roles=roles,
            status="active",
        )
        session.add(principal)
        session.flush()
        record, token = create_api_key(
            session,
            tenant_id=tenant.id,
            principal_id=principal.id,
            expires_at=expires_at,
        )
        if revoked:
            record.revoked_at = datetime.now(UTC)
        session.commit()
        return Identity(tenant_id=tenant.id, principal_id=principal.id, token=token)


def auth(identity: Identity, *, key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {identity.token}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def publish_stack(
    client: TestClient,
    identity: Identity,
    *,
    marker: str,
    connector: str = "simulated_erp",
    action: str = "create_sales_order",
    config: dict[str, Any] | None = None,
    approval_required: bool = True,
) -> dict[str, Any]:
    agent = client.post(
        "/v2/agents",
        headers=auth(identity),
        json={"slug": f"agent-{marker}", "name": f"Agent {marker}"},
    )
    assert agent.status_code == 201, agent.text
    agent_version = client.post(
        f"/v2/agents/{agent.json()['id']}/versions",
        headers=auth(identity),
        json={"definition": {"instructions": f"Do {marker}"}},
    )
    assert agent_version.status_code == 201, agent_version.text
    published_agent = client.post(
        f"/v2/agents/{agent.json()['id']}/versions/{agent_version.json()['id']}/publish",
        headers=auth(identity),
        json={"lock_version": agent_version.json()["lock_version"]},
    )
    assert published_agent.status_code == 200, published_agent.text

    workflow = client.post(
        "/v2/workflows",
        headers=auth(identity),
        json={"slug": f"workflow-{marker}", "name": f"Workflow {marker}"},
    )
    assert workflow.status_code == 201, workflow.text
    workflow_version = client.post(
        f"/v2/workflows/{workflow.json()['id']}/versions",
        headers=auth(identity),
        json={
            "agent_version_id": published_agent.json()["id"],
            "connector": connector,
            "action": action,
            "config": config or {},
            "input_schema": {"type": "object"},
            "approval_required": approval_required,
        },
    )
    assert workflow_version.status_code == 201, workflow_version.text
    published_workflow = client.post(
        f"/v2/workflows/{workflow.json()['id']}/versions/"
        f"{workflow_version.json()['id']}/publish",
        headers=auth(identity),
        json={"lock_version": workflow_version.json()["lock_version"]},
    )
    assert published_workflow.status_code == 200, published_workflow.text
    return {
        "agent": agent.json(),
        "agent_version": published_agent.json(),
        "workflow": workflow.json(),
        "workflow_version": published_workflow.json(),
    }


def create_run(
    client: TestClient,
    identity: Identity,
    workflow_id: str,
    *,
    key: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = client.post(
        f"/v2/workflows/{workflow_id}/runs/dry-run",
        headers=auth(identity, key=key),
        json={"input": payload or {"order_id": "ORDER-1"}},
    )
    assert response.status_code == 201, response.text
    return response.json()


def approve(client: TestClient, identity: Identity, run: dict[str, Any], key: str) -> None:
    response = client.post(
        f"/v2/runs/{run['id']}/approve",
        headers=auth(identity, key=key),
        json={"proposal_hash": run["proposal_hash"]},
    )
    assert response.status_code == 200, response.text


def test_v2_api_key_authentication_revocation_expiry_and_digest_only(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    assert client.get("/v2/me").status_code == 401
    assert client.get("/v2/me", headers={"Authorization": "Bearer bad"}).status_code == 401

    valid = create_identity(session_factory, marker="valid", roles=["owner"])
    response = client.get("/v2/me", headers=auth(valid))
    assert response.status_code == 200
    assert response.json()["tenant_id"] == valid.tenant_id
    assert response.json()["roles"] == ["owner"]

    key_id, plaintext_secret = valid.token.removeprefix("pa2_").split(".", 1)
    with session_factory() as session:
        record = session.scalar(select(ApiKey).where(ApiKey.key_id == key_id))
        assert record is not None
        assert plaintext_secret not in record.secret_digest
        assert plaintext_secret not in record.prefix
        assert len(record.secret_digest) == 64

    revoked = create_identity(
        session_factory, marker="revoked", roles=["owner"], revoked=True
    )
    expired = create_identity(
        session_factory,
        marker="expired",
        roles=["owner"],
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert client.get("/v2/me", headers=auth(revoked)).status_code == 401
    assert client.get("/v2/me", headers=auth(expired)).status_code == 401


@pytest.mark.parametrize(
    ("roles", "expected"),
    [
        (["owner"], 201),
        (["builder"], 201),
        (["operator"], 403),
        (["approver"], 403),
        (["auditor"], 403),
    ],
)
def test_catalog_write_rbac_matrix(
    client: TestClient,
    session_factory: sessionmaker[Session],
    roles: list[str],
    expected: int,
) -> None:
    marker = roles[0]
    identity = create_identity(session_factory, marker=marker, roles=roles)
    response = client.post(
        "/v2/agents",
        headers=auth(identity),
        json={"slug": f"rbac-{marker}", "name": marker},
    )
    assert response.status_code == expected


def test_catalog_versioning_immutability_pinning_and_cross_tenant_404(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    owner = create_identity(session_factory, marker="catalog-owner", roles=["owner"])
    other = create_identity(session_factory, marker="catalog-other", roles=["owner"])
    agent = client.post(
        "/v2/agents",
        headers=auth(owner),
        json={"slug": "catalog-agent", "name": "Catalog agent"},
    ).json()
    assert client.get(f"/v2/agents/{agent['id']}", headers=auth(other)).status_code == 404

    version = client.post(
        f"/v2/agents/{agent['id']}/versions",
        headers=auth(owner),
        json={"definition": {"revision": 1}},
    ).json()
    updated = client.patch(
        f"/v2/agents/{agent['id']}/versions/{version['id']}",
        headers=auth(owner),
        json={"lock_version": version["lock_version"], "definition": {"revision": 2}},
    )
    assert updated.status_code == 200
    published = client.post(
        f"/v2/agents/{agent['id']}/versions/{version['id']}/publish",
        headers=auth(owner),
        json={"lock_version": updated.json()["lock_version"]},
    )
    assert published.status_code == 200
    immutable = client.patch(
        f"/v2/agents/{agent['id']}/versions/{version['id']}",
        headers=auth(owner),
        json={
            "lock_version": published.json()["lock_version"],
            "definition": {"revision": 3},
        },
    )
    assert immutable.status_code == 409

    next_version = client.post(
        f"/v2/agents/{agent['id']}/versions",
        headers=auth(owner),
        json={"definition": {"revision": 3}},
    )
    assert next_version.status_code == 201
    assert next_version.json()["version"] == 2

    workflow = client.post(
        "/v2/workflows",
        headers=auth(owner),
        json={"slug": "catalog-workflow", "name": "Catalog workflow"},
    ).json()
    cannot_pin_draft = client.post(
        f"/v2/workflows/{workflow['id']}/versions",
        headers=auth(owner),
        json={
            "agent_version_id": next_version.json()["id"],
            "connector": "simulated_erp",
            "action": "create_sales_order",
        },
    )
    assert cannot_pin_draft.status_code == 404


def test_workflow_versions_always_require_approval_at_api_and_database(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    owner = create_identity(session_factory, marker="approval-first", roles=["owner"])
    stack = publish_stack(client, owner, marker="approval-first")
    workflow_id = stack["workflow"]["id"]
    version_payload = {
        "agent_version_id": stack["agent_version"]["id"],
        "connector": "simulated_erp",
        "action": "create_sales_order",
        "config": {},
        "input_schema": {"type": "object"},
        "approval_required": False,
    }
    rejected = client.post(
        f"/v2/workflows/{workflow_id}/versions",
        headers=auth(owner),
        json=version_payload,
    )
    assert rejected.status_code == 422

    version_payload["approval_required"] = True
    draft = client.post(
        f"/v2/workflows/{workflow_id}/versions",
        headers=auth(owner),
        json=version_payload,
    )
    assert draft.status_code == 201
    with session_factory() as session:
        with pytest.raises(IntegrityError):
            session.execute(
                update(WorkflowVersion)
                .where(WorkflowVersion.id == draft.json()["id"])
                .values(approval_required=False)
            )
            session.commit()
        session.rollback()


def test_concurrent_approve_vs_reject_has_one_winner_and_one_decision_event(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    owner = create_identity(session_factory, marker="decision-race", roles=["owner"])
    stack = publish_stack(client, owner, marker="decision-race")
    run = create_run(
        client, owner, stack["workflow"]["id"], key="decision-race-dry"
    )
    start = Barrier(2)

    def approve_request() -> int:
        start.wait(timeout=5)
        return client.post(
            f"/v2/runs/{run['id']}/approve",
            headers=auth(owner, key="decision-race-approve"),
            json={"proposal_hash": run["proposal_hash"]},
        ).status_code

    def reject_request() -> int:
        start.wait(timeout=5)
        return client.post(
            f"/v2/runs/{run['id']}/reject",
            headers=auth(owner, key="decision-race-reject"),
            json={"reason": "Rejected by the concurrent reviewer"},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = sorted(
            [executor.submit(approve_request), executor.submit(reject_request)],
            key=id,
        )
        resolved_statuses = sorted(item.result(timeout=10) for item in statuses)
    assert resolved_statuses == [200, 409]

    with session_factory() as session:
        persisted = session.get(Run, run["id"])
        assert persisted is not None
        assert persisted.status in {"approved", "rejected"}
        if persisted.status == "approved":
            assert persisted.approved_by == owner.principal_id
            assert persisted.approved_at is not None
            assert persisted.rejected_by is None
            assert persisted.rejected_at is None
            assert persisted.rejection_reason is None
        else:
            assert persisted.approved_by is None
            assert persisted.approved_at is None
            assert persisted.rejected_by == owner.principal_id
            assert persisted.rejected_at is not None
            assert persisted.rejection_reason is not None
        decision_events = session.scalars(
            select(V2AuditEvent).where(
                V2AuditEvent.run_id == run["id"],
                V2AuditEvent.event_type.in_(("run_approved", "run_rejected")),
            )
        ).all()
        assert len(decision_events) == 1
        decision_records = session.scalars(
            select(V2IdempotencyRecord).where(
                V2IdempotencyRecord.resource_id == run["id"],
                V2IdempotencyRecord.operation.in_(("run.approve", "run.reject")),
            )
        ).all()
        assert len(decision_records) == 1

        contradictory_values = (
            {
                "rejected_by": owner.principal_id,
                "rejected_at": datetime.now(UTC),
                "rejection_reason": "must be rejected by the database",
            }
            if persisted.status == "approved"
            else {
                "approved_by": owner.principal_id,
                "approved_at": datetime.now(UTC),
            }
        )
        with pytest.raises(IntegrityError):
            session.execute(
                update(Run)
                .where(Run.id == run["id"])
                .values(**contradictory_values)
            )
            session.commit()
        session.rollback()


def test_v2_run_approval_outbox_worker_and_tenant_idempotency(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    owner = create_identity(session_factory, marker="run-owner", roles=["owner"])
    stack = publish_stack(client, owner, marker="run")
    run = create_run(
        client, owner, stack["workflow"]["id"], key="tenant-shared-dry-run"
    )
    replay = client.post(
        f"/v2/workflows/{stack['workflow']['id']}/runs/dry-run",
        headers=auth(owner, key="tenant-shared-dry-run"),
        json={"input": {"order_id": "ORDER-1"}},
    )
    assert replay.status_code == 201
    assert replay.json() == run

    wrong_hash = client.post(
        f"/v2/runs/{run['id']}/approve",
        headers=auth(owner, key="run-wrong-hash"),
        json={"proposal_hash": "sha256:" + ("0" * 64)},
    )
    assert wrong_hash.status_code == 409
    approve(client, owner, run, "run-approve-key")

    def no_socket(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("HTTP request path must not perform network I/O")

    monkeypatch.setattr("socket.create_connection", no_socket)
    execution = client.post(
        f"/v2/runs/{run['id']}/execute",
        headers=auth(owner, key="run-execute-key"),
    )
    assert execution.status_code == 202
    operation = execution.json()
    assert operation["status"] == "pending"
    assert execution.headers["location"].endswith(operation["operation_id"])
    execution_replay = client.post(
        f"/v2/runs/{run['id']}/execute",
        headers=auth(owner, key="run-execute-key"),
    )
    assert execution_replay.status_code == 202
    assert execution_replay.json() == operation

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ExecutionOperation)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1
    assert process_once(session_factory)

    final = client.get(
        f"/v2/execution-operations/{operation['operation_id']}", headers=auth(owner)
    )
    assert final.status_code == 200
    assert final.json()["status"] == "executed"
    final_replay = client.post(
        f"/v2/runs/{run['id']}/execute",
        headers=auth(owner, key="run-execute-key"),
    )
    assert final_replay.status_code == 200
    assert final_replay.json() == final.json()
    audit = client.get(f"/v2/runs/{run['id']}/audit-events", headers=auth(owner))
    assert [event["event_type"] for event in audit.json()] == [
        "run_proposed",
        "run_approved",
        "execution_requested",
        "execution_completed",
    ]

    second_owner = create_identity(
        session_factory, marker="run-second-tenant", roles=["owner"]
    )
    second_stack = publish_stack(client, second_owner, marker="run-second")
    second_run = create_run(
        client,
        second_owner,
        second_stack["workflow"]["id"],
        key="tenant-shared-dry-run",
    )
    assert second_run["id"] != run["id"]


def test_run_operation_and_audit_are_hidden_from_other_tenants(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    owner = create_identity(session_factory, marker="isolation-owner", roles=["owner"])
    other = create_identity(session_factory, marker="isolation-other", roles=["owner"])
    stack = publish_stack(client, owner, marker="isolation")
    run = create_run(client, owner, stack["workflow"]["id"], key="isolation-dry")
    approve(client, owner, run, "isolation-approve")
    execution = client.post(
        f"/v2/runs/{run['id']}/execute",
        headers=auth(owner, key="isolation-execute"),
    )
    assert execution.status_code == 202

    assert client.get(f"/v2/runs/{run['id']}", headers=auth(other)).status_code == 404
    assert (
        client.get(
            f"/v2/execution-operations/{execution.json()['operation_id']}",
            headers=auth(other),
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/v2/runs/{run['id']}/audit-events", headers=auth(other)
        ).status_code
        == 404
    )


def test_m365_is_configured_only_for_the_explicit_bound_platform_tenant(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bound = create_identity(
        session_factory,
        marker="m365-bound",
        roles=["owner"],
        tenant_id="m365-bound-platform-tenant",
    )
    other = create_identity(session_factory, marker="m365-unbound", roles=["owner"])
    settings = replace(
        get_settings(),
        m365_connector_enabled=True,
        m365_platform_tenant_id=bound.tenant_id,
        m365_permission_attestation="Mail.ReadBasic.All",
        m365_tenant_id="entra-tenant",
        m365_client_id="client-id",
        m365_client_secret="client-secret",
        m365_mailbox_id="mailbox@example.test",
        m365_folder_id="Inbox",
    )
    monkeypatch.setattr("prontoagente.v2.connectors.get_settings", lambda: settings)
    monkeypatch.setattr("prontoagente.v2.runs.get_settings", lambda: settings)

    bound_catalog = client.get("/v2/connectors", headers=auth(bound)).json()
    other_catalog = client.get("/v2/connectors", headers=auth(other)).json()
    assert next(item for item in bound_catalog if item["name"] == "m365_mail_intake_v1")[
        "configured"
    ]
    assert not next(
        item for item in other_catalog if item["name"] == "m365_mail_intake_v1"
    )["configured"]

    bound_stack = publish_stack(
        client,
        bound,
        marker="m365-bound",
        connector="m365_mail_intake_v1",
        action="list_messages",
    )
    bound_run = create_run(
        client, bound, bound_stack["workflow"]["id"], key="m365-bound-dry"
    )
    approve(client, bound, bound_run, "m365-bound-approve")
    bound_execute = client.post(
        f"/v2/runs/{bound_run['id']}/execute",
        headers=auth(bound, key="m365-bound-execute"),
    )
    assert bound_execute.status_code == 202

    other_stack = publish_stack(
        client,
        other,
        marker="m365-unbound",
        connector="m365_mail_intake_v1",
        action="list_messages",
    )
    other_run = create_run(
        client, other, other_stack["workflow"]["id"], key="m365-unbound-dry"
    )
    approve(client, other, other_run, "m365-unbound-approve")
    other_execute = client.post(
        f"/v2/runs/{other_run['id']}/execute",
        headers=auth(other, key="m365-unbound-execute"),
    )
    assert other_execute.status_code == 409
    assert other_execute.json()["detail"]["code"] == "connector_not_configured"
    with session_factory() as session:
        other_outbox_count = session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.tenant_id == other.tenant_id)
        )
        assert other_outbox_count == 0


def test_outbox_lease_prevents_duplicate_connector_calls(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    owner = create_identity(session_factory, marker="worker-owner", roles=["owner"])
    stack = publish_stack(client, owner, marker="worker")
    run = create_run(client, owner, stack["workflow"]["id"], key="worker-dry-run")
    approve(client, owner, run, "worker-approve")
    requested = client.post(
        f"/v2/runs/{run['id']}/execute",
        headers=auth(owner, key="worker-execute"),
    )
    assert requested.status_code == 202

    started = Event()
    release = Event()
    counter_lock = Lock()
    call_count = 0

    class BlockingConnector:
        def execute(
            self, *, action: str, payload: dict[str, Any], operation_id: str
        ) -> dict[str, Any]:
            del action, payload, operation_id
            nonlocal call_count
            with counter_lock:
                call_count += 1
            started.set()
            assert release.wait(timeout=5)
            return {"ok": True}

    def factory(_name: str) -> BlockingConnector:
        return BlockingConnector()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(process_once, session_factory, connector_factory=factory)
        assert started.wait(timeout=5)
        second = executor.submit(process_once, session_factory, connector_factory=factory)
        assert second.result(timeout=5) is False
        assert call_count == 1
        release.set()
        assert first.result(timeout=5) is True
    assert call_count == 1


def test_worker_discards_a_result_after_lease_ownership_changes(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    owner = create_identity(session_factory, marker="stale-worker", roles=["owner"])
    stack = publish_stack(client, owner, marker="stale-worker")
    run = create_run(client, owner, stack["workflow"]["id"], key="stale-worker-dry")
    approve(client, owner, run, "stale-worker-approve")
    requested = client.post(
        f"/v2/runs/{run['id']}/execute",
        headers=auth(owner, key="stale-worker-execute"),
    )
    assert requested.status_code == 202

    started = Event()
    release = Event()

    class BlockingConnector:
        def execute(
            self, *, action: str, payload: dict[str, Any], operation_id: str
        ) -> dict[str, Any]:
            del action, payload, operation_id
            started.set()
            assert release.wait(timeout=5)
            return {"must_not_be_committed": True}

    def factory(_name: str) -> BlockingConnector:
        return BlockingConnector()

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            process_once,
            session_factory,
            connector_factory=factory,
            worker_id="worker-that-lost-its-lease",
        )
        assert started.wait(timeout=5)
        with session_factory() as session:
            outbox = session.scalar(
                select(OutboxEvent).where(OutboxEvent.tenant_id == owner.tenant_id)
            )
            assert outbox is not None
            outbox.lease_owner = "replacement-worker"
            session.commit()
        release.set()
        assert pending.result(timeout=5) is True

    with session_factory() as session:
        operation = session.get(
            ExecutionOperation, requested.json()["operation_id"]
        )
        persisted_run = session.get(Run, run["id"])
        outbox = session.scalar(
            select(OutboxEvent).where(OutboxEvent.tenant_id == owner.tenant_id)
        )
        assert operation is not None and operation.status == "executing"
        assert persisted_run is not None and persisted_run.status == "execution_pending"
        assert outbox is not None and outbox.status == "processing"
        assert outbox.lease_owner == "replacement-worker"
        completed = session.scalar(
            select(func.count())
            .select_from(V2AuditEvent)
            .where(
                V2AuditEvent.run_id == run["id"],
                V2AuditEvent.event_type == "execution_completed",
            )
        )
        assert completed == 0


def test_m365_disabled_is_409_without_outbox_or_network(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    owner = create_identity(session_factory, marker="m365-owner", roles=["owner"])
    stack = publish_stack(
        client,
        owner,
        marker="m365",
        connector="m365_mail_intake_v1",
        action="list_messages",
        config={"top": 10},
    )
    run = create_run(
        client,
        owner,
        stack["workflow"]["id"],
        key="m365-disabled-dry",
        payload={"top": 5},
    )
    approve(client, owner, run, "m365-disabled-approve")

    def no_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("disabled connector must not use network")

    monkeypatch.setattr("socket.create_connection", no_network)
    response = client.post(
        f"/v2/runs/{run['id']}/execute",
        headers=auth(owner, key="m365-disabled-execute"),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "connector_not_configured"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 0


def test_v2_openapi_has_bearer_scheme_and_paths(client: TestClient) -> None:
    document = client.get("/openapi.json").json()
    assert "ProntoAgenteV2ApiKey" in document["components"]["securitySchemes"]
    assert "/v2/agents" in document["paths"]
    assert "/v2/workflows/{workflow_id}/runs/dry-run" in document["paths"]
    assert "/v2/runs/{run_id}/execute" in document["paths"]
