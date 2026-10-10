import base64
import json

import pytest

from cashu.models import Token
from cashu.token import MAX_TOKEN_LENGTH, decode_token, encode_token


def test_token_bundle_roundtrips_all_denominations_and_backing_secrets(service):
    token, _ = service.fund(12345)
    encoded = encode_token(token)
    assert decode_token(encoded) == token
    assert decode_token(f"  cashu:{encoded}\n") == token
    assert "=" not in encoded
    service.client.credentials(decode_token(encoded))


@pytest.mark.parametrize(
    "encoded",
    ["", "cashuAabc", "cashuBabc", "cashuPS", "cashuPS!", "cashuPSA", "cashuPSe30"],
)
def test_invalid_token_strings_are_rejected(encoded):
    with pytest.raises(ValueError):
        decode_token(encoded)


@pytest.mark.parametrize(
    "update", [{"version": 2}, {"version": 1}, {"unexpected": "field"}]
)
def test_previous_versions_and_extra_fields_are_rejected(service, update):
    token, _ = service.fund(16)
    payload = token.model_dump() | update
    encoded = (
        "cashuPS" + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    )
    with pytest.raises(ValueError):
        decode_token(encoded)


def test_token_size_is_bounded():
    with pytest.raises(ValueError, match="too large"):
        decode_token("cashuPS" + "A" * MAX_TOKEN_LENGTH)


def test_duplicate_and_wrong_preimage_proofs_are_rejected(service):
    token, _ = service.fund(16)
    with pytest.raises(ValueError, match="duplicate"):
        Token(mint=token.mint, proofs=token.proofs * 2)
    payload = token.model_dump()
    payload["proofs"][0]["preimage"] = "11" * 32
    encoded = (
        "cashuPS" + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    )
    with pytest.raises(ValueError):
        decode_token(encoded)
