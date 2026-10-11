"""Real Breez/Spark holds and melts against a funded cashu-regtest --spark.

Uses new random regtest wallet keys and temporary databases. Never connects
to default/public Spark operators. Configuration follows Nutshell tests/spark.py.
"""

import asyncio
import hashlib
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import breez_sdk_spark as breez
import httpx
import uvicorn
from mnemonic import Mnemonic

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cashu.api import create_app  # noqa: E402
from cashu.client import Client  # noqa: E402
from cashu.mint import Mint  # noqa: E402
from cashu.models import InvoiceState  # noqa: E402
from cashu.sparkl2 import SparkBackend, SparkConnection  # noqa: E402

REGTEST = Path(os.getenv("CASHU_REGTEST_DIR", str(Path.home() / "cashu-regtest")))
PROJECT = os.getenv("CASHU_REGTEST_PROJECT", "cashu")
SSP = "http://127.0.0.1:5000"
ESPLORA = "http://127.0.0.1:30000"


async def compose(service, *args):
    process = await asyncio.create_subprocess_exec(
        "docker",
        "compose",
        "-p",
        PROJECT,
        "-f",
        str(REGTEST / "docker-compose.yml"),
        "exec",
        "-T",
        service,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 60)
    except BaseException as exc:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        if isinstance(exc, TimeoutError):
            raise RuntimeError(
                f"regtest command timed out in {service}; inspect the local SSP logs"
            ) from None
        raise
    if process.returncode:
        raise RuntimeError(f"regtest command failed in {service}")
    return stdout.decode()


async def lightning(*args):
    return json.loads(
        await compose(
            "clightning-2",
            "lightning-cli",
            "--network=regtest",
            "-N",
            "none",
            "--json",
            *args,
        )
    )


async def wait_until(label, check):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        result = await check()
        if result:
            return result
        await asyncio.sleep(1)
    raise RuntimeError(f"timed out waiting for {label}")


async def connection():
    async with httpx.AsyncClient(timeout=5, trust_env=False) as http:
        identity = await http.get(SSP + "/identity")
        identity.raise_for_status()
        tip = await http.get(ESPLORA + "/blocks/tip/height")
        tip.raise_for_status()
        if int(tip.text) <= 0:
            raise RuntimeError("Spark regtest chain is not funded")
    info = await lightning("getinfo")
    if info.get("warning_bitcoind_sync") or info.get("warning_lightningd_sync"):
        raise RuntimeError("regtest Lightning payer is not synchronized")
    operators = json.loads((REGTEST / "spark/operators.json").read_text())
    return SparkConnection(
        coordinator_identifier=f"{1:064x}",
        threshold=2,
        signing_operators=[
            {
                "id": operator["id"],
                "identifier": f"{operator['id'] + 1:064x}",
                "address": f"https://localhost:{8535 + operator['id']}",
                "identity_public_key": operator["identity_public_key"],
                "ca_cert_pem": await compose(
                    f"spark-operator-{operator['id']}", "cat", operator["cert_path"]
                ),
            }
            for operator in operators
        ],
        ssp_url=SSP,
        ssp_identity_public_key=identity.json()["identityPublicKey"],
        esplora_url=ESPLORA,
    )


