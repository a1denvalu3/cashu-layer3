import sqlite3
from pathlib import Path

import pytest
from conftest import SEED
from fastapi.testclient import TestClient

from tmp_cash.api import create_app
from tmp_cash.mint import Mint
from tmp_cash.server import create_backend, parse_args


@pytest.fixture(autouse=True)
def isolated_configuration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in (
        "TMP_CASH_MINT_BACKEND",
        "TMP_CASH_MINT_DATA",
        "TMP_CASH_MINT_HOST",
        "TMP_CASH_MINT_PORT",
        "TMP_CASH_ADMIN_TOKEN",
        "TMP_CASH_LND_ENDPOINT",
        "TMP_CASH_LND_CERT",
        "TMP_CASH_LND_MACAROON",
        "TMP_CASH_FEE_LIMIT_SAT",
        "TMP_CASH_LND_HOLD_EXPIRY_DELTA",
        "TMP_CASH_LND_TOKEN_LIFETIME_BLOCKS",
        "TMP_CASH_SETTLE_MARGIN_BLOCKS",
        "TMP_CASH_SETTLE_MARGIN_SECONDS",
        "TMP_CASH_SPARK_MNEMONIC",
        "TMP_CASH_SPARK_MNEMONIC_FILE",
        "TMP_CASH_SPARK_API_KEY",
        "TMP_CASH_SPARK_NETWORK",
        "TMP_CASH_SPARK_CONFIG_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.replace("TMP_CASH_", "CASHU_", 1), raising=False)


@pytest.mark.parametrize(
    "dotenv,environment,expected_port",
    [
        ("CASHU_MINT_PORT=4338\n", {}, 4338),
        ("CASHU_MINT_PORT=4338\nTMP_CASH_MINT_PORT=5338\n", {}, 5338),
        ("TMP_CASH_MINT_PORT=4338\nCASHU_MINT_PORT=5338\n", {}, 4338),
        ("TMP_CASH_MINT_PORT=4338\n", {"CASHU_MINT_PORT": "5338"}, 5338),
        (
            "CASHU_MINT_PORT=4338\n",
            {"TMP_CASH_MINT_PORT": "5338", "CASHU_MINT_PORT": "6338"},
            5338,
        ),
    ],
)
def test_legacy_configuration_preserves_source_precedence(
    tmp_path, monkeypatch, dotenv, environment, expected_port
):
    (tmp_path / ".env").write_text(dotenv)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    assert parse_args([]).port == expected_port
    assert parse_args(["--port", "7338"]).port == 7338


def test_legacy_configuration_keeps_the_backend_data_and_operator_secret(tmp_path):
    (tmp_path / ".env").write_text(
        "CASHU_MINT_BACKEND=lnd\n"
        "CASHU_MINT_DATA=data/existing-mint\n"
        "CASHU_LND_ENDPOINT=https://localhost:8081\n"
        "CASHU_LND_MACAROON=admin.macaroon\n"
        "CASHU_ADMIN_TOKEN=existing-secret\n"
    )
    args = parse_args([])
    assert args.backend == "lnd"
    assert args.data == Path("data/existing-mint")
    assert args.lnd_endpoint == "https://localhost:8081"
    assert args.lnd_macaroon == Path("admin.macaroon")
    assert args.admin_token == "existing-secret"


def test_dotenv_selects_lnd_and_its_credentials(tmp_path):
    (tmp_path / ".env").write_text(
        "TMP_CASH_MINT_BACKEND=lnd\n"
        "TMP_CASH_MINT_DATA=data/lnd-mint\n"
        "TMP_CASH_LND_ENDPOINT=https://localhost:8081\n"
        'TMP_CASH_LND_MACAROON="data/lnd mint/admin.macaroon"\n'
        "TMP_CASH_LND_CERT=data/lnd-mint/tls.cert\n"
        "TMP_CASH_MINT_PORT=4338\n"
        "TMP_CASH_FEE_LIMIT_SAT=20\n"
        "TMP_CASH_LND_HOLD_EXPIRY_DELTA=30\n"
        "TMP_CASH_LND_TOKEN_LIFETIME_BLOCKS=1000\n"
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
    (tmp_path / ".env").write_text(
        "TMP_CASH_MINT_BACKEND=fake\nTMP_CASH_MINT_PORT=4338\n"
    )
    monkeypatch.setenv("TMP_CASH_MINT_BACKEND", "lnd")
    monkeypatch.setenv("TMP_CASH_MINT_PORT", "5338")
    monkeypatch.setenv("TMP_CASH_LND_ENDPOINT", "https://localhost:8081")
    monkeypatch.setenv("TMP_CASH_LND_MACAROON", "admin.macaroon")
    assert parse_args([]).backend == "lnd"
    assert parse_args([]).port == 5338
    args = parse_args(["--backend", "fake", "--port", "6338"])
    assert args.backend == "fake"
    assert args.port == 6338


def test_operator_secret_is_optional_and_environment_overrides_dotenv(
    tmp_path, monkeypatch
):
    assert parse_args([]).admin_token is None
    (tmp_path / ".env").write_text("TMP_CASH_ADMIN_TOKEN=file-secret\n")
    assert parse_args([]).admin_token == "file-secret"
    monkeypatch.setenv("TMP_CASH_ADMIN_TOKEN", "environment-secret")
    assert parse_args([]).admin_token == "environment-secret"


def test_mint_command_passes_operator_secret_to_the_api(tmp_path, monkeypatch):
    from tmp_cash.server import main

    monkeypatch.setenv("TMP_CASH_ADMIN_TOKEN", "operator-test-secret")
    monkeypatch.setattr("sys.argv", ["mint", "--data", str(tmp_path / "mint")])

    def run(app, **kwargs):
        with TestClient(app) as http:
            assert http.post("/v1/admin/fund", json={"amount": 1000}).status_code == 401
            response = http.post(
                "/v1/admin/fund",
                json={"amount": 1000},
                headers={"Authorization": "Bearer operator-test-secret"},
            )
            assert response.status_code == 200
            assert response.json()["amount"] == 1000

    monkeypatch.setattr("tmp_cash.server.uvicorn.run", run)
    main()


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
        "TMP_CASH_MINT_BACKEND=unsupported\n",
        "TMP_CASH_MINT_BACKEND=lnd\n",
        "TMP_CASH_MINT_PORT=invalid\n",
        "TMP_CASH_FEE_LIMIT_SAT=-1\n",
        "TMP_CASH_LND_HOLD_EXPIRY_DELTA=-1\n",
        "TMP_CASH_LND_TOKEN_LIFETIME_BLOCKS=0\n",
        "TMP_CASH_LND_TOKEN_LIFETIME_BLOCKS=-1\n",
        "TMP_CASH_LND_TOKEN_LIFETIME_BLOCKS=65535\n",
        "TMP_CASH_SETTLE_MARGIN_BLOCKS=0\n",
        "TMP_CASH_SETTLE_MARGIN_SECONDS=-1\n",
        "TMP_CASH_MINT_BACKEND=spark\n",
        "TMP_CASH_MINT_BACKEND=spark\nTMP_CASH_SPARK_MNEMONIC=test\nTMP_CASH_SPARK_NETWORK=signet\n",
        "TMP_CASH_MINT_BACKEND=spark\nTMP_CASH_SPARK_MNEMONIC=test\nTMP_CASH_SPARK_NETWORK=regtest\n",
        "TMP_CASH_MINT_BACKEND=spark\nTMP_CASH_SPARK_MNEMONIC=test\nTMP_CASH_SPARK_CONFIG_FILE=local.json\n",
    ],
)
def test_invalid_configuration_fails_instead_of_using_fake(tmp_path, configuration):
    (tmp_path / ".env").write_text(configuration)
    with pytest.raises(SystemExit) as exc:
        parse_args([])
    assert exc.value.code == 2


