import asyncio
import json
import os
import sys

import pytest

from tmp_cash import cli
from tmp_cash.models import preimage_hash
from tmp_cash.token import decode_token, encode_token
from tmp_cash.wallet import Wallet


def run(monkeypatch, service, path, *arguments):
    monkeypatch.setattr(cli, "Client", lambda url: service.client)
    monkeypatch.setattr(service.client, "close", lambda: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tmp.cash",
            "--mint",
            service.client.mint_url,
            "--wallet",
            str(path),
            *arguments,
        ],
    )
    cli.main()


def test_cli_mint_send_receive_and_pay_with_fee_change(service, monkeypatch, capsys):
    alice = service.path / "alice.sqlite3"
    bob = service.path / "bob.sqlite3"
    preimage = os.urandom(32).hex()
    quote = service.client.quote(1000, preimage_hash(preimage))
    asyncio.run(service.backend.accept(quote.payment_hash))
    run(
        monkeypatch,
        service,
        alice,
        "mint",
        "--quote",
        quote.quote,
        "--preimage",
        preimage,
    )
    assert "Minted 1000 sats" in capsys.readouterr().err
    run(monkeypatch, service, alice, "send", "10")
    encoded = capsys.readouterr().out.strip()
    assert decode_token(encoded).amount == 10
    run(monkeypatch, service, bob, "receive", encoded)
    assert "Backing deadline" in capsys.readouterr().err
    invoice = service.backend.receiver_invoice(9, os.urandom(32))
    run(monkeypatch, service, bob, "pay", invoice, "--fee-reserve", "1")
    response = json.loads(capsys.readouterr().out)
    assert response["amount"] == 9 and response["state"] == "PAID"
    run(monkeypatch, service, bob, "balance")
    assert capsys.readouterr().out.strip() == "Balance: 1"
    run(monkeypatch, service, alice, "balance")
    assert capsys.readouterr().out.strip() == "Balance: 990"


def test_decode_does_not_create_wallet(service, monkeypatch, capsys):
    token, _ = service.fund(16)
    path = service.path / "unused.sqlite3"
    run(monkeypatch, service, path, "decode", encode_token(token))
    assert json.loads(capsys.readouterr().out)["version"] == 3
    assert not path.exists()


def test_htlcs_checks_only_wallet_or_explicit_hashes(service, monkeypatch, capsys):
    path = service.path / "wallet.sqlite3"
    token, _ = service.fund(16)
    run(monkeypatch, service, path, "htlcs")
    assert json.loads(capsys.readouterr().out)["htlcs"] == []
    run(monkeypatch, service, path, "htlcs", token.payment_hash)
    assert [
        h["payment_hash"] for h in json.loads(capsys.readouterr().out)["htlcs"]
    ] == [token.payment_hash]
    run(monkeypatch, service, path, "receive", encode_token(token))
    capsys.readouterr()
    run(monkeypatch, service, path, "htlcs")
    assert [
        h["payment_hash"] for h in json.loads(capsys.readouterr().out)["htlcs"]
    ] == [token.payment_hash]


def test_invalid_send_reports_actionable_error(service, monkeypatch, capsys):
    path = service.path / "empty.sqlite3"
    with pytest.raises(SystemExit) as exc:
        run(monkeypatch, service, path, "send", "10")
    assert exc.value.code == 1
    assert "one HTLC" in capsys.readouterr().err


def test_receive_journals_all_backings_before_first_remote_spend(
    service, monkeypatch, capsys
):
    from tmp_cash.client import ClientError
    from tmp_cash.models import Token

    first, _ = service.fund(32, os.urandom(32))
    second, _ = service.fund(64, os.urandom(32))
    incoming = Token(mint=first.mint, proofs=first.proofs + second.proofs)
    path = service.path / "receiver.sqlite3"
    finish = service.client.finish_swap

    def lost_response(operation):
        finish(operation)
        raise ClientError("response lost")

    monkeypatch.setattr(service.client, "finish_swap", lost_response)
    with pytest.raises(SystemExit):
        run(monkeypatch, service, path, "receive", encode_token(incoming))
    capsys.readouterr()
    wallet = Wallet(path)
    assert len(wallet.pending()) == 2
    assert (
        sum(row["amount"] for row in wallet.tokens() if row["state"] == "PENDING") == 96
    )
    monkeypatch.setattr(service.client, "finish_swap", finish)
    for operation in wallet.pending():
        wallet.retry(service.client, operation["id"])
    assert sum(row["amount"] for row in wallet.tokens() if row["state"] == "LIVE") == 96
    assert wallet.pending() == []
