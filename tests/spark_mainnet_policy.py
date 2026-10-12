"""Verify the default mainnet SSP's CLTV using unpaid HODL invoices.

Run explicitly with `poetry run python tests/spark_mainnet_policy.py`.
Creates an ephemeral, unfunded identity; never submits a payment or prints
invoices, private keys, challenges, or session tokens. No Breez API key needed.
"""

import base64
import hashlib
import os
import sys
from pathlib import Path

import breez_sdk_spark as breez
import httpx
from bolt11 import decode
from coincurve import PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cashu.sparkl2 import SPARK_MAINNET_MIN_FINAL_CLTV_EXPIRY_DELTA  # noqa: E402


def base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def verify_policy() -> None:
    config = breez.default_config(breez.Network.MAINNET).spark_config.ssp_config
    url = f"{config.base_url.rstrip('/')}/{config.schema_endpoint or 'graphql/spark/2025-03-19'}"
    with httpx.Client(timeout=15) as http:

        def query(operation: str, variables: dict) -> dict:
            response = http.post(url, json={"query": operation, "variables": variables})
            response.raise_for_status()
            body = response.json()
            if body.get("errors"):
                raise RuntimeError(
                    "mainnet SSP rejected the policy verification request"
                )
            return body["data"]

        key = PrivateKey(os.urandom(32))
        pubkey = key.public_key.format(compressed=True).hex()
        challenge = query(
            "mutation GetChallenge($input: GetChallengeInput!) { "
            "get_challenge(input: $input) { protected_challenge } }",
            {"input": {"public_key": pubkey}},
        )["get_challenge"]["protected_challenge"]
        decoded = base64.urlsafe_b64decode(challenge + "=" * (-len(challenge) % 4))
        session = query(
            "mutation VerifyChallenge($input: VerifyChallengeInput!) { "
            "verify_challenge(input: $input) { session_token } }",
            {
                "input": {
                    "protected_challenge": challenge,
                    "signature": base64url(key.sign(decoded)),
                    "identity_public_key": pubkey,
                }
            },
        )["verify_challenge"]["session_token"]
        http.headers["Authorization"] = "Bearer " + session
        for expiry in (60, 3600):
            payment_hash = hashlib.sha256(os.urandom(32)).hexdigest()
            result = query(
                "mutation RequestLightningReceive($input: RequestLightningReceiveInput!) { "
                "request_lightning_receive(input: $input) { request { "
                "invoice { encoded_invoice } } } }",
                {
                    "input": {
                        "network": "MAINNET",
                        "amount_sats": 1000,
                        "payment_hash": payment_hash,
                        "expiry_secs": expiry,
                        "memo": "cashu3 unpaid CLTV verification",
                        "include_spark_address": False,
                    }
                },
            )
            invoice = decode(
                result["request_lightning_receive"]["request"]["invoice"][
                    "encoded_invoice"
                ]
            )
            if (
                invoice.currency != "bc"
                or invoice.payment_hash != payment_hash
                or invoice.amount_msat != 1_000_000
            ):
                raise RuntimeError("mainnet SSP returned an inconsistent invoice")
            delta = invoice.min_final_cltv_expiry
            if delta != SPARK_MAINNET_MIN_FINAL_CLTV_EXPIRY_DELTA:
                raise RuntimeError(
                    f"mainnet CLTV policy changed: received {delta} blocks"
                )
            print(
                f"PASS: mainnet HODL minimum final CLTV={delta} blocks, "
                f"invoice payment window={invoice.expiry}s; invoice remains unpaid",
                flush=True,
            )


if __name__ == "__main__":
    try:
        verify_policy()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
    except (httpx.HTTPError, ValueError, KeyError):
        raise SystemExit(
            "mainnet policy verification failed; no payment was sent"
        ) from None
