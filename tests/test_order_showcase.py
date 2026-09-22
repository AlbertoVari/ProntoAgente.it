import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from decimal import Decimal
from threading import Barrier
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from prontoagente.config import Settings, get_settings
from prontoagente.v2.auth import create_api_key
from prontoagente.v2.connectors import ConnectorExecutionError, runtime_connector
from prontoagente.v2.demo_connectors import (
    DemoOrderParseError,
    OrderDocument,
    OrderLine,
    parse_order_subject,
    reconcile_order,
)
from prontoagente.v2.models import (
    OrderSourceClaim,
    OutboxEvent,
    Principal,
    Run,
    Tenant,
    V2AuditEvent,
    V2IdempotencyRecord,
)
from prontoagente.worker import process_once

MATCH_SUBJECT = (
    "PA1;order=PO-1001;customer=CUST-42;currency=EUR;"
    "lines=SKU-A:2:49.90,SKU-B:1:10.00"
)
MISMATCH_SUBJECT = (
    "PA1;order=PO-1002;customer=CUST-42;currency=EUR;"
    "lines=SKU-A:2:49.90,SKU-B:1:10.00"
)


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
) -> Identity:
    with session_factory() as session:
        resolved_tenant_id = tenant_id or str(uuid4())
        tenant = session.get(Tenant, resolved_tenant_id)
        if tenant is None:
            tenant = Tenant(
                id=resolved_tenant_id,
                slug=f"showcase-{marker}",
                name=f"Showcase {marker}",
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
        _record, token = create_api_key(
            session, tenant_id=tenant.id, principal_id=principal.id
        )
        session.commit()
        return Identity(tenant.id, principal.id, token)


def auth(identity: Identity, *, key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {identity.token}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def publish_workflow(
    client: TestClient,
    owner: Identity,
    *,
    marker: str,
    connector: str = "simulated_erp",
    action: str = "create_sales_order",
) -> str:
    agent = client.post(
        "/v2/agents",
        headers=auth(owner),
        json={"slug": f"mail-agent-{marker}", "name": f"Mail agent {marker}"},
    )
    assert agent.status_code == 201, agent.text
    agent_version = client.post(
        f"/v2/agents/{agent.json()['id']}/versions",
        headers=auth(owner),
        json={"definition": {"purpose": "offline deterministic showcase"}},
    )
    assert agent_version.status_code == 201, agent_version.text
    published_agent = client.post(
        f"/v2/agents/{agent.json()['id']}/versions/"
        f"{agent_version.json()['id']}/publish",
        headers=auth(owner),
        json={"lock_version": agent_version.json()["lock_version"]},
    )
    assert published_agent.status_code == 200, published_agent.text

    workflow = client.post(
        "/v2/workflows",
        headers=auth(owner),
        json={"slug": f"mail-workflow-{marker}", "name": f"Mail workflow {marker}"},
    )
    assert workflow.status_code == 201, workflow.text
    workflow_version = client.post(
        f"/v2/workflows/{workflow.json()['id']}/versions",
        headers=auth(owner),
        json={
            "agent_version_id": published_agent.json()["id"],
            "connector": connector,
            "action": action,
            "config": {"target": "offline-demo-erp"} if connector == "simulated_erp" else {},
            "input_schema": {
                "type": "object",
                "required": ["order_id", "customer_id", "currency", "lines"],
            },
            "approval_required": True,
        },
    )
    assert workflow_version.status_code == 201, workflow_version.text
    published_workflow = client.post(
        f"/v2/workflows/{workflow.json()['id']}/versions/"
        f"{workflow_version.json()['id']}/publish",
        headers=auth(owner),
        json={"lock_version": workflow_version.json()["lock_version"]},
    )
    assert published_workflow.status_code == 200, published_workflow.text
    return str(workflow.json()["id"])


def envelope(
    *,
    marker: str,
    subject: str = MATCH_SUBJECT,
    sender: str = "private.sender@example.test",
) -> dict[str, str]:
    return {
        "source_connector": "demo_mailbox_v1",
        "message_id": f"message-{marker}",
        "internet_message_id": f"<{marker}@example.test>",
        "sender": sender,
        "subject": subject,
    }


def post_from_mail(
    client: TestClient,
    identity: Identity,
    workflow_id: str,
    *,
    key: str,
    mail: dict[str, str],
) -> Any:
    return client.post(
        f"/v2/workflows/{workflow_id}/runs/from-mail",
        headers=auth(identity, key=key),
        json={"envelope": mail},
    )


@pytest.fixture
def demo_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    settings = replace(
        get_settings(), enable_demo_connectors=True, app_env="test"
    )
    monkeypatch.setattr("prontoagente.v2.runs.get_settings", lambda: settings)
    return settings


@pytest.mark.parametrize(
    "subject",
    [
        "PA1;order=PO-1001;customer=CUST-42;currency=USD;lines=SKU-A:1:1.00",
        "PA1;customer=CUST-42;order=PO-1001;currency=EUR;lines=SKU-A:1:1.00",
        "PA1;order=PO-1001;customer=CUST-42;currency=EUR;lines=sku-a:1:1.00",
        "PA1;order=PO-1001;customer=CUST-42;currency=EUR;lines=SKU-A:0:1.00",
        "PA1;order=PO-1001;customer=CUST-42;currency=EUR;lines=SKU-A:1000000:1.00",
        "PA1;order=PO-1001;customer=CUST-42;currency=EUR;lines=SKU-A:1:1.0",
        "PA1;order=PO-1001;customer=CUST-42;currency=EUR;lines=SKU-A:1:100000000.00",
        (
            "PA1;order=PO-1001;customer=CUST-42;currency=EUR;"
            "lines=SKU-A:1:1.00,SKU-A:2:1.00"
        ),
        (
            "PA1;order=PO-1001;customer=CUST-42;currency=EUR;"
            "lines=SKU-A:1:1.00\nINJECT"
        ),
        "A" * 513,
    ],
)
def test_pa1_parser_rejects_hostile_or_unbounded_subjects(subject: str) -> None:
    with pytest.raises(DemoOrderParseError):
        parse_order_subject(subject)


def test_pa1_parser_rejects_more_than_twenty_lines() -> None:
    lines = ",".join(f"SKU-{index:02d}:1:1.00" for index in range(21))
    subject = f"PA1;order=PO-1001;customer=CUST-42;currency=EUR;lines={lines}"
    with pytest.raises(DemoOrderParseError, match="between 1 and 20"):
        parse_order_subject(subject)


def test_local_reconciliation_has_exact_match_and_all_row_statuses() -> None:
    matched = reconcile_order(parse_order_subject(MATCH_SUBJECT))
    assert matched["outcome"] == "MATCHED"
    assert [row["sku"] for row in matched["rows"]] == ["SKU-A", "SKU-B"]
    assert [row["status"] for row in matched["rows"]] == ["MATCH", "MATCH"]

    mismatch = reconcile_order(parse_order_subject(MISMATCH_SUBJECT))
    assert mismatch["outcome"] == "REVIEW_REQUIRED"
    assert [(row["sku"], row["status"]) for row in mismatch["rows"]] == [
        ("SKU-A", "QTY_MISMATCH"),
        ("SKU-B", "MISSING_IN_ERP"),
        ("SKU-C", "MISSING_IN_EMAIL"),
    ]

    price_catalog = {
        "PO-PRICE": OrderDocument(
            order_id="PO-PRICE",
            customer_id="CUST-42",
            currency="EUR",
            lines=(OrderLine("SKU-A", 1, Decimal("2.00")),),
        )
    }
    priced = reconcile_order(
        parse_order_subject(
            "PA1;order=PO-PRICE;customer=CUST-42;currency=EUR;lines=SKU-A:1:1.00"
        ),
        catalog=price_catalog,
    )
    assert priced["rows"][0]["status"] == "PRICE_MISMATCH"

    missing = reconcile_order(
        parse_order_subject(
            "PA1;order=PO-404;customer=CUST-42;currency=EUR;lines=SKU-Z:1:1.00"
        )
    )
    assert missing["outcome"] == "ERP_ORDER_NOT_FOUND"
    assert missing["rows"][0]["status"] == "MISSING_IN_ERP"


def test_from_mail_match_is_safe_and_reuses_approval_and_outbox_lifecycle(
    client: TestClient,
    session_factory: sessionmaker[Session],
    demo_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del demo_settings
    owner = create_identity(session_factory, marker="e2e", roles=["owner"])
    workflow_id = publish_workflow(client, owner, marker="e2e")
    mail = envelope(marker="e2e")

    def no_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the offline showcase must not use network")

    monkeypatch.setattr("socket.create_connection", no_network)
    prepared = post_from_mail(
        client,
        owner,
        workflow_id,
        key="showcase-e2e-prepare",
        mail=mail,
    )
    assert prepared.status_code == 201, prepared.text
    assert prepared.headers["location"] == f"/v2/runs/{prepared.json()['id']}"
    run = prepared.json()
    assert run["status"] == "proposed"
    assert run["proposal"]["reconciliation"]["outcome"] == "MATCHED"
    assert run["payload"] == {
        "order_id": "PO-1001",
        "customer_id": "CUST-42",
        "currency": "EUR",
        "lines": [
            {"sku": "SKU-A", "quantity": 2, "unit_price": "49.90"},
            {"sku": "SKU-B", "quantity": 1, "unit_price": "10.00"},
        ],
    }
    assert set(run["payload"]) == {"order_id", "customer_id", "currency", "lines"}

    too_early = client.post(
        f"/v2/runs/{run['id']}/execute",
        headers=auth(owner, key="showcase-e2e-too-early"),
    )
    assert too_early.status_code == 409
    assert too_early.json()["detail"]["code"] == "approval_required"

    approval = client.post(
        f"/v2/runs/{run['id']}/approve",
        headers=auth(owner, key="showcase-e2e-approve"),
        json={"proposal_hash": run["proposal_hash"]},
    )
    assert approval.status_code == 200
    execution = client.post(
        f"/v2/runs/{run['id']}/execute",
        headers=auth(owner, key="showcase-e2e-execute"),
    )
    assert execution.status_code == 202
    assert process_once(session_factory)
    operation = client.get(
        f"/v2/execution-operations/{execution.json()['operation_id']}",
        headers=auth(owner),
    )
    assert operation.status_code == 200
    assert operation.json()["status"] == "executed"

    audit = client.get(
        f"/v2/runs/{run['id']}/audit-events", headers=auth(owner)
    ).json()
    persisted_text = json.dumps({"run": run, "audit": audit}, sort_keys=True)
    assert mail["subject"] not in persisted_text
    assert mail["sender"] not in persisted_text
    assert mail["message_id"] not in persisted_text
    assert audit[0]["payload"]["source"]["source_connector"] == "demo_mailbox_v1"

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(OrderSourceClaim)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1
        persisted_run = session.get(Run, run["id"])
        assert persisted_run is not None
        records = session.scalars(select(V2IdempotencyRecord)).all()
        database_text = json.dumps(
            {
                "input": persisted_run.input_payload,
                "proposal": persisted_run.proposal,
                "payload": persisted_run.payload,
                "idempotency": [record.response_body for record in records],
            },
            sort_keys=True,
        )
        assert mail["subject"] not in database_text
        assert mail["sender"] not in database_text


def test_showcase_expected_review_and_not_found_results(
    client: TestClient,
    session_factory: sessionmaker[Session],
    demo_settings: Settings,
) -> None:
    del demo_settings
    owner = create_identity(session_factory, marker="results", roles=["owner"])
    workflow_id = publish_workflow(client, owner, marker="results")

    mismatch = post_from_mail(
        client,
        owner,
        workflow_id,
        key="showcase-results-mismatch",
        mail=envelope(marker="results-mismatch", subject=MISMATCH_SUBJECT),
    )
    assert mismatch.status_code == 201
    reconciliation = mismatch.json()["proposal"]["reconciliation"]
    assert reconciliation["outcome"] == "REVIEW_REQUIRED"
    assert [(row["sku"], row["status"]) for row in reconciliation["rows"]] == [
        ("SKU-A", "QTY_MISMATCH"),
        ("SKU-B", "MISSING_IN_ERP"),
        ("SKU-C", "MISSING_IN_EMAIL"),
    ]

    missing = post_from_mail(
        client,
        owner,
        workflow_id,
        key="showcase-results-missing",
        mail=envelope(
            marker="results-missing",
            subject=(
                "PA1;order=PO-404;customer=CUST-42;currency=EUR;"
                "lines=SKU-Z:1:1.00"
            ),
        ),
    )
    assert missing.status_code == 201
    assert missing.json()["proposal"]["reconciliation"]["outcome"] == (
        "ERP_ORDER_NOT_FOUND"
    )


def test_demo_flag_is_off_by_default_and_always_off_in_production(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = create_identity(session_factory, marker="flags", roles=["owner"])
    workflow_id = publish_workflow(client, owner, marker="flags")
    base = get_settings()
    disabled = replace(base, enable_demo_connectors=False, app_env="development")
    monkeypatch.setattr("prontoagente.v2.runs.get_settings", lambda: disabled)
    response = post_from_mail(
        client,
        owner,
        workflow_id,
        key="showcase-flags-disabled",
        mail=envelope(marker="flags"),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "demo_connector_disabled"

    production = replace(base, enable_demo_connectors=True, app_env="production")
    monkeypatch.setattr("prontoagente.v2.runs.get_settings", lambda: production)
    response = post_from_mail(
        client,
        owner,
        workflow_id,
        key="showcase-flags-production",
        mail=envelope(marker="flags"),
    )
    assert response.status_code == 409
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 0
        assert session.scalar(select(func.count()).select_from(OrderSourceClaim)) == 0


def test_invalid_envelopes_create_no_run_claim_or_outbox(
    client: TestClient,
    session_factory: sessionmaker[Session],
    demo_settings: Settings,
) -> None:
    del demo_settings
    owner = create_identity(session_factory, marker="invalid", roles=["owner"])
    workflow_id = publish_workflow(client, owner, marker="invalid")
    invalid_payloads: list[dict[str, Any]] = [
        {"envelope": {**envelope(marker="invalid-source"), "source_connector": "m365"}},
        {
            "envelope": {
                **envelope(marker="invalid-extra"),
                "body": "must never be accepted",
                "attachments": [],
            }
        },
        {
            "envelope": envelope(
                marker="invalid-subject",
                subject=(
                    "PA1;order=PO-1001;customer=CUST-42;currency=EUR;"
                    "lines=SKU-A:0:1.00"
                ),
            )
        },
    ]
    for index, payload in enumerate(invalid_payloads):
        response = client.post(
            f"/v2/workflows/{workflow_id}/runs/from-mail",
            headers=auth(owner, key=f"showcase-invalid-{index}"),
            json=payload,
        )
        assert response.status_code == 422
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 0
        assert session.scalar(select(func.count()).select_from(OrderSourceClaim)) == 0
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 0


def test_from_mail_rbac_and_source_claims_are_tenant_scoped(
    client: TestClient,
    session_factory: sessionmaker[Session],
    demo_settings: Settings,
) -> None:
    del demo_settings
    owner = create_identity(session_factory, marker="rbac-owner", roles=["owner"])
    workflow_id = publish_workflow(client, owner, marker="rbac")
    operator = create_identity(
        session_factory,
        marker="rbac-operator",
        roles=["operator"],
        tenant_id=owner.tenant_id,
    )
    denied = [
        create_identity(
            session_factory,
            marker=f"rbac-{role}",
            roles=[role],
            tenant_id=owner.tenant_id,
        )
        for role in ("builder", "approver", "auditor")
    ]
    owner_run = post_from_mail(
        client,
        owner,
        workflow_id,
        key="showcase-rbac-owner",
        mail=envelope(marker="rbac-owner"),
    )
    assert owner_run.status_code == 201
    operator_run = post_from_mail(
        client,
        operator,
        workflow_id,
        key="showcase-rbac-operator",
        mail=envelope(marker="rbac-operator"),
    )
    assert operator_run.status_code == 201
    for index, identity in enumerate(denied):
        response = post_from_mail(
            client,
            identity,
            workflow_id,
            key=f"showcase-rbac-denied-{index}",
            mail=envelope(marker=f"rbac-denied-{index}"),
        )
        assert response.status_code == 403

    other_owner = create_identity(
        session_factory, marker="rbac-other", roles=["owner"]
    )
    other_workflow_id = publish_workflow(client, other_owner, marker="rbac-other")
    same_source_other_tenant = post_from_mail(
        client,
        other_owner,
        other_workflow_id,
        key="showcase-rbac-other",
        mail=envelope(marker="rbac-owner"),
    )
    assert same_source_other_tenant.status_code == 201
    assert (
        client.get(
            f"/v2/runs/{owner_run.json()['id']}", headers=auth(other_owner)
        ).status_code
        == 404
    )


def test_from_mail_idempotency_and_source_claim_conflict(
    client: TestClient,
    session_factory: sessionmaker[Session],
    demo_settings: Settings,
) -> None:
    del demo_settings
    owner = create_identity(session_factory, marker="idem", roles=["owner"])
    workflow_id = publish_workflow(client, owner, marker="idem")
    mail = envelope(marker="idem")
    first = post_from_mail(
        client, owner, workflow_id, key="showcase-idem-key", mail=mail
    )
    replay = post_from_mail(
        client, owner, workflow_id, key="showcase-idem-key", mail=mail
    )
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    conflict = post_from_mail(
        client, owner, workflow_id, key="showcase-idem-other", mail=mail
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "order_source_already_claimed"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 1
        assert session.scalar(select(func.count()).select_from(OrderSourceClaim)) == 1


def test_concurrent_source_claim_creates_exactly_one_run(
    client: TestClient,
    session_factory: sessionmaker[Session],
    demo_settings: Settings,
) -> None:
    del demo_settings
    owner = create_identity(session_factory, marker="race", roles=["owner"])
    workflow_id = publish_workflow(client, owner, marker="race")
    mail = envelope(marker="race")
    start = Barrier(2)

    def request(key: str) -> int:
        start.wait(timeout=5)
        return post_from_mail(
            client, owner, workflow_id, key=key, mail=mail
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(request, "showcase-race-one"),
            executor.submit(request, "showcase-race-two"),
        ]
        statuses = sorted(future.result(timeout=10) for future in futures)
    assert statuses == [201, 409]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 1
        assert session.scalar(select(func.count()).select_from(OrderSourceClaim)) == 1
        idempotency_count = session.scalar(
            select(func.count())
            .select_from(V2IdempotencyRecord)
            .where(V2IdempotencyRecord.operation == "run.from_mail")
        )
        assert idempotency_count == 1


def test_from_mail_rejects_incompatible_workflow_and_is_not_a_runtime_connector(
    client: TestClient,
    session_factory: sessionmaker[Session],
    demo_settings: Settings,
) -> None:
    del demo_settings
    owner = create_identity(session_factory, marker="boundary", roles=["owner"])
    workflow_id = publish_workflow(
        client,
        owner,
        marker="boundary",
        connector="m365_mail_intake_v1",
        action="list_messages",
    )
    response = post_from_mail(
        client,
        owner,
        workflow_id,
        key="showcase-boundary-key",
        mail=envelope(marker="boundary"),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "demo_workflow_incompatible"
    catalog = client.get("/v2/connectors", headers=auth(owner)).json()
    assert "demo_mailbox_v1" not in {item["name"] for item in catalog}
    with pytest.raises(ConnectorExecutionError, match="unknown connector"):
        runtime_connector("demo_mailbox_v1")
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 0
        assert session.scalar(select(func.count()).select_from(V2AuditEvent)) == 0
