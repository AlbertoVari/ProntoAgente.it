"""Network-free ERP connector used by this vertical slice."""

from typing import Any, Final


class SimulatedConnector:
    """Pure deterministic simulation; it deliberately has no network client."""

    name: Final[str] = "simulated_erp"

    def simulate(
        self,
        proposal: dict[str, Any],
        proposal_hash: str,
        operation_id: str,
    ) -> dict[str, Any]:
        """Return a deterministic acknowledgement keyed by the operation id."""

        del proposal  # The hash identifies the exact proposal while avoiding PII in the result.
        return {
            "connector": self.name,
            "outcome": "accepted",
            "external_reference": f"SIM-{operation_id.replace('-', '')[:12].upper()}",
            "idempotency_token": operation_id,
            "proposal_hash": proposal_hash,
            "network_used": False,
        }
