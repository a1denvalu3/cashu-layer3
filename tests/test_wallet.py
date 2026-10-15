import asyncio
import os
import sqlite3
import time

import pytest

from tmp_cash.models import preimage_hash
from tmp_cash.token import decode_token
from tmp_cash.wallet import Wallet


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


def test_refresh_does_not_expire_coins_minted_after_snapshot(service, monkeypatch):
    wallet = Wallet(service.path / "wallet.sqlite3")
    stale = service.client.check_htlcs([])

    def delayed_snapshot(hashes):
        mint_to_wallet(service, wallet, 100)
        return stale

    monkeypatch.setattr(service.client, "check_htlcs", delayed_snapshot)
    wallet.refresh(service.client)
    assert live_amount(wallet) == 100


@pytest.mark.parametrize("sent", [False, True])
def test_refresh_recovers_false_expiry_preserving_sent_state(
    service, monkeypatch, sent
):
    wallet = Wallet(service.path / "wallet.sqlite3")
    mint_to_wallet(service, wallet)
    if sent:
        wallet.send(service.client, 16)
    pending = service.client.check_htlcs
    snapshot = pending(r["payment_hash"] for r in wallet.tokens()).model_copy(
        update={"htlcs": []}
    )
    monkeypatch.setattr(service.client, "check_htlcs", lambda hashes: snapshot)
    wallet.refresh(service.client)
    assert {r["state"] for r in wallet.tokens()} == {"EXPIRED"}
    monkeypatch.setattr(service.client, "check_htlcs", pending)
    wallet.refresh(service.client)
    assert {r["state"] for r in wallet.tokens()} == {"SENT" if sent else "LIVE"}
    assert live_amount(wallet) == (0 if sent else 16)


def test_abandon_preserves_sent_input_state(service):
    wallet = Wallet(service.path / "wallet.sqlite3")
    mint_to_wallet(service, wallet)
    sent = decode_token(wallet.send(service.client, 16))
    operation = service.client.prepare_swap(sent)
    wallet.save(operation)
    wallet.abandon(service.client, operation.id)
    assert live_amount(wallet) == 0
    assert {r["state"] for r in wallet.tokens()} == {"SENT"}


def test_abandon_cannot_delete_recovery_during_concurrent_submission(
    service, monkeypatch
):
    from tmp_cash.client import ClientError

    wallet = Wallet(service.path / "wallet.sqlite3")
    token = mint_to_wallet(service, wallet)
    operation = service.client.prepare_swap(token, [8, 8])
    wallet.save(operation)
    old_check = service.client.check
    old_finish = service.client.finish_swap

    def lost_response(op):
        old_finish(op)
        raise ClientError("response lost")

    def stale_check(nullifiers):
        snapshot = old_check(nullifiers)
        monkeypatch.setattr(service.client, "finish_swap", lost_response)
        with pytest.raises(ClientError, match="response lost"):
            wallet.retry(service.client, operation.id)
        return snapshot

    monkeypatch.setattr(service.client, "check", stale_check)
    with pytest.raises(ValueError, match="submitted"):
        wallet.abandon(service.client, operation.id)
    assert wallet.pending() == [{"id": operation.id, "kind": "swap"}]
    monkeypatch.setattr(service.client, "finish_swap", old_finish)
    wallet.retry(service.client, operation.id)
    assert live_amount(wallet) == 16


def test_interrupted_split_send_retry_preserves_exact_outgoing_amount(
    service, monkeypatch
):
    from tmp_cash.client import ClientError

    wallet = Wallet(service.path / "wallet.sqlite3")
    mint_to_wallet(service, wallet, 100)
    finish = service.client.finish_swap

    def lost_response(op):
        finish(op)
        raise ClientError("response lost")

    monkeypatch.setattr(service.client, "finish_swap", lost_response)
    with pytest.raises(ClientError, match="response lost"):
        wallet.send(service.client, 10)
    saved = wallet.pending()[0]
    monkeypatch.setattr(service.client, "finish_swap", finish)
    sent = wallet.retry(service.client, saved["id"])
    assert sent.amount == 10
    assert live_amount(wallet) == 90
    assert sum(r["amount"] for r in wallet.tokens() if r["state"] == "SENT") == 10


def test_confirmed_failed_melt_releases_wallet_inputs(service):
    from tmp_cash.client import ClientError

    wallet = Wallet(service.path / "wallet.sqlite3")
    token = mint_to_wallet(service, wallet)
    service.backend.fee_msat = 1000
    invoice = service.backend.receiver_invoice(1, os.urandom(32))
    operation = service.client.prepare_melt(token, invoice, fee_reserve=0)
    wallet.save(operation)
    with pytest.raises(ClientError, match="melt payment failed"):
        wallet.retry(service.client, operation.id)
    assert live_amount(wallet) == 16
    assert wallet.pending() == []


