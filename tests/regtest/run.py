"""Test whole HTLC claims against an already running cashu-regtest network."""

import argparse
import hashlib
import json
import os
import ssl
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from cashu.api import create_app
from cashu.client import Client, ClientError
from cashu.db import BackendError
from cashu.lightning import LndRestBackend, invoice_details
from cashu.mint import Mint
from cashu.models import InvoiceState, htlc_claim_scalar

AMOUNT = 12345
SETUP_HINT = (
    "Set up and start cashu-regtest with funded channels before running make regtest."
)


class RegtestError(RuntimeError):
    pass


def execute(command: list[str], label: str) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RegtestError(f"Cannot access {label}. {SETUP_HINT}") from exc
    if result.returncode:
        raise RegtestError(f"{label} is missing or unavailable. {SETUP_HINT}")
    return result.stdout.strip()


@dataclass(frozen=True)
class Node:
    container: str
    rpcserver: str

    @classmethod
    def connect(cls, container: str) -> "Node":
        state = execute(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Running}} {{.Config.Hostname}}",
                container,
            ],
            f"cashu-regtest container {container}",
        )
        running, _, hostname = state.partition(" ")
        if running != "true" or not hostname:
            raise RegtestError(
                f"cashu-regtest container {container} is not running. {SETUP_HINT}"
            )
        return cls(container, f"{hostname}:10009")

    def command(self, *args: str) -> list[str]:
        return [
            "docker",
            "exec",
            self.container,
            "lncli",
            "--network=regtest",
            f"--rpcserver={self.rpcserver}",
            *args,
        ]

    def ln(self, *args: str) -> dict:
        result = json.loads(execute(self.command(*args), f"LND node {self.container}"))
        if not isinstance(result, dict):
            raise RegtestError(f"Invalid LND response from {self.container}")
        return result

    def copy(self, remote: str, local: Path) -> None:
        execute(
            ["docker", "cp", f"{self.container}:{remote}", str(local)],
            f"LND credentials in {self.container}",
        )


def verify_network(payer: Node, mint: Node, receiver: Node) -> str:
    identities = []
    for node in (payer, mint, receiver):
        info = node.ln("getinfo")
        if not any(
            chain.get("chain") == "bitcoin" and chain.get("network") == "regtest"
            for chain in info.get("chains", [])
        ):
            raise RegtestError(f"LND node {node.container} must use Bitcoin regtest.")
        if not info.get("synced_to_chain"):
            raise RegtestError(f"LND node {node.container} is not synced. {SETUP_HINT}")
        identities.append(info["identity_pubkey"])
        channels = [
            c for c in node.ln("listchannels").get("channels", []) if c.get("active")
        ]
        balance = "remote_balance" if node == receiver else "local_balance"
        required = AMOUNT if node == receiver else AMOUNT + 10
        if sum(int(c.get(balance, 0)) for c in channels) < required:
            direction = "inbound" if node == receiver else "outbound"
            raise RegtestError(
                f"LND node {node.container} needs active channels with {direction} liquidity. {SETUP_HINT}"
            )
    if len(set(identities)) != 3:
        raise RegtestError(
            "Payer, mint, and receiver must be three different LND nodes."
        )
    return identities[1]


def verify_rest(endpoint: str, macaroon: Path, cert: Path, identity: str) -> None:
    try:
        tls = ssl.create_default_context(cafile=str(cert))
        with httpx.Client(
            base_url=endpoint,
            headers={"Grpc-Metadata-macaroon": macaroon.read_bytes().hex()},
            verify=tls,
            timeout=10,
        ) as client:
            response = client.get("/v1/getinfo")
            response.raise_for_status()
            info = response.json()
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise RegtestError(
            f"LND REST API at {endpoint} is not ready; check its endpoint, TLS certificate, and macaroon. {SETUP_HINT}"
        ) from exc
    if not isinstance(info, dict) or info.get("identity_pubkey") != identity:
        raise RegtestError(
            "LND REST endpoint does not belong to the configured mint node."
        )


