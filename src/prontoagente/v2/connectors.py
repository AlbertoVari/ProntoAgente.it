"""Static connector registry and network-safe connector implementations."""

from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import quote

import httpx

from prontoagente.config import Settings, get_settings

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
M365_REQUIRED_PERMISSION = "Mail.ReadBasic.All"
GRAPH_MESSAGE_FIELDS = (
    "id",
    "subject",
    "from",
    "receivedDateTime",
    "isRead",
    "internetMessageId",
)


@dataclass(frozen=True, slots=True)
class ConnectorSpec:
    name: str
    capability: Literal["READ", "WRITE"]
    actions: tuple[str, ...]


CONNECTOR_SPECS: dict[str, ConnectorSpec] = {
    "simulated_erp": ConnectorSpec(
        name="simulated_erp", capability="WRITE", actions=("create_sales_order",)
    ),
    "m365_mail_intake_v1": ConnectorSpec(
        name="m365_mail_intake_v1", capability="READ", actions=("list_messages",)
    ),
}


class ConnectorError(Exception):
    code = "connector_error"


class ConnectorNotConfiguredError(ConnectorError):
    code = "connector_not_configured"


class ConnectorExecutionError(ConnectorError):
    code = "connector_failed"


class RetryableConnectorError(ConnectorError):
    code = "connector_retryable"

    def __init__(self, message: str, *, retry_after: int = 1) -> None:
        super().__init__(message)
        self.retry_after = max(1, min(retry_after, 3600))


class RuntimeConnector(Protocol):
    def execute(
        self,
        *,
        action: str,
        payload: dict[str, Any],
        operation_id: str,
    ) -> dict[str, Any]: ...


def m365_configured(
    prontoagente_tenant_id: str | None, settings: Settings | None = None
) -> bool:
    resolved = settings or get_settings()
    required = (
        resolved.m365_platform_tenant_id,
        resolved.m365_tenant_id,
        resolved.m365_client_id,
        resolved.m365_client_secret,
        resolved.m365_mailbox_id,
        resolved.m365_folder_id,
    )
    return bool(
        resolved.m365_connector_enabled
        and prontoagente_tenant_id
        and all(required)
        and resolved.m365_platform_tenant_id == prontoagente_tenant_id
        and resolved.m365_permission_attestation == M365_REQUIRED_PERMISSION
    )


def connector_catalog(
    prontoagente_tenant_id: str, settings: Settings | None = None
) -> list[dict[str, Any]]:
    resolved = settings or get_settings()
    return [
        {
            "name": spec.name,
            "capability": spec.capability,
            "actions": list(spec.actions),
            "configured": spec.name == "simulated_erp"
            or m365_configured(prontoagente_tenant_id, resolved),
        }
        for spec in CONNECTOR_SPECS.values()
    ]


def validate_connector_action(connector: str, action: str) -> None:
    spec = CONNECTOR_SPECS.get(connector)
    if spec is None or action not in spec.actions:
        raise ValueError("connector/action pair is not allowlisted")


def validate_public_config(connector: str, config: dict[str, Any]) -> None:
    """Permit only non-secret behavior flags; runtime credentials always come from env."""

    allowed = {"target"} if connector == "simulated_erp" else {"top"}
    if set(config) - allowed:
        raise ValueError("workflow config contains unsupported or secret-bearing keys")
    if "top" in config:
        top = config["top"]
        if not isinstance(top, int) or isinstance(top, bool) or not 1 <= top <= 50:
            raise ValueError("top must be an integer between 1 and 50")
    if "target" in config and (
        not isinstance(config["target"], str) or not 1 <= len(config["target"]) <= 200
    ):
        raise ValueError("target must be a non-empty string up to 200 characters")


class SimulatedErpV2Connector:
    def execute(
        self,
        *,
        action: str,
        payload: dict[str, Any],
        operation_id: str,
    ) -> dict[str, Any]:
        validate_connector_action("simulated_erp", action)
        return {
            "connector": "simulated_erp",
            "action": action,
            "outcome": "accepted",
            "external_reference": f"SIM-{operation_id.replace('-', '')[:12].upper()}",
            "idempotency_token": operation_id,
            "payload": payload,
            "network_used": False,
        }


