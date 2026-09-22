"""Run the fully offline synthetic-mail order showcase against a local database."""

import os
from pprint import pprint
from uuid import uuid4

from fastapi.testclient import TestClient

from prontoagente.main import app
from prontoagente.worker import process_once

MATCH_SUBJECT = (
    "PA1;order=PO-1001;customer=CUST-42;currency=EUR;"
    "lines=SKU-A:2:49.90,SKU-B:1:10.00"
)


def main() -> None:
    token = os.environ.get("PRONTOAGENTE_API_KEY")
    if not token:
        raise SystemExit("set PRONTOAGENTE_API_KEY to the one-time bootstrap value")
    if os.environ.get("ENABLE_DEMO_CONNECTORS", "").lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise SystemExit("set ENABLE_DEMO_CONNECTORS=true (never use it in production)")

    marker = uuid4().hex[:10]
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)

    agent = client.post(
        "/v2/agents",
        headers=headers,
        json={"slug": f"showcase-agent-{marker}", "name": "Offline showcase agent"},
    )
    agent.raise_for_status()
    agent_version = client.post(
        f"/v2/agents/{agent.json()['id']}/versions",
        headers=headers,
        json={"definition": {"purpose": "deterministic offline order preparation"}},
    )
    agent_version.raise_for_status()
    published_agent = client.post(
        f"/v2/agents/{agent.json()['id']}/versions/"
        f"{agent_version.json()['id']}/publish",
        headers=headers,
        json={"lock_version": agent_version.json()["lock_version"]},
    )
    published_agent.raise_for_status()

    workflow = client.post(
        "/v2/workflows",
        headers=headers,
        json={
            "slug": f"showcase-workflow-{marker}",
            "name": "Offline mail-to-order showcase",
        },
    )
    workflow.raise_for_status()
    workflow_version = client.post(
        f"/v2/workflows/{workflow.json()['id']}/versions",
        headers=headers,
        json={
            "agent_version_id": published_agent.json()["id"],
            "connector": "simulated_erp",
            "action": "create_sales_order",
            "config": {"target": "offline-demo-erp"},
            "input_schema": {
                "type": "object",
                "required": ["order_id", "customer_id", "currency", "lines"],
            },
            "approval_required": True,
        },
    )
    workflow_version.raise_for_status()
    published_workflow = client.post(
        f"/v2/workflows/{workflow.json()['id']}/versions/"
        f"{workflow_version.json()['id']}/publish",
        headers=headers,
        json={"lock_version": workflow_version.json()["lock_version"]},
    )
    published_workflow.raise_for_status()

    prepared = client.post(
        f"/v2/workflows/{workflow.json()['id']}/runs/from-mail",
        headers={**headers, "Idempotency-Key": f"showcase-prepare-{marker}"},
        json={
            "envelope": {
                "source_connector": "demo_mailbox_v1",
                "message_id": f"message-{marker}",
                "internet_message_id": f"<{marker}@example.test>",
                "sender": "orders@example.test",
                "subject": MATCH_SUBJECT,
            }
        },
    )
    prepared.raise_for_status()
    run = prepared.json()
    print("Prepared governed run (mail metadata is not persisted):")
    pprint(run, sort_dicts=False)

    approved = client.post(
        f"/v2/runs/{run['id']}/approve",
        headers={**headers, "Idempotency-Key": f"showcase-approve-{marker}"},
        json={"proposal_hash": run["proposal_hash"]},
    )
    approved.raise_for_status()
    execution = client.post(
        f"/v2/runs/{run['id']}/execute",
        headers={**headers, "Idempotency-Key": f"showcase-execute-{marker}"},
    )
    execution.raise_for_status()
    process_once()

    final = client.get(
        f"/v2/execution-operations/{execution.json()['operation_id']}",
        headers=headers,
    )
    final.raise_for_status()
    print("Final simulated execution:")
    pprint(final.json(), sort_dicts=False)


if __name__ == "__main__":
    main()