def wait_for(predicate, label: str, timeout: int = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (ClientError, httpx.HTTPError):
            pass
        time.sleep(1)
    raise RegtestError(
        f"timed out waiting for {label}; check cashu-regtest routing and liquidity"
    )


def exercise(
    payer_node: Node,
    mint_node: Node,
    receiver_node: Node,
    endpoint: str,
    identity: str,
    hold_expiry_delta: int = 18,
) -> None:
    with tempfile.TemporaryDirectory(prefix="cashu-lnd-") as directory:
        path = Path(directory)
        mint_node.copy("/root/.lnd/tls.cert", path / "tls.cert")
        mint_node.copy(
            "/root/.lnd/data/chain/bitcoin/regtest/admin.macaroon",
            path / "admin.macaroon",
        )
        verify_rest(endpoint, path / "admin.macaroon", path / "tls.cert", identity)
        backend = LndRestBackend(
            endpoint,
            path / "admin.macaroon",
            path / "tls.cert",
            hold_expiry_delta=hold_expiry_delta,
        )
        mint = Mint(path / "mint.sqlite3", os.urandom(32), backend)
        preimage = os.urandom(32)
        payment_hash = hashlib.sha256(preimage).hexdigest()
        amount = AMOUNT
        with TestClient(create_app(mint, recovery=False)) as http:
            client = Client("http://testserver", http)
            quote = client.quote(amount, payment_hash)
            assert quote.state == InvoiceState.open
            # This payer process remains blocked until the final burn settles it.
            payer = subprocess.Popen(
                payer_node.command("payinvoice", "--force", quote.request),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:

                def hold_accepted() -> bool:
                    if payer.poll() is not None:
                        raise RegtestError(
                            "Payer payment ended before the hold was accepted; check cashu-regtest routing and liquidity."
                        )
                    return client.get_quote(quote.quote).state == InvoiceState.accepted

                wait_for(hold_accepted, "accepted original hold")
                assert payer.poll() is None
                token = client.finish_mint(
                    client.prepare_mint(quote.quote, preimage.hex())
                )
                claim = htlc_claim_scalar(payment_hash, amount)
                keyset_id = client.credential(token).keyset_id
                assert client.credential(token).h == claim
                backing = client.check_backing(token)
                assert backing.htlc_expiry_height is not None
                assert (
                    backing.expiry_height
                    == backing.htlc_expiry_height - hold_expiry_delta
                )
                assert backing.blocks_remaining > 0
                for _ in range(2):
                    token = client.finish_swap(client.prepare_swap(token))
                    assert token.amount == amount
                    assert token.payment_hash == payment_hash
                    assert token.preimage == preimage.hex()
                    assert client.credential(token).h == claim
                    assert client.credential(token).keyset_id == keyset_id
                    assert client.get_quote(quote.quote).state == InvoiceState.accepted
                    assert payer.poll() is None
                    assert client.check_backing(token).payment_hash == payment_hash
                print(
                    f"Minted one {amount}-sat HTLC claim and privately swapped twice while the Lightning payer remains pending",
                    flush=True,
                )
                receiver = receiver_node.ln("addinvoice", "--amt", str(amount))
                payout_hash, _ = invoice_details(receiver["payment_request"])
                assert payout_hash != payment_hash
                operation = client.prepare_burn(token, receiver["payment_request"])
                receipt = client.finish_burn(operation)
                assert receipt.preimage == preimage.hex()
                assert receipt.payout_hash == payout_hash
                assert client.get_quote(quote.quote).state == InvoiceState.settled
                assert client.pending_htlcs().htlcs == []
                assert client.check([client.nullifier(token)]).states == ["SPENT"]
                assert client.finish_burn(operation) == receipt
                stdout, stderr = payer.communicate(timeout=30)
                assert payer.returncode == 0, stderr
                # lncli emits status updates followed by a JSON payment result.
                assert "SUCCEEDED" in stdout
                receiver_state = receiver_node.ln("lookupinvoice", payout_hash)
                assert receiver_state["state"] == "SETTLED"
                print(
                    "Unrelated receiver invoice paid; token's preimage settled the original hold; replay returned the same receipt",
                    flush=True,
                )
            finally:
                if payer.poll() is None:
                    payer.terminate()
                    payer.communicate(timeout=10)
                    # Release only this test's unresolved hold on failure.
                    try:
                        mint_node.ln("cancelinvoice", payment_hash)
                    except RegtestError:
                        pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test an existing, funded cashu-regtest network; no node provisioning."
    )
    parser.add_argument("--payer-container", default="cashu-lnd-1-1")
    parser.add_argument("--mint-container", default="cashu-lnd-3-1")
    parser.add_argument("--receiver-container", default="cashu-lnd-2-1")
    parser.add_argument("--lnd-endpoint", default="https://localhost:8081")
    parser.add_argument("--lnd-hold-expiry-delta", type=int, default=18)
    args = parser.parse_args()
    if not args.lnd_endpoint.startswith("https://"):
        parser.error("LND REST endpoint must use https")
    if args.lnd_hold_expiry_delta < 0:
        parser.error("LND hold expiry delta must not be negative")
    try:
        payer = Node.connect(args.payer_container)
        mint = Node.connect(args.mint_container)
        receiver = Node.connect(args.receiver_container)
        identity = verify_network(payer, mint, receiver)
        print("Existing cashu-regtest LND nodes are ready", flush=True)
        exercise(
            payer,
            mint,
            receiver,
            args.lnd_endpoint,
            identity,
            args.lnd_hold_expiry_delta,
        )
    except (
        RegtestError,
        ClientError,
        BackendError,
        OSError,
        ValueError,
        subprocess.TimeoutExpired,
        AssertionError,
    ) as exc:
        print(
            f"Regtest failed: {str(exc) or 'HTLC integration assertion failed'}",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