def test_unknown_submission_cannot_be_abandoned_when_inputs_appear_unspent(
    service, monkeypatch
):
    from tmp_cash.client import ClientError

    wallet = Wallet(service.path / "wallet.sqlite3")
    token = mint_to_wallet(service, wallet)
    operation = service.client.prepare_swap(token)
    wallet.save(operation)

    def timeout(_):
        raise ClientError("timeout before status is known")

    monkeypatch.setattr(service.client, "finish_swap", timeout)
    with pytest.raises(ClientError):
        wallet.retry(service.client, operation.id)
    assert service.client.check(
        [service.client.nullifier(p) for p in token.proofs]
    ).states == ["UNSPENT"]
    with pytest.raises(ValueError, match="submitted"):
        wallet.abandon(service.client, operation.id)
    assert wallet.pending() == [{"id": operation.id, "kind": "swap"}]


def legacy_pending_wallet(service, token, operation):
    path = service.path / "legacy-wallet.sqlite3"
    source = service.client.nullifier(token.proofs[0])
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata VALUES ('protocol','ps-wallet-v3');
            CREATE TABLE tokens (id TEXT PRIMARY KEY,token TEXT NOT NULL,state TEXT NOT NULL);
            CREATE TABLE operations (id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE operation_sources (source TEXT PRIMARY KEY REFERENCES tokens(id),operation TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE);
        """)
        conn.execute(
            "INSERT INTO tokens VALUES (?,?,'PENDING')",
            (source, token.model_dump_json()),
        )
        conn.execute(
            "INSERT INTO operations VALUES (?,?,?)",
            (operation.id, operation.kind, operation.model_dump_json()),
        )
        conn.execute(
            "INSERT INTO operation_sources VALUES (?,?)", (source, operation.id)
        )
    return Wallet(path)


def test_wallet_migration_keeps_old_submitted_recovery_material(service):
    token, _ = service.fund(16)
    operation = service.client.prepare_swap(token)
    wallet = legacy_pending_wallet(service, token, operation)
    with pytest.raises(ValueError, match="submitted"):
        wallet.abandon(service.client, operation.id)
    wallet.retry(service.client, operation.id)
    assert live_amount(wallet) == 16
    assert wallet.pending() == []


def test_legacy_melt_failure_restores_live_inputs(service):
    from tmp_cash.client import ClientError

    token, _ = service.fund(16)
    service.backend.fee_msat = 1000
    invoice = service.backend.receiver_invoice(1, os.urandom(32))
    operation = service.client.prepare_melt(token, invoice, fee_reserve=0)
    wallet = legacy_pending_wallet(service, token, operation)
    with pytest.raises(ClientError, match="melt payment failed"):
        wallet.retry(service.client, operation.id)
    assert wallet.pending() == []
    assert live_amount(wallet) == 16


def test_expired_melt_quote_releases_only_confirmed_unspent_inputs(
    service, monkeypatch
):
    from tmp_cash.client import ClientError

    wallet = Wallet(service.path / "wallet.sqlite3")
    token = mint_to_wallet(service, wallet)
    with service.backend.db.transaction() as conn:
        conn.execute(
            "UPDATE invoices SET expiry=expiry+3600 WHERE payment_hash=?",
            (token.payment_hash,),
        )
    invoice = service.backend.receiver_invoice(1, os.urandom(32))
    operation = service.client.prepare_melt(token, invoice, fee_reserve=0)
    wallet.save(operation)
    monkeypatch.setattr(time, "time", lambda: operation.quote.expiry + 1)
    with pytest.raises(ClientError, match="melt quote has expired"):
        wallet.retry(service.client, operation.id)
    assert wallet.pending() == []
    assert live_amount(wallet) == 16
    wallet.refresh(service.client)
    assert live_amount(wallet) == 16


def test_rejected_combined_melt_does_not_lock_its_remaining_live_backing(service):
    from tmp_cash.client import ClientError
    from tmp_cash.models import Token

    wallet = Wallet(service.path / "wallet.sqlite3")
    live = mint_to_wallet(service, wallet, 64)
    closed = mint_to_wallet(service, wallet, 32)
    invoice = service.backend.receiver_invoice(70, os.urandom(32))
    operation = service.client.prepare_melt(
        Token(mint=live.mint, proofs=live.proofs + closed.proofs),
        invoice,
        fee_reserve=0,
    )
    wallet.save(operation)
    asyncio.run(service.backend.cancel(closed.payment_hash))
    with pytest.raises(ClientError, match="backing HTLC is closed"):
        wallet.retry(service.client, operation.id)
    assert wallet.pending() == []
    assert live_amount(wallet) == 64


@pytest.mark.parametrize("reported", ["open", "underpaid"])
def test_ambiguous_backing_status_preserves_melt_journal_for_exact_retry(
    service, monkeypatch, reported
):
    from dataclasses import replace

    from tmp_cash.client import ClientError
    from tmp_cash.models import InvoiceState

    wallet = Wallet(service.path / "wallet.sqlite3")
    token = mint_to_wallet(service, wallet)
    invoice = service.backend.receiver_invoice(1, os.urandom(32))
    operation = service.client.prepare_melt(token, invoice, fee_reserve=0)
    wallet.save(operation)
    original = service.backend.get_invoice

    async def unavailable(payment_hash):
        found = await original(payment_hash)
        return (
            replace(found, state=InvoiceState.open)
            if reported == "open"
            else replace(found, amount_paid=0)
        )

    monkeypatch.setattr(service.backend, "get_invoice", unavailable)
    with pytest.raises(ClientError, match="503"):
        wallet.retry(service.client, operation.id)
    assert wallet.pending() == [{"id": operation.id, "kind": "melt"}]
    assert {row["state"] for row in wallet.tokens()} == {"PENDING"}
    monkeypatch.setattr(service.backend, "get_invoice", original)
    assert wallet.retry(service.client, operation.id).amount == 1
    assert live_amount(wallet) == 15


def test_legacy_ambiguous_rejection_does_not_release_melt_recovery(
    service, monkeypatch
):
    from tmp_cash.client import ClientError

    wallet = Wallet(service.path / "wallet.sqlite3")
    token = mint_to_wallet(service, wallet)
    invoice = service.backend.receiver_invoice(1, os.urandom(32))
    operation = service.client.prepare_melt(token, invoice, fee_reserve=0)
    wallet.save(operation)

    def old_server(_):
        detail = "backing HTLC is not fully accepted; its tokens are unavailable"
        raise ClientError(detail, status_code=409, detail=detail)

    monkeypatch.setattr(service.client, "finish_melt", old_server)
    with pytest.raises(ClientError, match="not fully accepted"):
        wallet.retry(service.client, operation.id)
    assert wallet.pending() == [{"id": operation.id, "kind": "melt"}]
    assert {row["state"] for row in wallet.tokens()} == {"PENDING"}


def test_competing_payout_reservation_rejection_keeps_recovery_for_retry(
    service, monkeypatch
):
    from tmp_cash.client import ClientError

    wallet = Wallet(service.path / "wallet.sqlite3")
    token = mint_to_wallet(service, wallet)
    invoice = service.backend.receiver_invoice(1, os.urandom(32))
    operation = service.client.prepare_melt(token, invoice, fee_reserve=0)
    wallet.save(operation)
    original = service.client.finish_melt

    def competing_reservation(_):
        detail = "payout invoice is already reserved or paid"
        raise ClientError(detail, status_code=409, detail=detail)

    monkeypatch.setattr(service.client, "finish_melt", competing_reservation)
    with pytest.raises(ClientError, match="already reserved"):
        wallet.retry(service.client, operation.id)
    assert wallet.pending() == [{"id": operation.id, "kind": "melt"}]
    assert {row["state"] for row in wallet.tokens()} == {"PENDING"}
    monkeypatch.setattr(service.client, "finish_melt", original)
    assert wallet.retry(service.client, operation.id).amount == 1
    assert live_amount(wallet) == 15


def test_already_paid_invoice_releases_fresh_inputs_without_a_second_payout(service):
    from tmp_cash.client import ClientError

    wallet = Wallet(service.path / "wallet.sqlite3")
    first = mint_to_wallet(service, wallet, 16)
    second = mint_to_wallet(service, wallet, 16)
    invoice = service.backend.receiver_invoice(1, os.urandom(32))
    original = service.client.prepare_melt(first, invoice, fee_reserve=0)
    wallet.save(original)
    wallet.retry(service.client, original.id)
    duplicate = service.client.prepare_melt(second, invoice, fee_reserve=0)
    wallet.save(duplicate)
    with pytest.raises(ClientError, match="payout invoice is already paid"):
        wallet.retry(service.client, duplicate.id)
    assert wallet.pending() == []
    assert live_amount(wallet) == 31
    assert service.client.check(
        [service.client.nullifier(p) for p in second.proofs]
    ).states == ["UNSPENT"]
    with service.backend.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1


def test_multi_receive_save_is_atomic_and_duplicate_mint_quotes_are_rejected(service):
    wallet = Wallet(service.path / "wallet.sqlite3")
    token = mint_to_wallet(service, wallet)
    first = service.client.prepare_swap(token)
    conflict = service.client.prepare_swap(token)
    with pytest.raises(ValueError, match="pending operation"):
        wallet.save_many([first, conflict])
    assert live_amount(wallet) == 16
    assert wallet.pending() == []
    quote = service.client.quote(8, preimage_hash(os.urandom(32).hex()))
    first_mint = service.client.prepare_mint(quote.quote)
    second_mint = service.client.prepare_mint(quote.quote)
    wallet.save(first_mint)
    with pytest.raises(ValueError, match="saved recovery request"):
        wallet.save(second_mint)
    assert wallet.pending_mint(quote.quote) == first_mint
