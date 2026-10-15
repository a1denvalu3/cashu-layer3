import argparse
import asyncio
import json
import os
import shlex
import sys
import time
from pathlib import Path

import httpx

from .client import Client, ClientError, SwapOperation
from .lightning import FakeBackend
from .models import InvoiceState, QuoteResponse, Token, TokenProof, preimage_hash
from .token import decode_token
from .wallet import Wallet


def wait_and_mint(
    client: Client, wallet: Wallet, quote: QuoteResponse, preimage: str | None = None
) -> Token:
    """Keep the exact issuance request across interrupted waits and responses."""
    resume = shlex.join(
        [
            "tmp.cash",
            "--mint",
            client.mint_url,
            "--wallet",
            str(wallet.db.path),
            "mint",
            "--quote",
            quote.quote,
        ]
    )
    try:
        operation = wallet.pending_mint(quote.quote)
        if operation is None:
            if quote.issued:
                raise ClientError(
                    "quote already issued; check your wallet or recover its saved operation"
                )
            operation = client.prepare_mint(quote.quote, preimage=preimage)
            wallet.save(operation)
        elif preimage is not None:
            operation = wallet.set_mint_preimage(operation.id, preimage)
        print(quote.model_dump_json(indent=2), flush=True)
        print(f"Saved mint operation {operation.id}", file=sys.stderr, flush=True)
        print(
            "Waiting for HODL payment acceptance. Ctrl+C stops the wait.",
            file=sys.stderr,
            flush=True,
        )
        disconnected = False
        while True:
            try:
                current = client.get_quote(quote.quote)
            except httpx.TransportError:
                if not disconnected:
                    print(
                        "Mint connection interrupted; waiting to reconnect.",
                        file=sys.stderr,
                        flush=True,
                    )
                    disconnected = True
                time.sleep(1)
                continue
            disconnected = False
            # A saved request can recover its exact response even after settlement.
            if current.issued or current.state == InvoiceState.accepted:
                token = wallet.retry(client, operation.id, "mint")
                assert isinstance(token, Token)
                print(
                    f"Minted {token.amount} sats and saved the token.",
                    file=sys.stderr,
                    flush=True,
                )
                return token
            if current.state != InvoiceState.open:
                raise ClientError(
                    f"HODL invoice is {current.state.value}; cannot mint this quote"
                )
            if current.expiry <= time.time():
                raise ClientError("HODL invoice expired before payment acceptance")
            time.sleep(1)
    except KeyboardInterrupt:
        print(
            f"\nStopped. Resume this quote with:\n{resume}", file=sys.stderr, flush=True
        )
        raise SystemExit(130) from None


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tmp.cash",
        description="tmp.cash wallet for denominated, expiring Lightning HTLC tokens",
    )
    parser.add_argument("--mint", help="mint URL (default: http://127.0.0.1:3338)")
    parser.add_argument("--wallet", type=Path, default=Path("data/wallet-v3.sqlite3"))
    sub = parser.add_subparsers(dest="command", required=True)
    quote = sub.add_parser("quote")
    quote.add_argument("amount", type=int)
    quote.add_argument("payment_hash")
    quote.add_argument("--expiry", type=int, default=3600)
    mint = sub.add_parser("mint")
    mint.add_argument("amount", nargs="?", type=int)
    mint.add_argument("payment_hash", nargs="?")
    mint.add_argument("--quote")
    mint.add_argument("--expiry", type=int, default=3600)
    mint.add_argument("--preimage")
    sub.add_parser("list")
    sub.add_parser("htlcs")
    balance = sub.add_parser("balance")
    balance.add_argument("--verbose", "-v", action="store_true")
    balance.add_argument(
        "--refresh",
        action="store_true",
        help="refresh HTLC backing and mark expired tokens",
    )
    send = sub.add_parser("send")
    send.add_argument("amount", type=int)
    send.add_argument("--payment-hash")
    send.add_argument("--token-id", action="append")
    send.add_argument("--preimage")
    for name in ("receive", "decode"):
        sub.add_parser(name).add_argument("token")
    for name in ("pay", "melt"):
        pay = sub.add_parser(name)
        pay.add_argument("invoice")
        pay.add_argument("--fee-reserve", type=int)
        pay.add_argument("--token-id", action="append")
        pay.add_argument("--payment-hash")
        pay.add_argument("--preimage")
    pending = sub.add_parser("pending")
    pending.add_argument("--sent", action="store_true")
    sub.add_parser("retry").add_argument("operation_id")
    sub.add_parser("abandon").add_argument("operation_id")
    accept = sub.add_parser("dev-accept")
    accept.add_argument("payment_hash")
    accept.add_argument(
        "--backend-db", type=Path, default=Path("data/mint-v3/fake.sqlite3")
    )
    receiver = sub.add_parser("dev-invoice")
    receiver.add_argument("amount", type=int)
    receiver.add_argument(
        "--backend-db", type=Path, default=Path("data/mint-v3/fake.sqlite3")
    )
    args = parser.parse_args()
    client = None
    try:
        if args.command == "dev-accept":
            asyncio.run(FakeBackend(args.backend_db).accept(args.payment_hash))
            print("ACCEPTED")
            return
        if args.command == "dev-invoice":
            print(
                FakeBackend(args.backend_db).receiver_invoice(
                    args.amount, os.urandom(32)
                )
            )
            return
        if args.command == "decode":
            print(decode_token(args.token).model_dump_json(indent=2))
            return
        incoming = decode_token(args.token) if args.command == "receive" else None
        client = Client(
            args.mint
            or (incoming.mint if incoming is not None else "http://127.0.0.1:3338")
        )
        wallet = Wallet(args.wallet)
        if args.command == "quote":
            print(
                client.quote(
                    args.amount, args.payment_hash, args.expiry
                ).model_dump_json(indent=2)
            )
        elif args.command == "mint":
            if args.quote:
                current = client.get_quote(args.quote)
            else:
                if args.amount is None or args.payment_hash is None:
                    parser.error(
                        "mint needs <amount> <payment_hash> or --quote <quote_id>"
                    )
                if (
                    args.preimage is not None
                    and preimage_hash(args.preimage) != args.payment_hash
                ):
                    raise ValueError(
                        "HTLC preimage does not match requested payment hash"
                    )
                current = client.quote(args.amount, args.payment_hash, args.expiry)
            print(
                wait_and_mint(client, wallet, current, args.preimage).model_dump_json(
                    indent=2
                )
            )
        elif args.command == "list":
            print(json.dumps(wallet.tokens(), indent=2))
        elif args.command == "htlcs":
            print(client.pending_htlcs().model_dump_json(indent=2))
        elif args.command == "balance":
            if args.refresh:
                wallet.refresh(client)
            rows = [
                r for r in wallet.tokens() if r["mint"].rstrip("/") == client.mint_url
            ]
            if args.verbose:
                print(
                    json.dumps(
                        {
                            state: sum(r["amount"] for r in rows if r["state"] == state)
                            for state in ("LIVE", "SENT", "PENDING", "EXPIRED")
                        },
                        indent=2,
                    )
                )
            else:
                print(
                    f"Balance: {sum(r['amount'] for r in rows if r['state'] == 'LIVE')}"
                )
        elif args.command == "pending":
            print(
                json.dumps(
                    [r for r in wallet.tokens() if r["state"] == "SENT"]
                    if args.sent
                    else wallet.pending(),
                    indent=2,
                )
            )
        elif args.command == "send":
            print(
                wallet.send(
                    client, args.amount, args.payment_hash, args.token_id, args.preimage
                )
            )
        elif args.command == "receive":
            assert incoming is not None
            # Separate proof-bearing swaps for each HTLC; never a mixed-h swap.
            groups: dict[str, list[TokenProof]] = {}
            for proof in incoming.proofs:
                groups.setdefault(proof.payment_hash, []).append(proof)
            operations: list[SwapOperation] = [
                client.prepare_swap(Token(mint=incoming.mint, proofs=proofs))
                for proofs in groups.values()
            ]
            wallet.save_many(list(operations))
            received_proofs = []
            for operation in operations:
                print(
                    f"Backing deadline: {operation.backing.model_dump_json()}",
                    file=sys.stderr,
                )
                print(
                    f"Saved swap operation {operation.id}", file=sys.stderr, flush=True
                )
                received = wallet.retry(client, operation.id)
                assert isinstance(received, Token)
                received_proofs.extend(received.proofs)
            print(
                Token(mint=client.mint_url, proofs=received_proofs).model_dump_json(
                    indent=2
                )
            )
        elif args.command in ("pay", "melt"):
            melt_quote = client.melt_quote(args.invoice, args.fee_reserve)
            wallet.refresh(client)
            token = wallet.select_token(
                melt_quote.amount + melt_quote.fee_reserve,
                client.mint_url,
                args.payment_hash,
                args.token_id,
            )
            if args.preimage is not None:
                token = token.with_preimage(args.preimage)
            melt_operation = client.prepare_melt(token, melt_quote)
            wallet.save(melt_operation)
            print(
                f"Saved melt operation {melt_operation.id}", file=sys.stderr, flush=True
            )
            print(wallet.retry(client, melt_operation.id).model_dump_json(indent=2))
        elif args.command == "retry":
            print(wallet.retry(client, args.operation_id).model_dump_json(indent=2))
        elif args.command == "abandon":
            wallet.abandon(client, args.operation_id)
            print("Released unspent inputs")
    except KeyboardInterrupt:
        print(
            "\nStopped. Use pending and retry to recover saved operations.",
            file=sys.stderr,
        )
        raise SystemExit(130) from None
    except httpx.HTTPError:
        print("Mint connection interrupted; use pending and retry.", file=sys.stderr)
        raise SystemExit(1) from None
    except (ClientError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()
