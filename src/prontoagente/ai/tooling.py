"""Strict read-only tool contract and local validation for AI order extraction."""

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from prontoagente.v2.demo_connectors import (
    MAX_LINES,
    MAX_QUANTITY,
    MAX_UNIT_PRICE,
    OrderDocument,
    OrderLine,
)

TOOL_NAME = "demo_erp_reconcile_v1"
TOOL_DESCRIPTION = "Extract one order for local read-only reconciliation."
Code = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Z0-9][A-Z0-9._-]*$",
    ),
]
Money = Annotated[
    str,
    StringConstraints(pattern=r"^(?:0|[1-9][0-9]{0,7})\.[0-9]{2}$"),
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class OrderExtractionLineV1(_StrictModel):
    sku: Code
    quantity: int = Field(ge=1, le=MAX_QUANTITY)
    unit_price: Money

    @field_validator("unit_price", mode="before")
    @classmethod
    def exact_money_string(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("unit price must be a decimal string")
        integer, separator, fraction = value.partition(".")
        if (
            separator != "."
            or len(fraction) != 2
            or not integer.isascii()
            or not fraction.isascii()
            or not integer.isdigit()
            or not fraction.isdigit()
            or (len(integer) > 1 and integer.startswith("0"))
        ):
            raise ValueError("unit price must have exactly two decimals")
        amount = Decimal(value)
        if not Decimal("0.00") < amount <= MAX_UNIT_PRICE:
            raise ValueError("unit price is outside the supported range")
        return value


class OrderExtractionV1(_StrictModel):
    order_id: Code
    customer_id: Code
    currency: Literal["EUR"]
    lines: list[OrderExtractionLineV1] = Field(min_length=1, max_length=MAX_LINES)

    @model_validator(mode="after")
    def unique_skus(self) -> "OrderExtractionV1":
        skus = [line.sku for line in self.lines]
        if len(skus) != len(set(skus)):
            raise ValueError("SKUs must be unique")
        return self


ORDER_EXTRACTION_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "order_id": {"type": "string", "pattern": r"^[A-Z0-9][A-Z0-9._-]{0,63}$"},
        "customer_id": {
            "type": "string",
            "pattern": r"^[A-Z0-9][A-Z0-9._-]{0,63}$",
        },
        "currency": {"type": "string", "enum": ["EUR"]},
        "lines": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_LINES,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "sku": {
                        "type": "string",
                        "pattern": r"^[A-Z0-9][A-Z0-9._-]{0,63}$",
                    },
                    "quantity": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_QUANTITY,
                    },
                    "unit_price": {
                        "type": "string",
                        "pattern": r"^(?:0|[1-9][0-9]{0,7})\.[0-9]{2}$",
                    },
                },
                "required": ["sku", "quantity", "unit_price"],
            },
        },
    },
    "required": ["order_id", "customer_id", "currency", "lines"],
}


class ToolValidationFailure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def validate_single_tool_call(
    tool_calls: tuple[tuple[str, dict[str, Any]], ...],
) -> OrderExtractionV1:
    if len(tool_calls) != 1:
        raise ToolValidationFailure("tool_call_count_invalid")
    name, arguments = tool_calls[0]
    if name != TOOL_NAME:
        raise ToolValidationFailure("tool_not_allowlisted")
    try:
        return OrderExtractionV1.model_validate(arguments)
    except ValueError as exc:
        raise ToolValidationFailure("tool_arguments_invalid") from exc


def to_order_document(extraction: OrderExtractionV1) -> OrderDocument:
    return OrderDocument(
        order_id=extraction.order_id,
        customer_id=extraction.customer_id,
        currency=extraction.currency,
        lines=tuple(
            sorted(
                (
                    OrderLine(
                        sku=line.sku,
                        quantity=line.quantity,
                        unit_price=Decimal(line.unit_price),
                    )
                    for line in extraction.lines
                ),
                key=lambda item: item.sku,
            )
        ),
    )