class M365MailIntakeConnector:
    """Application-only Graph reader; message bodies and attachments are never requested."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        prontoagente_tenant_id: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.prontoagente_tenant_id = prontoagente_tenant_id
        self.transport = transport

    def execute(
        self,
        *,
        action: str,
        payload: dict[str, Any],
        operation_id: str,
    ) -> dict[str, Any]:
        del operation_id
        validate_connector_action("m365_mail_intake_v1", action)
        if not m365_configured(self.prontoagente_tenant_id, self.settings):
            raise ConnectorNotConfiguredError("m365_mail_intake_v1 is not configured")
        top = payload.get("top", 25)
        if not isinstance(top, int) or isinstance(top, bool) or not 1 <= top <= 50:
            raise ConnectorExecutionError("top must be an integer between 1 and 50")

        tenant_id = self.settings.m365_tenant_id
        client_id = self.settings.m365_client_id
        client_secret = self.settings.m365_client_secret
        mailbox_id = self.settings.m365_mailbox_id
        folder_id = self.settings.m365_folder_id
        assert tenant_id and client_id and client_secret and mailbox_id and folder_id

        timeout = httpx.Timeout(self.settings.http_timeout_seconds)
        with httpx.Client(transport=self.transport, timeout=timeout) as client:
            try:
                token_response = client.post(
                    f"https://login.microsoftonline.com/"
                    f"{quote(tenant_id, safe='')}/oauth2/v2.0/token",
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "grant_type": "client_credentials",
                        "scope": GRAPH_SCOPE,
                    },
                )
            except httpx.TransportError as exc:
                raise RetryableConnectorError("M365 token service unavailable") from exc
            if token_response.status_code == 429:
                retry_header = token_response.headers.get("Retry-After", "1")
                retry_after = int(retry_header) if retry_header.isdigit() else 1
                raise RetryableConnectorError(
                    "M365 token service rate limit", retry_after=retry_after
                )
            if token_response.status_code >= 500:
                raise RetryableConnectorError("M365 token service unavailable")
            try:
                token_response.raise_for_status()
                raw_access_token = token_response.json()["access_token"]
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                raise ConnectorExecutionError("unable to acquire an M365 access token") from exc
            if not isinstance(raw_access_token, str) or not raw_access_token:
                raise ConnectorExecutionError("unable to acquire an M365 access token")
            access_token = raw_access_token

            path = (
                f"/users/{quote(mailbox_id, safe='')}/mailFolders/"
                f"{quote(folder_id, safe='')}/messages"
            )
            try:
                response = client.get(
                    f"{GRAPH_BASE_URL}{path}",
                    params={"$select": ",".join(GRAPH_MESSAGE_FIELDS), "$top": str(top)},
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            except httpx.TransportError as exc:
                raise RetryableConnectorError("Graph request failed before a response") from exc
            if response.status_code == 429:
                retry_header = response.headers.get("Retry-After", "1")
                retry_after = int(retry_header) if retry_header.isdigit() else 1
                raise RetryableConnectorError("Graph rate limit", retry_after=retry_after)
            if response.status_code >= 500:
                raise RetryableConnectorError("Graph service unavailable")
            try:
                response.raise_for_status()
                response_document = response.json()
            except (httpx.HTTPError, TypeError, ValueError) as exc:
                raise ConnectorExecutionError("Graph message read failed") from exc
            if not isinstance(response_document, dict):
                raise ConnectorExecutionError("Graph message read failed")
            raw_messages = response_document.get("value", [])
            if not isinstance(raw_messages, list):
                raise ConnectorExecutionError("Graph message read failed")
            messages = [
                {field: item.get(field) for field in GRAPH_MESSAGE_FIELDS if field in item}
                for item in raw_messages
                if isinstance(item, dict)
            ]
            return {
                "connector": "m365_mail_intake_v1",
                "action": "list_messages",
                "messages": messages,
                "count": len(messages),
            }


def runtime_connector(
    name: str,
    *,
    prontoagente_tenant_id: str | None = None,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
) -> RuntimeConnector:
    if name == "simulated_erp":
        return SimulatedErpV2Connector()
    if name == "m365_mail_intake_v1":
        return M365MailIntakeConnector(
            settings,
            prontoagente_tenant_id=prontoagente_tenant_id,
            transport=transport,
        )
    raise ConnectorExecutionError("unknown connector")
