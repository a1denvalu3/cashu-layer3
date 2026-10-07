import importlib.util
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

SPEC = importlib.util.spec_from_file_location(
    "cashu_regtest_runner", Path(__file__).parent / "regtest/run.py"
)
regtest = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = regtest
SPEC.loader.exec_module(regtest)


@pytest.mark.parametrize("failure", ["missing", "stopped", "docker-unavailable"])
def test_missing_regtest_fails_without_provisioning(monkeypatch, capsys, failure):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert command[:2] == ["docker", "inspect"]
        if failure == "docker-unavailable":
            raise FileNotFoundError("docker")
        return subprocess.CompletedProcess(
            command,
            1 if failure == "missing" else 0,
            stdout="" if failure == "missing" else "false lnd-1",
            stderr="",
        )

    monkeypatch.setattr(regtest.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["run.py"])
    with pytest.raises(SystemExit) as exc:
        regtest.main()
    assert exc.value.code == 1
    assert len(calls) == 1
    error = capsys.readouterr().err
    assert "cashu-regtest" in error
    assert "Set up and start" in error
    assert "Traceback" not in error


def test_container_connection_uses_hostname_without_a_checkout(monkeypatch):
    monkeypatch.setattr(regtest, "execute", lambda command, label: "true custom-lnd")
    node = regtest.Node.connect("my-running-lnd")
    command = node.command("getinfo")
    assert command[:3] == ["docker", "exec", "my-running-lnd"]
    assert "--rpcserver=custom-lnd:10009" in command


@pytest.fixture
def network(monkeypatch):
    nodes = tuple(
        regtest.Node(role, f"{role}:10009") for role in ("payer", "mint", "receiver")
    )
    info = {
        n.container: {
            "identity_pubkey": n.container,
            "synced_to_chain": True,
            "chains": [{"chain": "bitcoin", "network": "regtest"}],
        }
        for n in nodes
    }
    channels = {
        n.container: [
            {"active": True, "local_balance": "1000000", "remote_balance": "1000000"}
        ]
        for n in nodes
    }

    def ln(node, command):
        assert command in ("getinfo", "listchannels")
        return (
            info[node.container]
            if command == "getinfo"
            else {"channels": channels[node.container]}
        )

    monkeypatch.setattr(regtest.Node, "ln", ln)
    return nodes, info, channels


def test_ready_network_checks_existing_nodes(network):
    nodes, _, _ = network
    assert regtest.verify_network(*nodes) == "mint"


@pytest.mark.parametrize(
    "failure", ["wrong-network", "unsynced", "no-outbound", "no-inbound", "same-node"]
)
def test_unready_network_is_rejected_before_creating_invoices(network, failure):
    nodes, info, channels = network
    if failure == "wrong-network":
        info["mint"]["chains"][0]["network"] = "mainnet"
    elif failure == "unsynced":
        info["mint"]["synced_to_chain"] = False
    elif failure == "no-outbound":
        channels["mint"][0]["local_balance"] = "0"
    elif failure == "no-inbound":
        channels["receiver"][0]["remote_balance"] = "0"
    elif failure == "same-node":
        info["receiver"]["identity_pubkey"] = "mint"
    with pytest.raises(regtest.RegtestError):
        regtest.verify_network(*nodes)


@pytest.mark.parametrize("failure", ["wrong-node", "unavailable"])
def test_rest_endpoint_must_authenticate_the_configured_mint(
    monkeypatch, tmp_path, failure
):
    macaroon = tmp_path / "admin.macaroon"
    macaroon.write_bytes(b"test-only macaroon")
    tls = object()
    monkeypatch.setattr(regtest.ssl, "create_default_context", lambda **kwargs: tls)
    real_client = httpx.Client

    def respond(request):
        assert request.headers["Grpc-Metadata-macaroon"] == macaroon.read_bytes().hex()
        if failure == "unavailable":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, json={"identity_pubkey": "different-node"})

    def client(**kwargs):
        assert kwargs.pop("verify") is tls
        return real_client(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(regtest.httpx, "Client", client)
    with pytest.raises(regtest.RegtestError):
        regtest.verify_rest(
            "https://localhost:8081", macaroon, tmp_path / "tls.cert", "mint"
        )
