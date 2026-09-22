"""AES-GCM sealing for queued AI input containing transient untrusted metadata."""

import base64
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from prontoagente.canonical import canonical_json

NONCE_BYTES = 12


class CryptoConfigurationError(Exception):
    pass


class SealedInputError(Exception):
    pass


def _key(encoded_key: str | None) -> bytes:
    if not encoded_key:
        raise CryptoConfigurationError("AI encryption key is not configured")
    try:
        key = base64.b64decode(encoded_key, validate=True)
    except ValueError as exc:
        raise CryptoConfigurationError("AI encryption key is invalid") from exc
    if len(key) != 32:
        raise CryptoConfigurationError("AI encryption key must decode to 32 bytes")
    return key


def _associated_data(*, tenant_id: str, operation_id: str, key_id: str) -> bytes:
    return f"prontoagente-ai-v1:{tenant_id}:{operation_id}:{key_id}".encode()


def seal_json(
    value: dict[str, Any],
    *,
    tenant_id: str,
    operation_id: str,
    key_id: str | None,
    encoded_key: str | None,
) -> tuple[str, bytes]:
    if not key_id:
        raise CryptoConfigurationError("AI encryption key id is not configured")
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(_key(encoded_key)).encrypt(
        nonce,
        canonical_json(value).encode(),
        _associated_data(
            tenant_id=tenant_id, operation_id=operation_id, key_id=key_id
        ),
    )
    return key_id, nonce + ciphertext


def open_json(
    sealed: bytes,
    *,
    tenant_id: str,
    operation_id: str,
    expected_key_id: str,
    configured_key_id: str | None,
    encoded_key: str | None,
) -> dict[str, Any]:
    if configured_key_id != expected_key_id:
        raise SealedInputError("sealed input key is unavailable")
    if len(sealed) <= NONCE_BYTES:
        raise SealedInputError("sealed input is malformed")
    nonce, ciphertext = sealed[:NONCE_BYTES], sealed[NONCE_BYTES:]
    try:
        plaintext = AESGCM(_key(encoded_key)).decrypt(
            nonce,
            ciphertext,
            _associated_data(
                tenant_id=tenant_id,
                operation_id=operation_id,
                key_id=expected_key_id,
            ),
        )
        document = json.loads(plaintext)
    except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SealedInputError("sealed input could not be authenticated") from exc
    if not isinstance(document, dict):
        raise SealedInputError("sealed input is not an object")
    return document
