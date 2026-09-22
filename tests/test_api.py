from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
from typing import Any

import pytest
from fastapi.testclient import TestClient

from prontoagente import services as service_module
from prontoagente.connectors import SimulatedConnector


def order_payload(
    *, external_order_id: str = "WEB-1001", unit_price: str = "49.90"
) -> dict[str, Any]:
    return {
        "action_type": "create_sales_order",
        "order": {
            "external_order_id": external_order_id,
            "customer_id": "CUSTOMER-42",
            "currency": "eur",
            "lines": [
                {"sku": "PLAN-PRO", "quantity": 2, "unit_price": unit_price},
                {"sku": "SETUP", "quantity": 1, "unit_price": "10.00"},
            ],
        },
    }


def headers(key: str, actor: str = "founder") -> dict[str, str]:
    return {"Idempotency-Key": key, "X-Actor-Id": actor}


def create_draft(client: TestClient, key: str = "draft-key-0001") -> dict[str, Any]:
    response = client.post(
        "/v1/erp-drafts/dry-run", headers=headers(key), json=order_payload()
    )
    assert response.status_code == 201
    return response.json()


def test_health(client: TestClient) -> None:
    response = client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_mutations_require_headers_and_action_allowlist(client: TestClient) -> None:
    missing_headers = client.post("/v1/erp-drafts/dry-run", json=order_payload())
    assert missing_headers.status_code == 422

    unknown_action = order_payload()
    unknown_action["action_type"] = "delete_customer"
    rejected = client.post(
        "/v1/erp-drafts/dry-run",
        headers=headers("unknown-action-01"),
        json=unknown_action,
    )
    assert rejected.status_code == 422
    assert any(error["loc"][-1] == "action_type" for error in rejected.json()["detail"])


def test_dry_run_is_deterministic_and_does_not_invoke_connector(
    client: TestClient, monkeypatch: Any
) -> None:
    def fail_if_called(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise AssertionError("connector must not run during dry-run")

    monkeypatch.setattr(SimulatedConnector, "simulate", fail_if_called)
    first = client.post(
        "/v1/erp-drafts/dry-run",
        headers=headers("determinism-key-01"),
        json=order_payload(unit_price="49.9"),
    )
    second = client.post(
        "/v1/erp-drafts/dry-run",
        headers=headers("determinism-key-02"),
        json=order_payload(unit_price="49.90"),
    )

    assert first.status_code == second.status_code == 201
    first_body = first.json()
    second_body = second.json()
    assert first_body["id"] != second_body["id"]
    assert first_body["proposal"] == second_body["proposal"]
    assert first_body["proposal_hash"] == second_body["proposal_hash"]
    assert first_body["proposal"]["summary"] == (
        "Create sales order WEB-1001 for customer CUSTOMER-42"
    )
    assert first_body["proposal"]["target"] == {
        "connector": "simulated_erp",
        "resource": "sales_order",
    }
    assert first_body["proposal"]["totals"] == {"net_amount": "109.80"}
    assert first_body["order"]["currency"] == "EUR"
    assert first_body["status"] == "proposed"
    assert first_body["simulation_result"] is None


def test_dry_run_idempotency_replays_and_rejects_changed_payload(client: TestClient) -> None:
    first = client.post(
        "/v1/erp-drafts/dry-run",
        headers=headers("idempotency-key-01"),
        json=order_payload(),
    )
    replay = client.post(
        "/v1/erp-drafts/dry-run",
        headers=headers("idempotency-key-01"),
        json=order_payload(),
    )
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()

    changed = client.post(
        "/v1/erp-drafts/dry-run",
        headers=headers("idempotency-key-01"),
        json=order_payload(external_order_id="WEB-CHANGED"),
    )
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "idempotency_key_reused"

    events = client.get(f"/v1/erp-drafts/{first.json()['id']}/audit-events")
    assert events.status_code == 200
    assert [event["event_type"] for event in events.json()] == ["draft_proposed"]


def test_idempotency_key_cannot_be_replayed_by_another_actor(client: TestClient) -> None:
    response = client.post(
        "/v1/erp-drafts/dry-run",
        headers=headers("actor-key-000001", actor="founder"),
        json=order_payload(),
    )
    assert response.status_code == 201
    conflict = client.post(
        "/v1/erp-drafts/dry-run",
        headers=headers("actor-key-000001", actor="someone-else"),
        json=order_payload(),
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_key_reused"


def test_approval_requires_matching_hash_and_simulation_requires_approval(
    client: TestClient,
) -> None:
    draft = create_draft(client)

    too_early = client.post(
        f"/v1/erp-drafts/{draft['id']}/simulate",
        headers=headers("simulate-early-01"),
    )
    assert too_early.status_code == 409
    assert too_early.json()["detail"]["code"] == "approval_required"

    wrong_hash = "sha256:" + ("0" * 64)
    mismatch = client.post(
        f"/v1/erp-drafts/{draft['id']}/approve",
        headers=headers("approve-wrong-01"),
        json={"proposal_hash": wrong_hash},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "proposal_hash_mismatch"

    unchanged = client.get(f"/v1/erp-drafts/{draft['id']}")
    assert unchanged.status_code == 200
    assert unchanged.json()["status"] == "proposed"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"reason": "  "},
        {"reason": "x" * 501},
    ],
    ids=["missing", "blank", "too-long"],
)
def test_rejection_reason_is_required_and_bounded(
    client: TestClient, payload: dict[str, Any]
) -> None:
    draft = create_draft(client, "reject-validation-draft")
    response = client.post(
        f"/v1/erp-drafts/{draft['id']}/reject",
        headers=headers("reject-validation-key"),
        json=payload,
    )
    assert response.status_code == 422
    assert client.get(f"/v1/erp-drafts/{draft['id']}").json()["status"] == "proposed"


