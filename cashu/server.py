import argparse
import os
import sys
from pathlib import Path

import uvicorn
from dotenv import dotenv_values

from .api import create_app
from .lightning import FakeBackend, HoldInvoiceBackend, LndRestBackend
from .mint import Mint


def load_seed(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        seed = path.read_bytes()
    else:
        seed = os.urandom(32)
        with os.fdopen(fd, "wb") as file:
            file.write(seed)
            file.flush()
            os.fsync(file.fileno())
    if len(seed) != 32:
        raise ValueError("mint seed file must contain exactly 32 bytes")
    return seed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    configuration = {**dotenv_values(Path.cwd() / ".env"), **os.environ}
    parser = argparse.ArgumentParser(
        description="PS ecash mint over Lightning hold invoices"
    )
    parser.add_argument(
        "--data", type=Path, default=configuration.get("CASHU_MINT_DATA") or "data/mint"
    )
    parser.add_argument(
        "--backend",
        choices=["fake", "lnd"],
        default=configuration.get("CASHU_MINT_BACKEND") or "fake",
    )
    parser.add_argument(
        "--host", default=configuration.get("CASHU_MINT_HOST") or "127.0.0.1"
    )
    parser.add_argument(
        "--port", type=int, default=configuration.get("CASHU_MINT_PORT") or "3338"
    )
    parser.add_argument(
        "--lnd-endpoint", default=configuration.get("CASHU_LND_ENDPOINT")
    )
    parser.add_argument(
        "--lnd-macaroon", type=Path, default=configuration.get("CASHU_LND_MACAROON")
    )
    parser.add_argument(
        "--lnd-cert", type=Path, default=configuration.get("CASHU_LND_CERT")
    )
    parser.add_argument(
        "--fee-limit-sat",
        type=int,
        default=configuration.get("CASHU_FEE_LIMIT_SAT") or "10",
    )
    parser.add_argument(
        "--lnd-hold-expiry-delta",
        type=int,
        default=configuration.get("CASHU_LND_HOLD_EXPIRY_DELTA") or "18",
        help="must match LND's invoices.holdexpirydelta (default: 18 blocks)",
    )
    args = parser.parse_args(argv)
    if args.backend not in {"fake", "lnd"}:
        parser.error("CASHU_MINT_BACKEND must be fake or lnd")
    if not 0 <= args.port <= 65535:
        parser.error("mint port must be between 0 and 65535")
    if args.fee_limit_sat < 0:
        parser.error("fee limit must not be negative")
    if args.lnd_hold_expiry_delta < 0:
        parser.error("LND hold expiry delta must not be negative")
    if args.backend == "lnd" and (not args.lnd_endpoint or not args.lnd_macaroon):
        parser.error(
            "LND needs --lnd-endpoint and --lnd-macaroon, or their .env settings"
        )
    return args


def main() -> None:
    args = parse_args()
    backend: HoldInvoiceBackend
    if args.backend == "lnd":
        backend = LndRestBackend(
            args.lnd_endpoint,
            args.lnd_macaroon,
            args.lnd_cert,
            args.fee_limit_sat,
            hold_expiry_delta=args.lnd_hold_expiry_delta,
        )
    else:
        backend = FakeBackend(args.data / "fake.sqlite3")
    seed = load_seed(args.data / "mint.seed")
    mint = Mint(args.data / "mint.sqlite3", seed, backend)
    print(f"Mint backend: {args.backend}", file=sys.stderr)
    uvicorn.run(create_app(mint), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
