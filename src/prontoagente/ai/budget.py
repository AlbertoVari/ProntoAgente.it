"""Conservative pre-I/O token bounds for the fixed M3 provider contract."""

from typing import Any, Final

from prontoagente.canonical import canonical_json

PROVIDER_PROTOCOL_OVERHEAD_BYTES: Final = 512


def provider_input_token_upper_bound(
    *,
    system_prompt: str,
    user_data: str,
    tool_name: str,
    tool_description: str,
    tool_schema: dict[str, Any],
) -> int:
    """Bound tokens by UTF-8 bytes, including provider framing allowance."""

    provider_visible_input = canonical_json(
        {
            "system": system_prompt,
            "user": user_data,
            "tool": {
                "name": tool_name,
                "description": tool_description,
                "schema": tool_schema,
            },
        }
    )
    return len(provider_visible_input.encode("utf-8")) + PROVIDER_PROTOCOL_OVERHEAD_BYTES
