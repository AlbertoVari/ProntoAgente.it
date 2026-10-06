"""Prompt publication, isolation, and historical resolution."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session, sessionmaker

from prontoagente.ai.prompts import EMAIL_ORDER_EXTRACT_V1, resolve_prompt
from prontoagente.errors import ConflictError
from prontoagente.v2.auth import AuthContext
from prontoagente.v2.prompt_catalog import publish_prompt_version
from prontoagente.v2.schemas import PublishRequest
from test_v2 import auth, create_identity


def test_prompt_lifecycle_isolation_and_legacy(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    owner = create_identity(session_factory, marker="prompt-owner", roles=["owner"])
    other = create_identity(session_factory, marker="prompt-other", roles=["owner"])
    created = client.post(
        "/v2/prompts", headers=auth(owner), json={"slug": "orders", "name": "Orders"}
    )
    assert created.status_code == 201, created.text
    prompt_id = created.json()["id"]
    draft = client.post(
        f"/v2/prompts/{prompt_id}/versions",
        headers=auth(owner),
        json={"system_prompt": "Extract order metadata", "tool_name": "demo_erp_reconcile_v1"},
    )
    assert draft.status_code == 201, draft.text
    version = draft.json()
    agent_id = client.post(
        "/v2/agents", headers=auth(owner), json={"slug": "prompt-agent", "name": "Prompt Agent"}
    ).json()["id"]
    definition = {
        "ai": {
            "prompt_id": "orders/v1",
            "tool_name": "demo_erp_reconcile_v1",
            "max_input_tokens": 4096,
            "max_output_tokens": 512,
        }
    }
    agent_url = f"/v2/agents/{agent_id}/versions"
    assert (
        client.post(agent_url, headers=auth(owner), json={"definition": definition}).status_code
        == 409
    )
    with session_factory() as session:
        with pytest.raises(ValueError):
            resolve_prompt(session, owner.tenant_id, "orders/v1")
        assert (
            resolve_prompt(session, owner.tenant_id, "email_order_extract/v1")
            == EMAIL_ORDER_EXTRACT_V1
        )
    assert client.get(f"/v2/prompts/{prompt_id}", headers=auth(other)).status_code == 404
    assert (
        client.patch(
            f"/v2/prompts/{prompt_id}/versions/{version['id']}",
            headers=auth(other),
            json={"lock_version": 1, "system_prompt": "x", "tool_name": "demo_erp_reconcile_v1"},
        ).status_code
        == 404
    )
    published = client.post(
        f"/v2/prompts/{prompt_id}/versions/{version['id']}/publish",
        headers=auth(owner),
        json={"lock_version": 1},
    )
    assert published.status_code == 200, published.text
    assert published.json()["prompt_hash"].startswith("sha256:")
    agent_version = client.post(agent_url, headers=auth(owner), json={"definition": definition})
    assert agent_version.status_code == 201, agent_version.text
    other_agent = client.post(
        "/v2/agents", headers=auth(other), json={"slug": "other-agent", "name": "Other Agent"}
    ).json()["id"]
    assert (
        client.post(
            f"/v2/agents/{other_agent}/versions",
            headers=auth(other),
            json={"definition": definition},
        ).status_code
        == 409
    )
    assert (
        client.patch(
            f"/v2/prompts/{prompt_id}/versions/{version['id']}",
            headers=auth(owner),
            json={"lock_version": 2, "system_prompt": "edit", "tool_name": "demo_erp_reconcile_v1"},
        ).status_code
        == 409
    )
    with session_factory() as session:
        assert (
            resolve_prompt(session, owner.tenant_id, "orders/v1").prompt_hash
            == published.json()["prompt_hash"]
        )
        with pytest.raises(ValueError):
            resolve_prompt(session, other.tenant_id, "orders/v1")
        with pytest.raises(DatabaseError):
            session.execute(
                text("UPDATE prompt_versions SET system_prompt='tamper' WHERE id=:id"),
                {"id": version["id"]},
            )


def test_publish_race(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    owner = create_identity(session_factory, marker="prompt-race", roles=["owner"])
    prompt_id = client.post(
        "/v2/prompts", headers=auth(owner), json={"slug": "racing", "name": "Race"}
    ).json()["id"]
    version_id = client.post(
        f"/v2/prompts/{prompt_id}/versions",
        headers=auth(owner),
        json={"system_prompt": "First", "tool_name": "demo_erp_reconcile_v1"},
    ).json()["id"]
    context = AuthContext(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        subject="race",
        display_name="race",
        roles=frozenset({"owner"}),
        api_key_id="test",
    )

    def publish() -> str:
        with session_factory() as session:
            try:
                publish_prompt_version(
                    session, context, prompt_id, version_id, PublishRequest(lock_version=1)
                )
                return "published"
            except ConflictError:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: publish(), range(2)))
    assert sorted(results) == ["conflict", "published"]
