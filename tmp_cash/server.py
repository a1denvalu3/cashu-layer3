import argparse
import os
import sys
from pathlib import Path

import uvicorn
from dotenv import dotenv_values

from .api import create_app
from .db import IncompatibleMintDatabase
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
    configuration: dict[str, str | None] = {}
    for source in (dotenv_values(Path.cwd() / ".env"), os.environ):
        # Preserve existing deployments; environment still takes priority over
        # .env, and the new prefix wins over its legacy alias in either source.
        configuration.update(
            {
                name.replace("CASHU_", "TMP_CASH_", 1): value
                for name, value in source.items()
                if name.startswith("CASHU_")
            }
        )
        configuration.update(source)
    parser = argparse.ArgumentParser(
        prog="tmp.cash-mint",
        description="tmp.cash PS ecash mint over Lightning hold invoices",
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=configuration.get("TMP_CASH_MINT_DATA") or "data/mint-v3",
    )
    parser.add_argument(
        "--backend",
        choices=["fake", "lnd", "spark"],
        default=configuration.get("TMP_CASH_MINT_BACKEND") or "fake",
    )
    parser.add_argument(
        "--host", default=configuration.get("TMP_CASH_MINT_HOST") or "127.0.0.1"
    )
    parser.add_argument(
        "--port", type=int, default=configuration.get("TMP_CASH_MINT_PORT") or "3338"
    )
    parser.set_defaults(admin_token=configuration.get("TMP_CASH_ADMIN_TOKEN") or None)
    parser.add_argument(
        "--lnd-endpoint", default=configuration.get("TMP_CASH_LND_ENDPOINT")
    )
    parser.add_argument(
        "--lnd-macaroon", type=Path, default=configuration.get("TMP_CASH_LND_MACAROON")
    )
    parser.add_argument(
        "--lnd-cert", type=Path, default=configuration.get("TMP_CASH_LND_CERT")
    )
    parser.add_argument(
        "--fee-limit-sat",
        type=int,
        default=configuration.get("TMP_CASH_FEE_LIMIT_SAT") or "10",
    )
    parser.add_argument(
        "--settle-margin-blocks",
        type=int,
        default=configuration.get("TMP_CASH_SETTLE_MARGIN_BLOCKS") or "6",
    )
    parser.add_argument(
        "--settle-margin-seconds",
        type=int,
        default=configuration.get("TMP_CASH_SETTLE_MARGIN_SECONDS") or "120",
    )
    parser.add_argument(
        "--lnd-hold-expiry-delta",
        type=int,
        default=configuration.get("TMP_CASH_LND_HOLD_EXPIRY_DELTA") or "18",
        help="must match LND's invoices.holdexpirydelta (default: 18 blocks)",
    )
    parser.add_argument(
        "--lnd-token-lifetime-blocks",
        type=int,
        default=configuration.get("TMP_CASH_LND_TOKEN_LIFETIME_BLOCKS") or "2016",
        help="requested redeemable lifetime for new LND holds (default: 2016 blocks, about two weeks)",
    )
    parser.add_argument(
        "--spark-mnemonic-file",
        type=Path,
        default=configuration.get("TMP_CASH_SPARK_MNEMONIC_FILE"),
        help="file containing the Spark wallet mnemonic",
    )
    parser.set_defaults(spark_mnemonic=configuration.get("TMP_CASH_SPARK_MNEMONIC"))
    parser.add_argument(
        "--spark-network",
        choices=["mainnet", "regtest"],
        default=configuration.get("TMP_CASH_SPARK_NETWORK") or "mainnet",
    )
    parser.set_defaults(spark_api_key=configuration.get("TMP_CASH_SPARK_API_KEY"))
    parser.add_argument(
        "--spark-config-file",
        type=Path,
        default=configuration.get("TMP_CASH_SPARK_CONFIG_FILE"),
        help="private regtest operator, SSP and Esplora connection JSON",
    )
    args = parser.parse_args(argv)
    if args.backend not in {"fake", "lnd", "spark"}:
        parser.error("TMP_CASH_MINT_BACKEND must be fake, lnd or spark")
    if not 0 <= args.port <= 65535:
        parser.error("mint port must be between 0 and 65535")
    if args.fee_limit_sat < 0:
        parser.error("fee limit must not be negative")
    if args.lnd_hold_expiry_delta < 0:
        parser.error("LND hold expiry delta must not be negative")
    if args.settle_margin_blocks < 1 or args.settle_margin_seconds < 1:
        parser.error("settlement margins must be positive")
    if args.lnd_token_lifetime_blocks < 1:
        parser.error("LND token lifetime must be positive")
    if (
        args.lnd_token_lifetime_blocks
        + args.lnd_hold_expiry_delta
        + args.settle_margin_blocks
        > 65535
    ):
        parser.error(
            "LND token lifetime plus cancellation and settlement margins must not exceed 65535 blocks"
        )
    if args.backend == "lnd" and (not args.lnd_endpoint or not args.lnd_macaroon):
        parser.error(
            "LND needs --lnd-endpoint and --lnd-macaroon, or their .env settings"
        )
    if args.backend == "spark":
        if not args.spark_mnemonic_file and not args.spark_mnemonic:
            parser.error("Spark needs --spark-mnemonic-file or TMP_CASH_SPARK_MNEMONIC")
        if args.spark_network not in {"mainnet", "regtest"}:
            parser.error("TMP_CASH_SPARK_NETWORK must be mainnet or regtest")
        if args.spark_network == "regtest" and not args.spark_config_file:
            parser.error("Spark regtest needs --spark-config-file")
        if args.spark_network != "regtest" and args.spark_config_file:
            parser.error("custom Spark endpoints require --spark-network regtest")
    return args


