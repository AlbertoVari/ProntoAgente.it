"""HTTP request dependencies."""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Header


@dataclass(frozen=True, slots=True)
class MutationContext:
    idempotency_key: str
    actor_id: str


def mutation_context(
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=8,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        ),
    ],
    actor_id: Annotated[
        str,
        Header(
            alias="X-Actor-Id",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
        ),
    ],
) -> MutationContext:
    """Require traceability and replay protection metadata on every mutation."""

    return MutationContext(idempotency_key=idempotency_key, actor_id=actor_id)