def test_rejection_is_idempotent_terminal_and_audited(
    client: TestClient, monkeypatch: Any
) -> None:
    def fail_if_called(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise AssertionError("connector must not run during rejection")

    monkeypatch.setattr(SimulatedConnector, "simulate", fail_if_called)
    draft = create_draft(client, "reject-flow-draft")
    url = f"/v1/erp-drafts/{draft['id']}/reject"
    rejection_headers = headers("reject-flow-key-01", actor="reviewer")

    rejected = client.post(
        url,
        headers=rejection_headers,
        json={"reason": "  Dati cliente non verificati  "},
    )
    assert rejected.status_code == 200
    rejected_body = rejected.json()
    assert rejected_body["status"] == "rejected"
    assert rejected_body["rejected_by"] == "reviewer"
    assert rejected_body["rejected_at"] is not None
    assert rejected_body["rejection_reason"] == "Dati cliente non verificati"
    assert rejected_body["approved_by"] is None
    assert rejected_body["simulation_result"] is None

    replay = client.post(
        url,
        headers=rejection_headers,
        json={"reason": "Dati cliente non verificati"},
    )
    assert replay.status_code == 200
    assert replay.json() == rejected_body

    changed_replay = client.post(
        url,
        headers=rejection_headers,
        json={"reason": "Motivo differente"},
    )
    assert changed_replay.status_code == 409
    assert changed_replay.json()["detail"]["code"] == "idempotency_key_reused"

    second_rejection = client.post(
        url,
        headers=headers("reject-flow-key-02", actor="reviewer"),
        json={"reason": "Dati cliente non verificati"},
    )
    assert second_rejection.status_code == 409
    assert second_rejection.json()["detail"]["code"] == "invalid_state"

    approval = client.post(
        f"/v1/erp-drafts/{draft['id']}/approve",
        headers=headers("reject-approve-key"),
        json={"proposal_hash": draft["proposal_hash"]},
    )
    assert approval.status_code == 409
    assert approval.json()["detail"]["code"] == "invalid_state"

    simulation = client.post(
        f"/v1/erp-drafts/{draft['id']}/simulate",
        headers=headers("reject-simulate-key"),
    )
    assert simulation.status_code == 409
    assert simulation.json()["detail"]["code"] == "invalid_state"

    events = client.get(f"/v1/erp-drafts/{draft['id']}/audit-events")
    assert events.status_code == 200
    assert [event["event_type"] for event in events.json()] == [
        "draft_proposed",
        "proposal_rejected",
    ]
    assert events.json()[1]["payload"]["reason"] == "Dati cliente non verificati"


def test_complete_flow_connector_once_replays_and_audits(
    client: TestClient, monkeypatch: Any
) -> None:
    draft = create_draft(client, "flow-draft-0001")

    approval = client.post(
        f"/v1/erp-drafts/{draft['id']}/approve",
        headers=headers("flow-approve-001", actor="reviewer"),
        json={"proposal_hash": draft["proposal_hash"]},
    )
    assert approval.status_code == 200
    assert approval.json()["status"] == "approved"
    assert approval.json()["approved_by"] == "reviewer"

    approval_replay = client.post(
        f"/v1/erp-drafts/{draft['id']}/approve",
        headers=headers("flow-approve-001", actor="reviewer"),
        json={"proposal_hash": draft["proposal_hash"]},
    )
    assert approval_replay.status_code == 200
    assert approval_replay.json() == approval.json()

    duplicate_approval = client.post(
        f"/v1/erp-drafts/{draft['id']}/approve",
        headers=headers("flow-approve-002", actor="reviewer"),
        json={"proposal_hash": draft["proposal_hash"]},
    )
    assert duplicate_approval.status_code == 409
    assert duplicate_approval.json()["detail"]["code"] == "invalid_state"

    original_simulate: Callable[..., dict[str, Any]] = SimulatedConnector.simulate
    connector_calls = 0

    def count_calls(
        connector: SimulatedConnector,
        proposal: dict[str, Any],
        proposal_hash: str,
        operation_id: str,
    ) -> dict[str, Any]:
        nonlocal connector_calls
        connector_calls += 1
        return original_simulate(connector, proposal, proposal_hash, operation_id)

    monkeypatch.setattr(SimulatedConnector, "simulate", count_calls)
    simulation = client.post(
        f"/v1/erp-drafts/{draft['id']}/simulate",
        headers=headers("flow-simulate-01", actor="operator"),
    )
    assert simulation.status_code == 200
    simulation_body = simulation.json()
    operation_id = simulation_body["operation_id"]
    assert simulation.headers["location"] == f"/v1/simulation-operations/{operation_id}"
    assert simulation_body["status"] == "simulated"
    assert simulation_body["result"] == {
        "connector": "simulated_erp",
        "outcome": "accepted",
        "external_reference": f"SIM-{operation_id.replace('-', '')[:12].upper()}",
        "idempotency_token": operation_id,
        "proposal_hash": draft["proposal_hash"],
        "network_used": False,
    }
    assert connector_calls == 1

    simulation_replay = client.post(
        f"/v1/erp-drafts/{draft['id']}/simulate",
        headers=headers("flow-simulate-01", actor="operator"),
    )
    assert simulation_replay.status_code == 200
    assert simulation_replay.json() == simulation_body
    assert connector_calls == 1

    operation = client.get(f"/v1/simulation-operations/{operation_id}")
    assert operation.status_code == 200
    assert operation.json() == simulation_body

    workflow = client.get(f"/v1/erp-drafts/{draft['id']}")
    assert workflow.status_code == 200
    assert workflow.json()["status"] == "simulated"
    assert workflow.json()["simulation_result"] == simulation_body["result"]

    duplicate_simulation = client.post(
        f"/v1/erp-drafts/{draft['id']}/simulate",
        headers=headers("flow-simulate-02", actor="operator"),
    )
    assert duplicate_simulation.status_code == 409

    events = client.get(f"/v1/erp-drafts/{draft['id']}/audit-events")
    assert events.status_code == 200
    assert [event["event_type"] for event in events.json()] == [
        "draft_proposed",
        "proposal_approved",
        "simulation_started",
        "simulation_completed",
    ]
    assert [event["actor_id"] for event in events.json()] == [
        "founder",
        "reviewer",
        "operator",
        "operator",
    ]


def test_concurrent_simulation_claim_invokes_connector_once(
    client: TestClient, monkeypatch: Any
) -> None:
    draft = create_draft(client, "concurrent-draft")
    approval = client.post(
        f"/v1/erp-drafts/{draft['id']}/approve",
        headers=headers("concurrent-approve", actor="reviewer"),
        json={"proposal_hash": draft["proposal_hash"]},
    )
    assert approval.status_code == 200

    original_simulate: Callable[..., dict[str, Any]] = SimulatedConnector.simulate
    connector_started = Event()
    release_connector = Event()
    call_lock = Lock()
    connector_calls = 0

    def blocking_simulate(
        connector: SimulatedConnector,
        proposal: dict[str, Any],
        proposal_hash: str,
        operation_id: str,
    ) -> dict[str, Any]:
        nonlocal connector_calls
        with call_lock:
            connector_calls += 1
        connector_started.set()
        assert release_connector.wait(timeout=5)
        return original_simulate(connector, proposal, proposal_hash, operation_id)

    monkeypatch.setattr(SimulatedConnector, "simulate", blocking_simulate)
    url = f"/v1/erp-drafts/{draft['id']}/simulate"
    claim_headers = headers("concurrent-simulate-key", actor="operator")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(client.post, url, headers=claim_headers)
        assert connector_started.wait(timeout=5)

        in_progress_replay = client.post(url, headers=claim_headers)
        assert in_progress_replay.status_code == 202
        assert in_progress_replay.json()["status"] == "simulating"
        operation_id = in_progress_replay.json()["operation_id"]
        assert in_progress_replay.headers["location"] == (
            f"/v1/simulation-operations/{operation_id}"
        )

        competing_key = client.post(
            url,
            headers=headers("concurrent-other-key", actor="operator"),
        )
        assert competing_key.status_code == 409
        assert competing_key.json()["detail"]["code"] == "simulation_in_progress"
        assert connector_calls == 1

        release_connector.set()
        first_response = first_future.result(timeout=5)

    assert first_response.status_code == 200
    assert first_response.json()["operation_id"] == operation_id
    assert first_response.json()["status"] == "simulated"
    assert connector_calls == 1

    final_replay = client.post(url, headers=claim_headers)
    assert final_replay.status_code == 200
    assert final_replay.json() == first_response.json()
    assert connector_calls == 1


def test_simultaneous_claim_race_is_resolved_without_a_second_connector_call(
    client: TestClient, monkeypatch: Any
) -> None:
    draft = create_draft(client, "simultaneous-claim-draft")
    approval = client.post(
        f"/v1/erp-drafts/{draft['id']}/approve",
        headers=headers("simultaneous-claim-approve", actor="reviewer"),
        json={"proposal_hash": draft["proposal_hash"]},
    )
    assert approval.status_code == 200

    claim_barrier = Barrier(2)
    connector_calls = 0
    call_lock = Lock()
    claim_checks = 0
    original_simulate: Callable[..., dict[str, Any]] = SimulatedConnector.simulate
    original_existing_operation = service_module._existing_simulation_operation

    def synchronize_claims(session: Any, workflow_id: str) -> Any:
        nonlocal claim_checks
        existing = original_existing_operation(session, workflow_id)
        with call_lock:
            claim_checks += 1
            should_wait = claim_checks <= 2
        if should_wait:
            claim_barrier.wait(timeout=5)
        return existing

    def count_calls(
        connector: SimulatedConnector,
        proposal: dict[str, Any],
        proposal_hash: str,
        operation_id: str,
    ) -> dict[str, Any]:
        nonlocal connector_calls
        with call_lock:
            connector_calls += 1
        return original_simulate(connector, proposal, proposal_hash, operation_id)

    monkeypatch.setattr(SimulatedConnector, "simulate", count_calls)
    monkeypatch.setattr(
        service_module,
        "_existing_simulation_operation",
        synchronize_claims,
    )
    url = f"/v1/erp-drafts/{draft['id']}/simulate"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                client.post,
                url,
                headers=headers(f"simultaneous-claim-{suffix}", actor="operator"),
            )
            for suffix in ("one", "two")
        ]
        responses = [future.result(timeout=10) for future in futures]

    assert sorted(response.status_code for response in responses) == [200, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["detail"]["code"] in {
        "simulation_in_progress",
        "simulation_already_claimed",
    }
    assert connector_calls == 1


def test_simulated_connector_failure_is_terminal_and_replayed(
    client: TestClient, monkeypatch: Any
) -> None:
    draft = create_draft(client, "failure-draft")
    approval = client.post(
        f"/v1/erp-drafts/{draft['id']}/approve",
        headers=headers("failure-approve", actor="reviewer"),
        json={"proposal_hash": draft["proposal_hash"]},
    )
    assert approval.status_code == 200
    connector_calls = 0

    def fail_simulation(*_args: object, **_kwargs: object) -> dict[str, Any]:
        nonlocal connector_calls
        connector_calls += 1
        raise RuntimeError("known fake failure")

    monkeypatch.setattr(SimulatedConnector, "simulate", fail_simulation)
    url = f"/v1/erp-drafts/{draft['id']}/simulate"
    failure_headers = headers("failure-simulate", actor="operator")
    failed = client.post(url, headers=failure_headers)
    assert failed.status_code == 502
    assert failed.json()["status"] == "simulation_failed"
    assert failed.json()["error_code"] == "simulated_connector_failure"
    assert failed.json()["error_message"] == "known fake failure"
    assert connector_calls == 1

    replay = client.post(url, headers=failure_headers)
    assert replay.status_code == 502
    assert replay.json() == failed.json()
    assert connector_calls == 1

    events = client.get(f"/v1/erp-drafts/{draft['id']}/audit-events")
    assert [event["event_type"] for event in events.json()] == [
        "draft_proposed",
        "proposal_approved",
        "simulation_started",
        "simulation_failed",
    ]


def test_unknown_workflow_is_404(client: TestClient) -> None:
    response = client.get("/v1/erp-drafts/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "workflow_not_found"


def test_persisted_timestamps_are_returned_as_explicit_utc(client: TestClient) -> None:
    draft = create_draft(client, "utc-timestamps-draft")
    approval = client.post(
        f"/v1/erp-drafts/{draft['id']}/approve",
        headers=headers("utc-timestamps-approve", actor="reviewer"),
        json={"proposal_hash": draft["proposal_hash"]},
    )
    assert approval.status_code == 200

    persisted = client.get(f"/v1/erp-drafts/{draft['id']}")
    assert persisted.status_code == 200
    body = persisted.json()
    assert body["created_at"].endswith("Z")
    assert body["updated_at"].endswith("Z")
    assert body["approved_at"].endswith("Z")

    events = client.get(f"/v1/erp-drafts/{draft['id']}/audit-events")
    assert events.status_code == 200
    assert all(event["created_at"].endswith("Z") for event in events.json())
