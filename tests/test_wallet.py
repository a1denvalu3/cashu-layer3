import asyncio
import hashlib
import os

import pytest

from cashu.token import decode_token
from cashu.wallet import Wallet


def fund_wallet(service, wallet, amount=1000):
    preimage = os.urandom(32)
    payment_hash = hashlib.sha256(preimage).hexdigest()
    quote = service.client.quote(amount, payment_hash)
    asyncio.run(service.backend.accept(payment_hash))
    operation = service.client.prepare_mint(quote.quote, preimage.hex())
    wallet.save(operation)
    return wallet.retry(service.client, operation.id, "mint")


def test_multiple_whole_claims_require_explicit_selection(service):
    wallet = Wallet(service.path / "wallet.sqlite3")
    first = fund_wallet(service, wallet)
    fund_wallet(service, wallet)
    with pytest.raises(ValueError, match="multiple tokens"):
        wallet.send(1000)
    assert all(t["state"] == "LIVE" for t in wallet.tokens())
    encoded = wallet.send(1000, service.client.nullifier(first))
    assert decode_token(encoded) == first
    assert sorted(t["state"] for t in wallet.tokens()) == ["LIVE", "SENT"]


def test_unswapped_sent_token_can_be_recovered_or_received_back(service):
    wallet = Wallet(service.path / "wallet.sqlite3")
    token = fund_wallet(service, wallet)
    token_id = service.client.nullifier(token)
    encoded = wallet.send(1000)
    with pytest.raises(ValueError, match="no available token"):
        wallet.send(1000)
    wallet = Wallet(service.path / "wallet.sqlite3")
    assert wallet.send(1000, token_id) == encoded
    with pytest.raises(ValueError, match="does not match"):
        wallet.send(1, token_id)
    operation = service.client.prepare_swap(decode_token(encoded))
    wallet.save(operation)
    received = wallet.retry(service.client, operation.id, "swap")
    assert received.amount == token.amount
    assert received.payment_hash == token.payment_hash
    assert received.preimage == token.preimage
    assert received.credential != token.credential
    assert sorted(t["state"] for t in wallet.tokens()) == ["LIVE", "SPENT"]
    assert service.client.check([token_id]).states == ["SPENT"]


def test_legacy_token_needs_its_original_preimage_before_sending(service):
    wallet = Wallet(service.path / "wallet.sqlite3")
    token = fund_wallet(service, wallet)
    token_id = service.client.nullifier(token)
    legacy = token.model_copy(update={"preimage": None})
    with wallet.db.transaction() as conn:
        conn.execute(
            "UPDATE tokens SET token=? WHERE id=?", (legacy.model_dump_json(), token_id)
        )
    with pytest.raises(ValueError, match="no HTLC preimage"):
        wallet.send(token.amount)
    with pytest.raises(ValueError, match="preimage does not match"):
        wallet.send(token.amount, preimage="11" * 32)
    assert wallet.token(token_id) == legacy
    encoded = wallet.send(token.amount, preimage=token.preimage)
    assert decode_token(encoded) == token
    assert wallet.send(token.amount, token_id) == encoded
