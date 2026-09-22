"""Pure offline parser and reconciliation logic for the synthetic order showcase."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final, Literal

from prontoagente.canonical import sha256_digest

DEMO_MAIL_SOURCE: Final = "demo_mailbox_v1"
PARSER_VERSION: Final = "PA1"
CATALOG_VERSION: Final = "demo-erp-catalog-v1"
MAX_SUBJECT_LENGTH: Final = 512
MAX_LINES: Final = 20
MAX_QUANTITY: Final = 999_999
MAX_UNIT_PRICE: Final = Decimal("99999999.99")

_CODE_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,63}\Z", re.ASCII)
_QUANTITY_PATTERN = re.compile(r"[1-9][0-9]{0,5}\Z", re.ASCII)
_PRICE_PATTERN = re.compile(r"(?:0|[1-9][0-9]{0,7})\.[0-9]{2}\Z", re.ASCII)

LineStatus = Literal[
    "MATCH",
    "QTY_MISMATCH",
    "PRICE_MISMATCH",
    "MISSING_IN_ERP",
    "MISSING_IN_EMAIL",
]
ReconciliationOutcome = Literal["MATCHED", "REVIEW_REQUIRED", "ERP_ORDER_NOT_FOUND"]


class DemoOrderParseError(ValueError):
    """The synthetic subject does not satisfy the closed PA1 grammar."""


@dataclass(frozen=True, slots=True)
class OrderLine:
    sku: str
    quantity: int
    unit_price: Decimal


@dataclass(frozen=True, slots=True)
class OrderDocument:
    order_id: str
    customer_id: str
    currency: str
    lines: tuple[OrderLine, ...]


def _line(sku: str, quantity: int, unit_price: str) -> OrderLine:
    return OrderLine(sku=sku, quantity=quantity, unit_price=Decimal(unit_price))


ERP_CATALOG: Final[Mapping[str, OrderDocument]] = MappingProxyType(
    {
        "PO-1001": OrderDocument(
            order_id="PO-1001",
            customer_id="CUST-42",
            currency="EUR",
            lines=(
                _line("SKU-A", 2, "49.90"),
                _line("SKU-B", 1, "10.00"),
            ),
        ),
        "PO-1002": OrderDocument(
            order_id="PO-1002",
            customer_id="CUST-42",
            currency="EUR",
            lines=(
                _line("SKU-A", 3, "49.90"),
                _line("SKU-C", 1, "5.00"),
            ),
        ),
    }
)


def source_reference_hash(
    *, source_connector: str, message_id: str, internet_message_id: str
) -> str:
    """Derive a stable opaque identity without persisting mail metadata."""

    return sha256_digest(
        {
            "source_connector": source_connector,
            "message_id": message_id,
            "internet_message_id": internet_message_id,
        }
    )


def _parse_code(value: str, *, field: str) -> str:
    if _CODE_PATTERN.fullmatch(value) is None:
        raise DemoOrderParseError(f"{field} contains unsupported characters or length")
    return value


def parse_order_subject(subject: str) -> OrderDocument:
    """Parse the exact, versioned PA1 subject grammar with closed numeric bounds."""

    if not 1 <= len(subject) <= MAX_SUBJECT_LENGTH:
        raise DemoOrderParseError("subject length is outside the supported range")
    if not subject.isascii() or any(
        ord(character) < 32 or ord(character) > 126 for character in subject
    ):
        raise DemoOrderParseError("subject must contain printable ASCII only")

    segments = subject.split(";")
    if len(segments) != 5 or segments[0] != PARSER_VERSION:
        raise DemoOrderParseError("subject does not use the PA1 grammar")
    expected_keys = ("order", "customer", "currency", "lines")
    values: dict[str, str] = {}
    for expected_key, segment in zip(expected_keys, segments[1:], strict=True):
        prefix = f"{expected_key}="
        if not segment.startswith(prefix):
            raise DemoOrderParseError("subject fields are missing or out of order")
        values[expected_key] = segment.removeprefix(prefix)

    order_id = _parse_code(values["order"], field="order")
    customer_id = _parse_code(values["customer"], field="customer")
    if values["currency"] != "EUR":
        raise DemoOrderParseError("only EUR is supported by the showcase")

    raw_lines = values["lines"].split(",")
    if not 1 <= len(raw_lines) <= MAX_LINES:
        raise DemoOrderParseError("the subject must contain between 1 and 20 lines")
    parsed_lines: list[OrderLine] = []
    seen_skus: set[str] = set()
    for raw_line in raw_lines:
        parts = raw_line.split(":")
        if len(parts) != 3:
            raise DemoOrderParseError("each line must be SKU:quantity:unit_price")
        sku = _parse_code(parts[0], field="sku")
        if sku in seen_skus:
            raise DemoOrderParseError("SKUs must be unique")
        seen_skus.add(sku)
        if _QUANTITY_PATTERN.fullmatch(parts[1]) is None:
            raise DemoOrderParseError("quantity must be a bounded positive integer")
        quantity = int(parts[1])
        if quantity > MAX_QUANTITY:
            raise DemoOrderParseError("quantity exceeds the supported bound")
        if _PRICE_PATTERN.fullmatch(parts[2]) is None:
            raise DemoOrderParseError("unit price must have exactly two decimals")
        unit_price = Decimal(parts[2])
        if not Decimal("0.00") < unit_price <= MAX_UNIT_PRICE:
            raise DemoOrderParseError("unit price is outside the supported range")
        parsed_lines.append(
            OrderLine(sku=sku, quantity=quantity, unit_price=unit_price)
        )

    return OrderDocument(
        order_id=order_id,
        customer_id=customer_id,
        currency="EUR",
        lines=tuple(sorted(parsed_lines, key=lambda item: item.sku)),
    )


def canonical_order(order: OrderDocument) -> dict[str, Any]:
    """Return the only payload shape accepted by the demo preparation."""

    return {
        "order_id": order.order_id,
        "customer_id": order.customer_id,
        "currency": order.currency,
        "lines": [
            {
                "sku": line.sku,
                "quantity": line.quantity,
                "unit_price": format(line.unit_price, ".2f"),
            }
            for line in order.lines
        ],
    }


def _line_value(line: OrderLine | None) -> dict[str, Any] | None:
    if line is None:
        return None
    return {
        "quantity": line.quantity,
        "unit_price": format(line.unit_price, ".2f"),
    }


def reconcile_order(
    order: OrderDocument,
    *,
    catalog: Mapping[str, OrderDocument] = ERP_CATALOG,
) -> dict[str, Any]:
    """Compare one parsed order with an immutable local catalog deterministically."""

    erp_order = catalog.get(order.order_id)
    email_lines = {line.sku: line for line in order.lines}
    erp_lines = {} if erp_order is None else {line.sku: line for line in erp_order.lines}
    rows: list[dict[str, Any]] = []
    for sku in sorted(email_lines.keys() | erp_lines.keys()):
        email_line = email_lines.get(sku)
        erp_line = erp_lines.get(sku)
        status: LineStatus
        if erp_line is None:
            status = "MISSING_IN_ERP"
        elif email_line is None:
            status = "MISSING_IN_EMAIL"
        elif email_line.quantity != erp_line.quantity:
            status = "QTY_MISMATCH"
        elif email_line.unit_price != erp_line.unit_price:
            status = "PRICE_MISMATCH"
        else:
            status = "MATCH"
        rows.append(
            {
                "sku": sku,
                "status": status,
                "email": _line_value(email_line),
                "erp": _line_value(erp_line),
            }
        )

    if erp_order is None:
        outcome: ReconciliationOutcome = "ERP_ORDER_NOT_FOUND"
        customer_match: bool | None = None
        currency_match: bool | None = None
    else:
        customer_match = order.customer_id == erp_order.customer_id
        currency_match = order.currency == erp_order.currency
        outcome = (
            "MATCHED"
            if customer_match
            and currency_match
            and all(row["status"] == "MATCH" for row in rows)
            else "REVIEW_REQUIRED"
        )
    return {
        "outcome": outcome,
        "catalog_version": CATALOG_VERSION,
        "order_id": order.order_id,
        "customer_match": customer_match,
        "currency_match": currency_match,
        "rows": rows,
    }
