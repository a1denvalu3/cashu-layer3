import pytest


@pytest.mark.parametrize("origin", ["http://127.0.0.1:5173", "https://wallet.example"])
@pytest.mark.parametrize(
    ("method", "path", "headers"),
    [
        ("GET", "/v1/info", "ngrok-skip-browser-warning,cache-control"),
        (
            "POST",
            "/v1/mint/quote/bolt11",
            "content-type,ngrok-skip-browser-warning",
        ),
    ],
)
def test_wallet_tunnel_preflight_accepts_request_headers(
    service, origin, method, path, headers
):
    response = service.client.http.options(
        path,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": headers,
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["access-control-allow-headers"] == headers
    assert method in response.headers["access-control-allow-methods"]
    assert "access-control-allow-credentials" not in response.headers


def test_wallet_can_read_mint_json_and_validation_errors(service):
    headers = {
        "Origin": "https://wallet.example",
        "ngrok-skip-browser-warning": "1",
    }
    info = service.client.http.get("/v1/info", headers=headers)
    assert info.status_code == 200
    assert info.headers["access-control-allow-origin"] == "*"
    assert info.json()["name"] == "cashu3"
    invalid = service.client.http.post(
        "/v1/mint/quote/bolt11", json={}, headers=headers
    )
    assert invalid.status_code == 422
    assert invalid.headers["access-control-allow-origin"] == "*"
