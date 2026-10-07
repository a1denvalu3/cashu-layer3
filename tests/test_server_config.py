from pathlib import Path

import pytest

from cashu.server import parse_args


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
    )
    args = parse_args([])
    assert args.backend == "lnd"
    assert args.data == Path("data/lnd-mint")
    assert args.lnd_endpoint == "https://localhost:8081"
    assert args.lnd_macaroon == Path("data/lnd mint/admin.macaroon")
    assert args.lnd_cert == Path("data/lnd-mint/tls.cert")
    assert args.port == 4338
    assert args.fee_limit_sat == 20


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
    "configuration",
    [
        "CASHU_MINT_BACKEND=unsupported\n",
        "CASHU_MINT_BACKEND=lnd\n",
        "CASHU_MINT_PORT=invalid\n",
        "CASHU_FEE_LIMIT_SAT=-1\n",
    ],
)
def test_invalid_configuration_fails_instead_of_using_fake(tmp_path, configuration):
    (tmp_path / ".env").write_text(configuration)
    with pytest.raises(SystemExit) as exc:
        parse_args([])
    assert exc.value.code == 2
