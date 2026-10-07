import argparse
import asyncio
import json
import os
import shlex
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .client import Client, ClientError
from .lightning import FakeBackend, invoice_details
from .models import InvoiceState, QuoteResponse, Token, preimage_hash
from .token import decode_token
from .wallet import Wallet


def wait_and_mint(
    client: Client, wallet: Wallet, quote: QuoteResponse, preimage: str | None = None
) -> Token:
    """Keep the exact issuance request across interrupted waits and responses."""
    resume = shlex.join(
        [
            "cashu",
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
        description="Cashu wallet for whole Lightning HTLC claims"
    )
    parser.add_argument("--mint", help="mint URL (default: http://127.0.0.1:3338)")
    parser.add_argument("--wallet", type=Path, default=Path("data/wallet.sqlite3"))
    sub = parser.add_subparsers(dest="command", required=True)
    quote = sub.add_parser("quote")
    quote.add_argument("amount", type=int)
    quote.add_argument("payment_hash")
    quote.add_argument("--expiry", type=int, default=3600)
    mint = sub.add_parser(
        "mint", help="create a HODL invoice and wait to mint its funded claim"
    )
    mint.add_argument("amount", nargs="?", help="whole HTLC amount in sats")
    mint.add_argument("payment_hash", nargs="?")
    mint.add_argument(
        "--quote", help="resume an existing quote and wait for its funding"
    )
    mint.add_argument("--expiry", type=int, default=3600)
    mint.add_argument(
        "--preimage", help="save the original HTLC preimage locally with the token"
    )
    sub.add_parser("list")
    sub.add_parser("htlcs", help="show the mint's public pending HTLCs and deadlines")
    balance = sub.add_parser("balance", help="show the available balance")
    balance.add_argument("--verbose", "-v", action="store_true")
    send = sub.add_parser("send", help="send one whole HTLC claim as a token string")
    send.add_argument("amount", type=int)
    send.add_argument("--token-id", help="select a token from cashu list")
    send.add_argument(
        "--preimage", help="attach the original HTLC preimage to an existing token"
    )
    receive = sub.add_parser("receive", help="privately swap a received token string")
    receive.add_argument("token")
    decode = sub.add_parser("decode", help="decode a token string without spending it")
    decode.add_argument("token")
    pay = sub.add_parser(
        "pay", help="burn one whole token to pay an invoice of the same amount"
    )
    pay.add_argument("invoice")
    pay.add_argument("--token-id")
    pay.add_argument(
        "--preimage", help="original HTLC preimage if missing from the token"
    )
    burn = sub.add_parser("burn")
    burn.add_argument("token_id")
    burn.add_argument("invoice")
    burn.add_argument(
        "--preimage", help="original HTLC preimage if missing from the token"
    )
    pending = sub.add_parser("pending")
    pending.add_argument("--sent", action="store_true", help="list sent bearer tokens")
    retry = sub.add_parser("retry")
    retry.add_argument("operation_id")
    abandon = sub.add_parser("abandon")
    abandon.add_argument("operation_id")
    accept = sub.add_parser("dev-accept", help="accept fake HTLC funding locally")
    accept.add_argument("payment_hash")
    accept.add_argument(
        "--backend-db", type=Path, default=Path("data/mint/fake.sqlite3")
    )
    receiver = sub.add_parser(
        "dev-invoice", help="create a fake receiver invoice locally"
    )
    receiver.add_argument("amount", type=int)
    receiver.add_argument(
        "--backend-db", type=Path, default=Path("data/mint/fake.sqlite3")
    )
    args = parser.parse_args()
    if args.command == "mint":
        if args.quote and (args.amount is not None or args.payment_hash is not None):
            parser.error("use mint <amount> <payment_hash> or mint --quote <quote_id>")
        if not args.quote:
            if args.amount is not None and args.payment_hash is None:
                # Preserve the earlier mint <quote_id> form.
                if len(args.amount) == 64:
                    args.quote = args.amount
                else:
                    parser.error(
                        "mint needs <amount> <payment_hash> or --quote <quote_id>"
                    )
            elif args.amount is None or args.payment_hash is None:
                parser.error("mint needs <amount> <payment_hash> or --quote <quote_id>")
    if args.command == "dev-accept":
        asyncio.run(FakeBackend(args.backend_db).accept(args.payment_hash))
        print("ACCEPTED")
        return
    if args.command == "dev-invoice":
        backend = FakeBackend(args.backend_db)
        invoice = backend.receiver_invoice(args.amount, os.urandom(32))
        payment_hash, amount = invoice_details(invoice)
        print(
            json.dumps(
                {"request": invoice, "payment_hash": payment_hash, "amount": amount}
            )
        )
        return
    client = None
    try:
        if args.command == "decode":
            print(decode_token(args.token).model_dump_json(indent=2))
            return
        wallet = Wallet(args.wallet)
        incoming = decode_token(args.token) if args.command == "receive" else None
        payment_token = None
        if args.command == "pay":
            _, amount = invoice_details(args.invoice)
            if amount is None:
                raise ValueError("receiver invoice must specify the whole HTLC amount")
            payment_token = wallet.select_token(
                amount, token_id=args.token_id, mint=args.mint
            )
        selected_token = incoming or payment_token
        token_mint = selected_token.mint if selected_token is not None else None
        client = Client(args.mint or token_mint or "http://127.0.0.1:3338")
        if args.command == "quote":
            print(
                client.quote(
                    args.amount, args.payment_hash, args.expiry
                ).model_dump_json(indent=2)
            )
        elif args.command == "list":
            print(json.dumps(wallet.tokens(), indent=2))
        elif args.command == "htlcs":
            print(client.pending_htlcs().model_dump_json(indent=2))
        elif args.command == "balance":
            tokens = wallet.tokens()
            if args.verbose:
                print(
                    json.dumps(
                        {
                            state: sum(
                                t["amount"] for t in tokens if t["state"] == state
                            )
                            for state in ("LIVE", "SENT", "PENDING")
                        },
                        indent=2,
                    )
                )
            else:
                print(
                    f"Balance: {sum(t['amount'] for t in tokens if t['state'] == 'LIVE')}"
                )
        elif args.command == "pending":
            print(
                json.dumps(
                    [t for t in wallet.tokens() if t["state"] == "SENT"]
                    if args.sent
                    else wallet.pending(),
                    indent=2,
                )
            )
        elif args.command == "send":
            print(wallet.send(args.amount, args.token_id, args.mint, args.preimage))
        elif args.command == "mint":
            if args.quote:
                quote_response = client.get_quote(args.quote)
            else:
                if (
                    args.preimage is not None
                    and preimage_hash(args.preimage) != args.payment_hash
                ):
                    raise ValueError(
                        "HTLC preimage does not match the requested payment hash"
                    )
                quote_response = client.quote(
                    int(args.amount), args.payment_hash, args.expiry
                )
            token = wait_and_mint(client, wallet, quote_response, args.preimage)
            print(token.model_dump_json(indent=2))
        elif args.command == "receive":
            assert incoming is not None
            swap_operation = client.prepare_swap(incoming)
            backing = swap_operation.backing
            assert backing is not None
            if backing.expiry_height is not None:
                deadline = (
                    f"block {backing.expiry_height} "
                    f"({backing.blocks_remaining} blocks remaining; "
                    f"HTLC CLTV {backing.htlc_expiry_height})"
                )
            else:
                assert backing.expires_at is not None
                deadline = datetime.fromtimestamp(
                    backing.expires_at, timezone.utc
                ).isoformat()
            print(f"Backing HTLC is pending; expires at {deadline}", file=sys.stderr)
            wallet.save(swap_operation)
            print(f"Saved swap operation {swap_operation.id}", file=sys.stderr)
            received = wallet.retry(client, swap_operation.id, "swap")
            print(received.model_dump_json(indent=2))
        elif args.command in ("pay", "burn"):
            if payment_token is None:
                payment_token = wallet.token(args.token_id)
            burn_operation = client.prepare_burn(
                payment_token, args.invoice, args.preimage
            )
            wallet.save(burn_operation)
            print(f"Saved burn operation {burn_operation.id}", file=sys.stderr)
            print(
                wallet.retry(client, burn_operation.id, "burn").model_dump_json(
                    indent=2
                )
            )
        elif args.command == "retry":
            matches = [p for p in wallet.pending() if p["id"] == args.operation_id]
            if not matches:
                raise ValueError("unknown pending operation")
            print(
                wallet.retry(
                    client, args.operation_id, matches[0]["kind"]
                ).model_dump_json(indent=2)
            )
        elif args.command == "abandon":
            wallet.abandon(client, args.operation_id)
            print("Released the unspent token")
    except KeyboardInterrupt:
        print(
            "\nStopped. Use pending and retry to recover any saved operation.",
            file=sys.stderr,
        )
        sys.exit(130)
    except httpx.HTTPError:
        print(
            "Mint connection interrupted; use pending and retry to recover the saved operation",
            file=sys.stderr,
        )
        sys.exit(1)
    except (ClientError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()