async def main():
    spec = await connection()  # All preflight checks precede creating invoices.
    with tempfile.TemporaryDirectory(prefix="cashu-spark-v3-") as directory:
        path = Path(directory)
        backend = SparkBackend(
            path / "spark.sqlite3",
            Mnemonic("english").generate(),
            network="regtest",
            connection=spec,
        )
        mint = Mint(path / "mint.sqlite3", os.urandom(32), backend)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(create_app(mint), log_level="error"))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        funding = []
        try:

            async def started():
                return server.started

            await wait_until("mint server", started)
            sdk = await backend._ensure_sdk()
            # The mint supplies outgoing liquidity from its own Spark wallet.
            liquidity = await sdk.receive_payment(
                breez.ReceivePaymentRequest(
                    payment_method=breez.ReceivePaymentMethod.BOLT11_INVOICE(
                        description="regtest mint liquidity",
                        amount_sats=20_000,
                        expiry_secs=3600,
                        payment_hash=None,
                    ),
                )
            )
            await lightning("pay", liquidity.payment_request)

            async def liquid():
                info = await sdk.get_info(breez.GetInfoRequest(ensure_synced=True))
                return info.balance_sats >= 20_000

            await wait_until("mint payout liquidity", liquid)

            with httpx.Client(base_url=url, timeout=60, trust_env=False) as http:
                client = Client(url, http)

                async def fund(amount):
                    preimage = os.urandom(32)
                    payment_hash = hashlib.sha256(preimage).hexdigest()
                    quote = await asyncio.to_thread(client.quote, amount, payment_hash)
                    funding.append(asyncio.create_task(lightning("pay", quote.request)))

                    async def accepted():
                        invoice = await backend.get_invoice(payment_hash)
                        return invoice.state == InvoiceState.accepted

                    await wait_until("funded hold without preimage", accepted)
                    operation = await asyncio.to_thread(
                        client.prepare_mint, quote.quote, preimage.hex()
                    )
                    token = await asyncio.to_thread(client.finish_mint, operation)
                    return token, preimage

                token, preimage = await fund(1000)
                operation = await asyncio.to_thread(client.prepare_swap, token)
                token = await asyncio.to_thread(client.finish_swap, operation)
                payout = await lightning(
                    "invoice", "9000", str(uuid4()), "partial melt"
                )
                operation = await asyncio.to_thread(
                    client.prepare_melt, token, payout["bolt11"], fee_reserve=10
                )

                async def paid():
                    try:
                        return await asyncio.to_thread(client.finish_melt, operation)
                    except Exception:
                        return None

                receipt = await wait_until("partial payout", paid)
                change = await asyncio.to_thread(client.melt_change, operation, receipt)
                if change.amount != 1000 - 9 - receipt.fee:
                    raise RuntimeError("partial melt change does not conserve value")
                if (
                    await backend.get_invoice(token.proofs[0].payment_hash)
                ).state != InvoiceState.accepted:
                    raise RuntimeError("partial melt settled the original hold early")
                print(
                    "PASS: mint, private swap, partial melt and fee change; original hold stays pending",
                    flush=True,
                )
                await backend.settle_invoice(preimage)

                async def closed():
                    return (
                        await backend.get_invoice(token.proofs[0].payment_hash)
                    ).state == InvoiceState.settled

                await wait_until("explicit delayed claim", closed)
                snapshot = await asyncio.to_thread(client.pending_htlcs)
                if snapshot.htlcs:
                    raise RuntimeError("settled backing still has live tokens")
                print(
                    "PASS: explicit settlement expires remaining denominations",
                    flush=True,
                )

                payout = await lightning("invoice", "15000", str(uuid4()), "full melt")
                prepared = await sdk.prepare_send_payment(
                    breez.PrepareSendPaymentRequest(
                        payment_request=breez.PaymentRequest.INPUT(payout["bolt11"]),
                    )
                )
                fee = prepared.payment_method.lightning_fee_sats
                token, _ = await fund(15 + fee)
                operation = await asyncio.to_thread(
                    client.prepare_melt, token, payout["bolt11"], fee_reserve=fee
                )
                receipt = await wait_until("full payout", paid)

                async def exhausted():
                    return (
                        await backend.get_invoice(token.proofs[0].payment_hash)
                    ).state == InvoiceState.settled

                await wait_until("full-value settlement", exhausted)
                if receipt.change:
                    raise RuntimeError("fully redeemed hold returned unexpected change")
                await asyncio.wait_for(asyncio.gather(*funding), 60)
                print(
                    "PASS: full melt including fee settles backing and releases the original payer",
                    flush=True,
                )
        finally:
            for task in funding:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*funding, return_exceptions=True)
            server.should_exit = True
            await serving


if __name__ == "__main__":
    asyncio.run(main())