def test_spark_configuration_uses_file_and_environment(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "TMP_CASH_MINT_BACKEND=spark\n"
        "TMP_CASH_SPARK_MNEMONIC_FILE=wallet.mnemonic\n"
        "TMP_CASH_SPARK_API_KEY=test-key\n"
        "TMP_CASH_SPARK_NETWORK=mainnet\n"
    )
    args = parse_args([])
    assert args.backend == "spark"
    assert args.spark_mnemonic_file == Path("wallet.mnemonic")
    assert args.spark_api_key == "test-key"
    monkeypatch.setenv("TMP_CASH_SPARK_NETWORK", "regtest")
    args = parse_args(["--spark-config-file", "local.json"])
    assert args.spark_network == "regtest"
    assert args.spark_config_file == Path("local.json")


def test_mint_command_reports_balances_at_startup(tmp_path, monkeypatch, caplog):
    import logging

    from tmp_cash.server import main

    monkeypatch.setattr("sys.argv", ["mint", "--data", str(tmp_path / "mint")])

    def run(app, **kwargs):
        with TestClient(app) as http:
            assert http.get("/v1/info").status_code == 200

    monkeypatch.setattr("tmp_cash.server.uvicorn.run", run)
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        main()
    assert "Backend balances (fake):" in caplog.text
    assert "No real wallet balance (simulated backend)" in caplog.text


def test_startup_reports_old_database_without_resetting_it(tmp_path, monkeypatch):
    from tmp_cash.server import main

    data = tmp_path / "old-mint"
    data.mkdir()
    seed = b"s" * 32
    (data / "mint.seed").write_bytes(seed)
    with sqlite3.connect(data / "mint.sqlite3") as db:
        db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO metadata VALUES ('protocol','ps-htlc-claim-v2')")
        db.execute("CREATE TABLE quotes (quote TEXT PRIMARY KEY)")
        db.execute("INSERT INTO quotes VALUES ('previous-claim')")
    (tmp_path / ".env").write_text(f"TMP_CASH_MINT_DATA={data}\n")
    monkeypatch.setattr("sys.argv", ["mint"])
    with pytest.raises(SystemExit) as exc:
        main()
    message = str(exc.value)
    assert str(data / "mint.sqlite3") in message
    assert "TMP_CASH_MINT_DATA" in message
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
