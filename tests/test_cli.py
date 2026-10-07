import hashlib
import json
import os
import select
import signal
import socket
import sqlite3
import subprocess
import sys
import time

import httpx
import pytest

from cashu.crypto.ps import Credential
from cashu.models import Token
from cashu.token import decode_token, encode_token
from cashu.wallet import Wallet

PYTHON = sys.executable


def read_quote(process, timeout=10):
    """Read the flushed initial JSON while the mint command is still running."""
    output = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if select.select([process.stdout], [], [], 0.1)[0]:
            chunk = os.read(process.stdout.fileno(), 4096).decode()
            assert chunk, "mint command exited before printing its quote"
            output += chunk
            try:
                quote, end = json.JSONDecoder().raw_decode(output)
                assert not output[end:].strip()
                return quote
            except json.JSONDecodeError:
                pass
    raise AssertionError("mint command did not flush its quote before waiting")


@pytest.fixture
def cli_environment(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = subprocess.Popen(
        [
            str(PYTHON),
            "-m",
            "cashu.server",
            "--backend",
            "fake",
            "--port",
            str(port),
            "--data",
            str(tmp_path / "mint"),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    endpoint = f"http://127.0.0.1:{port}"
    backend_path = str(tmp_path / "mint/fake.sqlite3")
    commands = []

    def command(wallet, args, explicit_mint):
        return [
            str(PYTHON),
            "-m",
            "cashu.cli",
            *(["--mint", endpoint] if explicit_mint else []),
            "--wallet",
            str(tmp_path / f"{wallet}.sqlite3"),
            *args,
        ]

    def start(wallet, *args):
        process = subprocess.Popen(
            command(wallet, args, True),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        commands.append(process)
        return process

    def cli(wallet, *args, status=0, explicit_mint=True):
        result = subprocess.run(
            command(wallet, args, explicit_mint),
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == status, result.stderr
        return result.stdout if status == 0 else result.stderr

    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if httpx.get(endpoint + "/v1/info").status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            assert server.poll() is None, "server exited during startup"
            time.sleep(0.1)
        else:
            raise AssertionError("mint server did not start")
        yield endpoint, backend_path, cli, start
    finally:
        for process in commands:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=10)
        server.terminate()
        server.wait(timeout=10)


def test_real_server_and_two_cli_wallets(tmp_path, cli_environment):
    endpoint, backend_path, cli, start = cli_environment
    preimage = os.urandom(32)
    payment_hash = hashlib.sha256(preimage).hexdigest()
    minting = start("alice", "mint", "1000", payment_hash, "--preimage", preimage.hex())
    quote = read_quote(minting)
    assert quote["payment_hash"] == payment_hash
    assert quote["state"] == "OPEN"
    assert minting.poll() is None
    assert len(json.loads(cli("alice", "pending"))) == 1
    # Creating the funding HODL invoice requires no receiver invoice.
    with sqlite3.connect(backend_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM receivers").fetchone()[0] == 0
    assert (
        cli(
            "alice",
            "dev-accept",
            payment_hash,
            "--backend-db",
            backend_path,
        ).strip()
        == "ACCEPTED"
    )
    output, errors = minting.communicate(timeout=20)
    assert minting.returncode == 0, errors
    token = json.loads(output)
    assert token["preimage"] == preimage.hex()
    alice_id = json.loads(cli("alice", "list"))[0]["id"]
    assert cli("alice", "balance").strip() == "Balance: 1000"
    assert "already issued" in cli("alice", "mint", "1000", payment_hash, status=1)
    assert json.loads(cli("alice", "pending")) == []
    assert "cannot be split" in cli("alice", "send", "1", status=1)
    encoded = cli("alice", "send", "1000").strip()
    assert encoded.startswith("cashuPS")
    assert decode_token(encoded).model_dump() == token
    assert cli("alice", "balance").strip() == "Balance: 0"
    assert json.loads(cli("alice", "pending", "--sent"))[0]["id"] == alice_id
    assert "no available token" in cli("alice", "send", "1000", status=1)
    assert cli("alice", "send", "1000", "--token-id", alice_id).strip() == encoded
    assert json.loads(cli("alice", "decode", encoded)) == token
    tampered = encode_token(
        decode_token(encoded).model_copy(update={"amount": 1000000})
    )
    assert "does not match" in cli("bob", "receive", tampered, status=1)
    received = json.loads(cli("bob", "receive", encoded, explicit_mint=False))
    assert received["payment_hash"] == token["payment_hash"]
    assert received["amount"] == token["amount"]
    assert received["credential"] != token["credential"]
    assert received["preimage"] == preimage.hex()
    assert cli("bob", "balance").strip() == "Balance: 1000"
    unrelated = json.loads(
        cli("bob", "dev-invoice", "1000", "--backend-db", backend_path)
    )
    assert unrelated["payment_hash"] != payment_hash
    receipt = json.loads(cli("bob", "pay", unrelated["request"], explicit_mint=False))
    assert receipt["state"] == "SETTLED"
    assert receipt["payment_hash"] == payment_hash
    assert receipt["payout_hash"] == unrelated["payment_hash"]
    assert (
        hashlib.sha256(bytes.fromhex(receipt["payout_preimage"])).hexdigest()
        == unrelated["payment_hash"]
    )
    assert all(t["state"] == "SPENT" for t in json.loads(cli("bob", "list")))
    assert json.loads(cli("bob", "pending")) == []
    assert os.stat(tmp_path / "bob.sqlite3").st_mode & 0o777 == 0o600


def test_ctrl_c_keeps_the_quote_and_resuming_reuses_its_request(
    tmp_path, cli_environment
):
    endpoint, backend_path, cli, start = cli_environment
    preimage = os.urandom(32)
    payment_hash = hashlib.sha256(preimage).hexdigest()
    minting = start("alice", "mint", "1000", payment_hash, "--preimage", preimage.hex())
    quote = read_quote(minting)
    wallet = Wallet(tmp_path / "alice.sqlite3")
    saved = wallet.pending_mint(quote["quote"])
    assert saved is not None
    assert saved.preimage == preimage.hex()
    minting.send_signal(signal.SIGINT)
    output, errors = minting.communicate(timeout=10)
    assert minting.returncode == 130
    assert "Traceback" not in errors
    assert f"mint --quote {quote['quote']}" in errors
    assert wallet.pending_mint(quote["quote"]) == saved
    assert wallet.tokens() == []
    assert (
        httpx.get(endpoint + f"/v1/mint/quote/bolt11/{quote['quote']}").json()["state"]
        == "OPEN"
    )
    resuming = start("alice", "mint", "--quote", quote["quote"])
    assert read_quote(resuming)["quote"] == quote["quote"]
    assert resuming.poll() is None
    assert wallet.pending_mint(quote["quote"]) == saved
    cli("alice", "dev-accept", payment_hash, "--backend-db", backend_path)
    output, errors = resuming.communicate(timeout=20)
    assert resuming.returncode == 0, errors
    token = Token.model_validate_json(output)
    assert token.preimage == preimage.hex()
    assert Credential.from_bytes(bytes.fromhex(token.credential)).s == int(
        saved.secret, 16
    )
    assert wallet.pending() == []
    assert len(wallet.tokens()) == 1


@pytest.mark.parametrize("action", ["send", "pay"])
def test_existing_token_can_attach_its_preimage_for_send_or_pay(
    cli_environment, action
):
    _, backend_path, cli, start = cli_environment
    preimage = os.urandom(32).hex()
    payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    minting = start("alice", "mint", "1000", payment_hash)
    read_quote(minting)
    cli("alice", "dev-accept", payment_hash, "--backend-db", backend_path)
    output, errors = minting.communicate(timeout=20)
    assert minting.returncode == 0, errors
    assert Token.model_validate_json(output).preimage is None
    payout = json.loads(
        cli("alice", "dev-invoice", "1000", "--backend-db", backend_path)
    )
    if action == "send":
        assert "no HTLC preimage" in cli("alice", "send", "1000", status=1)
        encoded = cli("alice", "send", "1000", "--preimage", preimage).strip()
        assert decode_token(encoded).preimage == preimage
        received = json.loads(cli("bob", "receive", encoded, explicit_mint=False))
        assert received["preimage"] == preimage
        receipt = json.loads(cli("bob", "pay", payout["request"], explicit_mint=False))
    else:
        assert "no HTLC preimage" in cli("alice", "pay", payout["request"], status=1)
        receipt = json.loads(
            cli("alice", "pay", payout["request"], "--preimage", preimage)
        )
    assert receipt["payment_hash"] == payment_hash
    assert receipt["preimage"] == preimage
    assert receipt["payout_hash"] == payout["payment_hash"]
    assert receipt["state"] == "SETTLED"
