from dataclasses import replace
from typing import Any

import httpx
import pytest

from prontoagente.config import get_settings
from prontoagente.v2.connectors import (
    GRAPH_BASE_URL,
    GRAPH_SCOPE,
    ConnectorExecutionError,
    ConnectorNotConfiguredError,
    M365MailIntakeConnector,
    RetryableConnectorError,
)

PLATFORM_TENANT_ID = "platform-tenant"


def configured_settings():
    return replace(
        get_settings(),
        m365_connector_enabled=True,
        m365_platform_tenant_id=PLATFORM_TENANT_ID,
        m365_permission_attestation="Mail.ReadBasic.All",
        m365_tenant_id="tenant-id",
        m365_client_id="client-id",
        m365_client_secret="super-secret-value",
        m365_mailbox_id="mailbox@example.test",
        m365_folder_id="Inbox",
        http_timeout_seconds=2,
    )


def test_m365_connector_uses_one_safe_graph_get_and_redacts_content() -> None:
    graph_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            assert request.method == "POST"
            assert GRAPH_SCOPE.replace(":", "%3A").split("%3A", 1)[0] in request.content.decode()
            return httpx.Response(200, json={"access_token": "access-token-secret"})
        assert str(request.url).startswith(GRAPH_BASE_URL)
        graph_requests.append(request)
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "message-1",
                        "subject": "Subject",
                        "from": {"emailAddress": {"address": "sender@example.test"}},
                        "receivedDateTime": "2026-09-22T10:00:00Z",
                        "isRead": False,
                        "internetMessageId": "<id@example.test>",
                        "body": {"content": "must-not-leak"},
                        "bodyPreview": "must-not-leak",
                        "attachments": [{"name": "must-not-leak"}],
                    }
                ]
            },
        )

    connector = M365MailIntakeConnector(
        configured_settings(),
        prontoagente_tenant_id=PLATFORM_TENANT_ID,
        transport=httpx.MockTransport(handler),
    )
    result = connector.execute(
        action="list_messages", payload={"top": 10}, operation_id="operation-id"
    )

    assert len(graph_requests) == 1
    graph_request = graph_requests[0]
    assert graph_request.method == "GET"
    assert graph_request.url.path == ("/v1.0/users/mailbox@example.test/mailFolders/Inbox/messages")
    selected = graph_request.url.params["$select"].split(",")
    assert "body" not in selected
    assert "bodyPreview" not in selected
    assert "attachments" not in selected
    assert graph_request.url.params["$top"] == "10"
    assert "must-not-leak" not in str(result)
    assert "access-token-secret" not in str(result)
    assert "super-secret-value" not in str(result)
    assert result["count"] == 1


def test_m365_429_retry_after_is_retryable() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "token"})
        return httpx.Response(429, headers={"Retry-After": "7"})

    connector = M365MailIntakeConnector(
        configured_settings(),
        prontoagente_tenant_id=PLATFORM_TENANT_ID,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(RetryableConnectorError) as caught:
        connector.execute(action="list_messages", payload={"top": 1}, operation_id="op")
    assert caught.value.retry_after == 7
    graph_methods = [
        request.method for request in requests if request.url.host == "graph.microsoft.com"
    ]
    assert graph_methods == ["GET"]


def test_m365_response_never_echoes_runtime_configuration() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"host": request.url.host, "method": request.method})
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "token"})
        return httpx.Response(200, json={"value": []})

    result = M365MailIntakeConnector(
        configured_settings(),
        prontoagente_tenant_id=PLATFORM_TENANT_ID,
        transport=httpx.MockTransport(handler),
    ).execute(action="list_messages", payload={}, operation_id="op")
    serialized = str(result)
    assert "tenant-id" not in serialized
    assert "client-id" not in serialized
    assert "super-secret-value" not in serialized
    assert seen == [
        {"host": "login.microsoftonline.com", "method": "POST"},
        {"host": "graph.microsoft.com", "method": "GET"},
    ]


def test_m365_token_service_429_is_retryable_without_graph_call() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "11"})

    connector = M365MailIntakeConnector(
        configured_settings(),
        prontoagente_tenant_id=PLATFORM_TENANT_ID,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(RetryableConnectorError) as caught:
        connector.execute(action="list_messages", payload={}, operation_id="op")

    assert caught.value.retry_after == 11
    assert [(request.url.host, request.method) for request in requests] == [
        ("login.microsoftonline.com", "POST")
    ]


@pytest.mark.parametrize(
    "token_document",
    [{}, {"access_token": None}, {"access_token": ""}],
)
def test_m365_rejects_missing_or_invalid_access_token(token_document: object) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=token_document)

    connector = M365MailIntakeConnector(
        configured_settings(),
        prontoagente_tenant_id=PLATFORM_TENANT_ID,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ConnectorExecutionError, match="unable to acquire"):
        connector.execute(action="list_messages", payload={}, operation_id="op")


def test_m365_rejects_malformed_message_collection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "token"})
        return httpx.Response(200, json={"value": {"not": "a list"}})

    connector = M365MailIntakeConnector(
        configured_settings(),
        prontoagente_tenant_id=PLATFORM_TENANT_ID,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ConnectorExecutionError, match="Graph message read failed"):
        connector.execute(action="list_messages", payload={}, operation_id="op")


@pytest.mark.parametrize("attestation", [None, "Mail.Read.All"])
def test_m365_permission_attestation_is_required_before_any_network(
    attestation: str | None,
) -> None:
    calls = 0

    def no_network(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("an invalid permission attestation must fail closed")

    settings = replace(
        configured_settings(), m365_permission_attestation=attestation
    )
    connector = M365MailIntakeConnector(
        settings,
        prontoagente_tenant_id=PLATFORM_TENANT_ID,
        transport=httpx.MockTransport(no_network),
    )
    with pytest.raises(ConnectorNotConfiguredError):
        connector.execute(action="list_messages", payload={}, operation_id="op")
    assert calls == 0


def test_m365_connector_rejects_a_different_prontoagente_tenant_without_network() -> None:
    calls = 0

    def no_network(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("a cross-tenant connector call must fail closed")

    connector = M365MailIntakeConnector(
        configured_settings(),
        prontoagente_tenant_id="different-platform-tenant",
        transport=httpx.MockTransport(no_network),
    )
    with pytest.raises(ConnectorNotConfiguredError):
        connector.execute(action="list_messages", payload={}, operation_id="op")
    assert calls == 0