def create_backend(args: argparse.Namespace) -> HoldInvoiceBackend:
    if args.backend == "lnd":
        return LndRestBackend(
            args.lnd_endpoint,
            args.lnd_macaroon,
            args.lnd_cert,
            args.fee_limit_sat,
            hold_expiry_delta=args.lnd_hold_expiry_delta,
            cltv_expiry=(
                args.lnd_token_lifetime_blocks
                + args.lnd_hold_expiry_delta
                + args.settle_margin_blocks
            ),
        )
    if args.backend == "spark":
        # Keep the native SDK optional for fake/LND installations.
        try:
            from .sparkl2 import SparkBackend, load_connection
        except ImportError:
            raise SystemExit(
                "Spark needs its optional SDK: poetry install -E spark"
            ) from None
        mnemonic = (
            args.spark_mnemonic_file.read_text().strip()
            if args.spark_mnemonic_file
            else args.spark_mnemonic
        )
        connection = (
            load_connection(args.spark_config_file) if args.spark_config_file else None
        )
        return SparkBackend(
            args.data / "spark.sqlite3",
            mnemonic,
            args.spark_network,
            args.spark_api_key,
            args.fee_limit_sat,
            connection,
        )
    return FakeBackend(args.data / "fake.sqlite3", fee_limit_sat=args.fee_limit_sat)


def main() -> None:
    args = parse_args()
    backend = create_backend(args)
    seed = load_seed(args.data / "mint.seed")
    try:
        mint = Mint(
            args.data / "mint.sqlite3",
            seed,
            backend,
            args.settle_margin_blocks,
            args.settle_margin_seconds,
        )
    except IncompatibleMintDatabase as exc:
        raise SystemExit(
            f"Cannot open {args.data / 'mint.sqlite3'}: {exc}.\n"
            "Set TMP_CASH_MINT_DATA in .env to a new v3 directory, "
            "or pass --data <new-directory>.\n"
            "The existing database and seed must be retained for the old protocol."
        ) from None
    print(f"Mint backend: {args.backend}", file=sys.stderr)
    uvicorn.run(
        create_app(mint, report_balances=True, admin_token=args.admin_token),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
