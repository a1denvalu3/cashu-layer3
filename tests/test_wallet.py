import asyncio
import os
import sqlite3

import pytest

from cashu.models import preimage_hash
from cashu.token import decode_token
from cashu.wallet import Wallet


def mint_to_wallet(service, wallet, amount=16, preimage=None):
    preimage = preimage or os.urandom(32).hex()
    quote = service.client.quote(amount, preimage_hash(preimage))
    asyncio.run(service.backend.accept(quote.payment_hash))
    operation = service.client.prepare_mint(quote.quote, preimage)
    wallet.save(operation)
    return wallet.retry(service.client, operation.id)


def live_amount(wallet):
    return sum(r["amount"] for r in wallet.tokens() if r["state"] == "LIVE")


def test_send_splits_exact_amount_and_retains_change(service):
    alice = Wallet(service.path / "alice.sqlite3")
    mint_to_wallet(service, alice, 1000)
    encoded = alice.send(service.client, 10)
    incoming = decode_token(encoded)
    assert incoming.amount == 10
    assert [p.amount for p in incoming.proofs] == [8, 2]
    assert live_amount(alice) == 990
    ids = [r["id"] for r in alice.tokens() if r["state"] == "SENT"]
    assert alice.send(service.client, 10, token_ids=ids) == encoded
    bob = Wallet(service.path / "bob.sqlite3")
    operation = service.client.prepare_swap(incoming)
    bob.save(operation)
    assert bob.retry(service.client, operation.id).amount == 10
    assert live_amount(bob) == 10


def test_combined_payment_reserves_all_inputs_and_saves_fee_change(service):
    wallet = Wallet(service.path / "wallet.sqlite3")
    mint_to_wallet(service, wallet, 8)
    mint_to_wallet(service, wallet, 8)
    invoice = service.backend.receiver_invoice(10, os.urandom(32))
    quote = service.client.melt_quote(invoice, 1)
    token = wallet.select_token(
        quote.amount + quote.fee_reserve, service.client.mint_url
    )
    assert len({p.payment_hash for p in token.proofs}) == 2
    operation = service.client.prepare_melt(token, quote)
    wallet.save(operation)
    assert live_amount(wallet) == 0
    assert all(r["state"] == "PENDING" for r in wallet.tokens())
    response = wallet.retry(service.client, operation.id)
    assert response.amount == 10
    assert live_amount(wallet) == 6
    assert wallet.pending() == []


def test_overlapping_local_operations_cannot_reserve_one_input_twice(service):
    wallet = Wallet(service.path / "wallet.sqlite3")
    token = mint_to_wallet(service, wallet)
    first = service.client.prepare_swap(token, [8, 8])
    second = service.client.prepare_swap(token, [4, 4, 4, 4])
    wallet.save(first)
    wallet.save(first)
    with pytest.raises(ValueError, match="pending operation"):
        wallet.save(second)
    assert wallet.pending() == [{"id": first.id, "kind": "swap"}]
    wallet.abandon(service.client, first.id)
    assert live_amount(wallet) == 16


def test_local_swap_cannot_be_abandoned_after_remote_spend(service):
    wallet = Wallet(service.path / "wallet.sqlite3")
    token = mint_to_wallet(service, wallet)
    operation = service.client.prepare_swap(token, [8, 8])
    wallet.save(operation)
    service.client.finish_swap(operation)
    with pytest.raises(ValueError, match="spent or reserved"):
        wallet.abandon(service.client, operation.id)
    wallet.retry(service.client, operation.id)
    assert live_amount(wallet) == 16


def test_refresh_marks_terminal_claims_expired_without_retaining_balance(service):
    wallet = Wallet(service.path / "wallet.sqlite3")
    token = mint_to_wallet(service, wallet)
    asyncio.run(service.backend.cancel(token.payment_hash))
    wallet.refresh(service.client)
    assert live_amount(wallet) == 0
    assert all(r["state"] == "EXPIRED" for r in wallet.tokens())


def test_send_requires_one_htlc_and_explicit_choice_if_ambiguous(service):
    wallet = Wallet(service.path / "wallet.sqlite3")
    a = mint_to_wallet(service, wallet, 8)
    mint_to_wallet(service, wallet, 8)
    with pytest.raises(ValueError, match="one HTLC"):
        wallet.send(service.client, 10)
    with pytest.raises(ValueError, match="ambiguous"):
        wallet.send(service.client, 4)
    assert (
        decode_token(wallet.send(service.client, 4, payment_hash=a.payment_hash)).amount
        == 4
    )
    assert live_amount(wallet) == 12


def test_wallet_rejects_previous_protocol(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tokens(id TEXT PRIMARY KEY,token TEXT,state TEXT)")
    with pytest.raises(ValueError, match="previous protocol"):
        Wallet(path)
