import base64
import json

import pytest
from conftest import PREIMAGE

from cashu.token import decode_token, encode_token


def test_token_string_preserves_the_whole_claim_and_spending_secret(service):
    token, _ = service.fund(12345)
    encoded = encode_token(token)
    assert encoded.startswith("cashuPS")
    assert "=" not in encoded
    assert decode_token(encoded) == token
    assert decode_token(encoded).preimage == PREIMAGE.hex()
    assert decode_token(f"  cashu:{encoded}\n") == token
    service.client.credential(decode_token(encoded))


@pytest.mark.parametrize(
    "encoded",
    ["", "cashuAabc", "cashuBabc", "cashuPS", "cashuPS!", "cashuPSA", "cashuPSe30"],
)
def test_invalid_token_strings_are_rejected(encoded):
    with pytest.raises(ValueError):
        decode_token(encoded)


def test_unknown_token_versions_and_extra_fields_are_rejected(service):
    token, _ = service.fund()
    for update in ({"version": 1}, {"unexpected": "field"}):
        raw = token.model_dump()
        raw.update(update)
        encoded = (
            "cashuPS" + base64.urlsafe_b64encode(json.dumps(raw).encode()).decode()
        )
        with pytest.raises(ValueError):
            decode_token(encoded)


def test_token_size_is_bounded():
    with pytest.raises(ValueError, match="too large"):
        decode_token("cashuPS" + "A" * 16384)


@pytest.mark.parametrize("preimage", ["11" * 32, "11" * 31])
def test_received_token_rejects_a_missing_or_wrong_preimage_encoding(service, preimage):
    token, _ = service.fund()
    encoded = encode_token(token.model_copy(update={"preimage": preimage}))
    with pytest.raises(ValueError, match="invalid cashuPS token"):
        decode_token(encoded)
