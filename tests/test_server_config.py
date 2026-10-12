import sqlite3
from pathlib import Path

import pytest
from conftest import SEED
from fastapi.testclient import TestClient

from cashu.api import create_app
from cashu.mint import Mint
from cashu.server import create_backend, parse_args


@pytest.fixture(autouse=True)
def isolated_configuration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in (
        "CASHU_MINT_BACKEND",
        "CASHU_MINT_DATA",
        "CASHU_MINT_HOST",
        "CASHU_MINT_PORT",
        "CASHU_LND_ENDPOINT",
        "CASHU_LND_CERT",
        "CASHU_LND_MACAROON",
        "CASHU_FEE_LIMIT_SAT",
        "CASHU_LND_HOLD_EXPIRY_DELTA",
        "CASHU_LND_TOKEN_LIFETIME_BLOCKS",
        "CASHU_SETTLE_MARGIN_BLOCKS",
        "CASHU_SETTLE_MARGIN_SECONDS",
        "CASHU_SPARK_MNEMONIC",
        "CASHU_SPARK_MNEMONIC_FILE",
        "CASHU_SPARK_API_KEY",
        "CASHU_SPARK_NETWORK",
        "CASHU_SPARK_CONFIG_FILE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_dotenv_selects_lnd_and_its_credentials(tmp_path):
    (tmp_path / ".env").write_text(
        "CASHU_MINT_BACKEND=lnd\n"
        "CASHU_MINT_DATA=data/lnd-mint\n"
        "CASHU_LND_ENDPOINT=https://localhost:8081\n"
        'CASHU_LND_MACAROON="data/lnd mint/admin.macaroon"\n'
        "CASHU_LND_CERT=data/lnd-mint/tls.cert\n"
        "CASHU_MINT_PORT=4338\n"
        "CASHU_FEE_LIMIT_SAT=20\n"
        "CASHU_LND_HOLD_EXPIRY_DELTA=30\n"
        "CASHU_LND_TOKEN_LIFETIME_BLOCKS=1000\n"
    )
    args = parse_args([])
    assert args.backend == "lnd"
    assert args.data == Path("data/lnd-mint")
    assert args.lnd_endpoint == "https://localhost:8081"
    assert args.lnd_macaroon == Path("data/lnd mint/admin.macaroon")
    assert args.lnd_cert == Path("data/lnd-mint/tls.cert")
    assert args.port == 4338
    assert args.fee_limit_sat == 20
    assert args.lnd_hold_expiry_delta == 30
    assert args.lnd_token_lifetime_blocks == 1000


def test_cli_overrides_environment_which_overrides_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("CASHU_MINT_BACKEND=fake\nCASHU_MINT_PORT=4338\n")
    monkeypatch.setenv("CASHU_MINT_BACKEND", "lnd")
    monkeypatch.setenv("CASHU_MINT_PORT", "5338")
    monkeypatch.setenv("CASHU_LND_ENDPOINT", "https://localhost:8081")
    monkeypatch.setenv("CASHU_LND_MACAROON", "admin.macaroon")
    assert parse_args([]).backend == "lnd"
    assert parse_args([]).port == 5338
    args = parse_args(["--backend", "fake", "--port", "6338"])
    assert args.backend == "fake"
    assert args.port == 6338


@pytest.mark.parametrize(
    "options,lifetime,cancellation,margin",
    [
        ([], 2016, 18, 6),
        (["--lnd-token-lifetime-blocks", "1008"], 1008, 18, 6),
        (
            ["--lnd-hold-expiry-delta", "30", "--settle-margin-blocks", "12"],
            2016,
            30,
            12,
        ),
        (["--lnd-token-lifetime-blocks", "65511"], 65511, 18, 6),
    ],
)
def test_lnd_lifetime_includes_margins_and_is_advertised(
    tmp_path, options, lifetime, cancellation, margin
):
    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    args = parse_args(
        [
            "--backend",
            "lnd",
            "--lnd-endpoint",
            "https://lnd.test",
            "--lnd-macaroon",
            str(macaroon),
            *options,
        ]
    )
    backend = create_backend(args)
    assert backend.cltv_expiry == lifetime + cancellation + margin
    assert backend.hold_expiry_delta == cancellation
    mint = Mint(tmp_path / "mint.sqlite3", SEED, backend, args.settle_margin_blocks)
    with TestClient(create_app(mint, recovery=False)) as http:
        info = http.get("/v1/info").json()
    assert info["default_token_lifetime_blocks"] == lifetime
    assert info["funding_htlc_cltv_delta"] == lifetime + cancellation + margin
    assert info["settle_margin_blocks"] == margin


def test_time_based_backend_does_not_advertise_a_block_lifetime(tmp_path):
    backend = create_backend(parse_args([]))
    mint = Mint(tmp_path / "mint.sqlite3", SEED, backend)
    with TestClient(create_app(mint, recovery=False)) as http:
        info = http.get("/v1/info").json()
    assert info["default_token_lifetime_blocks"] is None
    assert info["funding_htlc_cltv_delta"] is None
    assert info["settle_margin_seconds"] == 120


@pytest.mark.parametrize(
    "configuration",
    [
        "CASHU_MINT_BACKEND=unsupported\n",
        "CASHU_MINT_BACKEND=lnd\n",
        "CASHU_MINT_PORT=invalid\n",
        "CASHU_FEE_LIMIT_SAT=-1\n",
        "CASHU_LND_HOLD_EXPIRY_DELTA=-1\n",
        "CASHU_LND_TOKEN_LIFETIME_BLOCKS=0\n",
        "CASHU_LND_TOKEN_LIFETIME_BLOCKS=-1\n",
        "CASHU_LND_TOKEN_LIFETIME_BLOCKS=65535\n",
        "CASHU_SETTLE_MARGIN_BLOCKS=0\n",
        "CASHU_SETTLE_MARGIN_SECONDS=-1\n",
        "CASHU_MINT_BACKEND=spark\n",
        "CASHU_MINT_BACKEND=spark\nCASHU_SPARK_MNEMONIC=test\nCASHU_SPARK_NETWORK=signet\n",
        "CASHU_MINT_BACKEND=spark\nCASHU_SPARK_MNEMONIC=test\nCASHU_SPARK_NETWORK=regtest\n",
        "CASHU_MINT_BACKEND=spark\nCASHU_SPARK_MNEMONIC=test\nCASHU_SPARK_CONFIG_FILE=local.json\n",
    ],
)
def test_invalid_configuration_fails_instead_of_using_fake(tmp_path, configuration):
    (tmp_path / ".env").write_text(configuration)
    with pytest.raises(SystemExit) as exc:
        parse_args([])
    assert exc.value.code == 2


def test_spark_configuration_uses_file_and_environment(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "CASHU_MINT_BACKEND=spark\n"
        "CASHU_SPARK_MNEMONIC_FILE=wallet.mnemonic\n"
        "CASHU_SPARK_API_KEY=test-key\n"
        "CASHU_SPARK_NETWORK=mainnet\n"
    )
    args = parse_args([])
    assert args.backend == "spark"
    assert args.spark_mnemonic_file == Path("wallet.mnemonic")
    assert args.spark_api_key == "test-key"
    monkeypatch.setenv("CASHU_SPARK_NETWORK", "regtest")
    args = parse_args(["--spark-config-file", "local.json"])
    assert args.spark_network == "regtest"
    assert args.spark_config_file == Path("local.json")


def test_startup_reports_old_database_without_resetting_it(tmp_path, monkeypatch):
    from cashu.server import main

    data = tmp_path / "old-mint"
    data.mkdir()
    seed = b"s" * 32
    (data / "mint.seed").write_bytes(seed)
    with sqlite3.connect(data / "mint.sqlite3") as db:
        db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO metadata VALUES ('protocol','ps-htlc-claim-v2')")
        db.execute("CREATE TABLE quotes (quote TEXT PRIMARY KEY)")
        db.execute("INSERT INTO quotes VALUES ('previous-claim')")
    (tmp_path / ".env").write_text(f"CASHU_MINT_DATA={data}\n")
    monkeypatch.setattr("sys.argv", ["mint"])
    with pytest.raises(SystemExit) as exc:
        main()
    message = str(exc.value)
    assert str(data / "mint.sqlite3") in message
    assert "CASHU_MINT_DATA" in message
    assert "--data <new-directory>" in message
    assert (data / "mint.seed").read_bytes() == seed
    with sqlite3.connect(data / "mint.sqlite3") as db:
        assert db.execute("SELECT * FROM metadata").fetchall() == [
            ("protocol", "ps-htlc-claim-v2")
        ]
        assert db.execute("SELECT * FROM quotes").fetchall() == [("previous-claim",)]
        assert {
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        } == {"metadata", "quotes"}
