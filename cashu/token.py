"""Pasteable bearer tokens for this experimental PS protocol."""

import base64
import binascii
import re

from .models import Token

TOKEN_PREFIX = "cashuPS"
MAX_TOKEN_LENGTH = 16384


def encode_token(token: Token) -> str:
    payload = base64.urlsafe_b64encode(token.model_dump_json().encode()).decode()
    return TOKEN_PREFIX + payload.rstrip("=")


def decode_token(encoded: str) -> Token:
    encoded = encoded.strip()
    if encoded.startswith("cashu:"):
        encoded = encoded[len("cashu:") :]
    if not encoded.startswith(TOKEN_PREFIX):
        raise ValueError("expected a cashuPS token")
    if len(encoded) > MAX_TOKEN_LENGTH:
        raise ValueError("token is too large")
    payload = encoded[len(TOKEN_PREFIX) :]
    if not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", payload):
        raise ValueError("invalid cashuPS token encoding")
    try:
        raw = base64.b64decode(
            payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True
        )
        return Token.model_validate_json(raw)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid cashuPS token") from exc
