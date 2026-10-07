"""Python client with serializable operations for exact retries."""

import hashlib
import os
import time
from typing import Literal

import httpx
from pydantic import model_validator

from .crypto.bls import PublicKey, curve_order
from .crypto.ps import (
    G_NULL,
    Credential,
    MintPublicKeyPS,
    blind_transfer_commit,
    present,
    present_private,
    prove_owner_secret,
    unblind_issued,
    verify_credential,
)
from .models import (
    BurnRequest,
    BurnResponse,
    CheckRequest,
    CheckResponse,
    G1Hex,
    Hex32,
    KeysetResponse,
    MintRequest,
    PendingHTLC,
    PendingHTLCsResponse,
    QuoteRequest,
    QuoteResponse,
    SignatureResponse,
    SwapRequest,
    Token,
    WireModel,
    burn_binding,
    htlc_claim_scalar,
    preimage_hash,
    swap_binding,
)


class MintOperation(WireModel):
    kind: Literal["mint"] = "mint"
    id: Hex32
    quote: QuoteResponse
    secret: Hex32
    request: MintRequest
    preimage: Hex32 | None = None

    @model_validator(mode="after")
    def validate_preimage(self) -> "MintOperation":
        if (
            self.preimage is not None
            and preimage_hash(self.preimage) != self.quote.payment_hash
        ):
            raise ValueError("HTLC preimage does not match the mint quote")
        return self


class SwapOperation(WireModel):
    kind: Literal["swap"] = "swap"
    id: Hex32
    token: Token
    secret: Hex32
    blinding: Hex32
    u: G1Hex | None = None  # Retained for saved two-round swap operations.
    request: SwapRequest
    backing: PendingHTLC | None = None  # Local snapshot, never part of the swap RPC.


class BurnOperation(WireModel):
    kind: Literal["burn"] = "burn"
    id: Hex32
    token: Token
    request: BurnRequest


def fresh_secret() -> int:
    s = 0
    while not 0 < s < curve_order:
        s = int.from_bytes(os.urandom(32), "big")
    return s


class ClientError(Exception):
    pass


class Client:
    def __init__(self, mint_url: str, http: httpx.Client | None = None):
        self.mint_url = mint_url.rstrip("/")
        self.http = http or httpx.Client(base_url=self.mint_url, timeout=90)

    def _get(self, path: str) -> str:
        response = self.http.get(path)
        return self._response(response)

    def _post(self, path: str, request: WireModel) -> str:
        response = self.http.post(path, json=request.model_dump(mode="json"))
        return self._response(response)

    @staticmethod
    def _response(response: httpx.Response) -> str:
        if response.status_code != 200:
            raise ClientError(
                f"mint returned HTTP {response.status_code}: {response.text}"
            )
        return response.text

    def keyset(self) -> MintPublicKeyPS:
        response = KeysetResponse.model_validate_json(self._get("/v1/keys"))
        public = MintPublicKeyPS.from_bytes(bytes.fromhex(response.public_key))
        if response.id != public.keyset_id:
            raise ClientError("mint returned inconsistent keyset parameters")
        return public

    def quote(
        self, amount: int, payment_hash: str, expiry: int = 3600
    ) -> QuoteResponse:
        response = QuoteResponse.model_validate_json(
            self._post(
                "/v1/mint/quote/bolt11",
                QuoteRequest(amount=amount, payment_hash=payment_hash, expiry=expiry),
            )
        )
        if response.amount != amount or response.payment_hash != payment_hash:
            raise ClientError("mint quote changed the requested amount or payment hash")
        return response

    def get_quote(self, quote_id: str) -> QuoteResponse:
        return QuoteResponse.model_validate_json(
            self._get(f"/v1/mint/quote/bolt11/{quote_id}")
        )

    def pending_htlcs(self) -> PendingHTLCsResponse:
        return PendingHTLCsResponse.model_validate_json(self._get("/v1/htlcs/pending"))

    def check_backing(
        self, token: Token, snapshot: PendingHTLCsResponse | None = None
    ) -> PendingHTLC:
        """Verify the credential and find its backing in the public full list."""
        self.credential(token)
        return self._check_backing(token, snapshot or self.pending_htlcs())

    @staticmethod
    def _check_backing(token: Token, snapshot: PendingHTLCsResponse) -> PendingHTLC:
        matches = [
            entry
            for entry in snapshot.htlcs
            if entry.payment_hash == token.payment_hash
        ]
        if len(matches) != 1:
            raise ClientError("token has no unique pending backing HTLC at the mint")
        backing = matches[0]
        if backing.amount != token.amount:
            raise ClientError("pending backing HTLC amount does not match the token")
        if snapshot.block_height is not None:
            if (
                backing.expiry_height is None
                or backing.htlc_expiry_height is None
                or backing.expiry_height > backing.htlc_expiry_height
                or backing.expiry_height <= snapshot.block_height
                or backing.blocks_remaining
                != backing.expiry_height - snapshot.block_height
            ):
                raise ClientError("pending backing HTLC has expired or has no deadline")
        elif backing.expires_at is None or backing.expires_at <= max(
            snapshot.checked_at, int(time.time())
        ):
            raise ClientError("pending backing HTLC has expired or has no deadline")
        return backing

    def prepare_mint(self, quote_id: str, preimage: str | None = None) -> MintOperation:
        quote = self.get_quote(quote_id)
        s = fresh_secret()
        C, proof = prove_owner_secret(s)
        return MintOperation(
            id=os.urandom(32).hex(),
            quote=quote,
            secret=f"{s:064x}",
            request=MintRequest(
                quote=quote_id,
                commitment=C.format().hex(),
                proof=proof.to_bytes().hex(),
            ),
            preimage=preimage,
        )

    def finish_mint(self, operation: MintOperation) -> Token:
        signature = SignatureResponse.model_validate_json(
            self._post("/v1/mint/bolt11", operation.request)
        )
        if signature.keyset_id != operation.quote.keyset_id:
            raise ClientError("mint changed the quote's signing key")
        cred = Credential(
            u=PublicKey(compressed=bytes.fromhex(signature.u), group="G1"),
            v=PublicKey(compressed=bytes.fromhex(signature.v), group="G1"),
            h=htlc_claim_scalar(operation.quote.payment_hash, operation.quote.amount),
            s=int(operation.secret, 16),
            keyset_id=signature.keyset_id,
        )
        return self._token(
            cred,
            operation.quote.amount,
            operation.quote.payment_hash,
            operation.preimage,
        )

    def credential(self, token: Token) -> Credential:
        if token.mint.rstrip("/") != self.mint_url:
            raise ClientError("token belongs to another mint")
        cred = Credential.from_bytes(bytes.fromhex(token.credential))
        if cred.h != htlc_claim_scalar(token.payment_hash, token.amount):
            raise ClientError("token HTLC hash or amount does not match the credential")
        public = self.keyset()
        if not verify_credential(public, cred):
            raise ClientError("invalid token credential or amount")
        if (
            token.preimage is not None
            and preimage_hash(token.preimage) != token.payment_hash
        ):
            raise ClientError("HTLC preimage does not match the token payment hash")
        return cred

    def _token(
        self,
        cred: Credential,
        amount: int,
        payment_hash: str,
        preimage: str | None = None,
    ) -> Token:
        token = Token(
            mint=self.mint_url,
            amount=amount,
            payment_hash=payment_hash,
            credential=cred.to_bytes().hex(),
            preimage=preimage,
        )
        self.credential(token)
        return token

    def prepare_swap(self, token: Token) -> SwapOperation:
        cred = self.credential(token)
        backing = self._check_backing(token, self.pending_htlcs())
        public = self.keyset()
        s = fresh_secret()
        C, owner_proof = prove_owner_secret(s)
        binding = swap_binding(C.format())
        pres, o = present_private(public, cred, binding=binding)
        B, t, proof = blind_transfer_commit(
            public, cred.h, o, pres.kappa_h, binding=binding
        )
        return SwapOperation(
            id=os.urandom(32).hex(),
            token=token,
            secret=f"{s:064x}",
            blinding=f"{t:064x}",
            request=SwapRequest(
                presentation=pres.to_bytes().hex(),
                commitment=C.format().hex(),
                owner_proof=owner_proof.to_bytes().hex(),
                b=B.format().hex(),
                equality_proof=proof.to_bytes().hex(),
                version=2,
            ),
            backing=backing,
        )

    def finish_swap(self, operation: SwapOperation) -> Token:
        signature = SignatureResponse.model_validate_json(
            self._post("/v1/swap", operation.request)
        )
        if operation.request.version == 1 and signature.u != operation.u:
            raise ClientError("mint changed the blind swap base")
        public = self.keyset()
        u = PublicKey(compressed=bytes.fromhex(signature.u), group="G1")
        cred = Credential(
            u=u,
            v=unblind_issued(
                PublicKey(compressed=bytes.fromhex(signature.v), group="G1"),
                int(operation.blinding, 16),
                u if operation.request.version == 2 else public.Y_h1,
            ),
            h=htlc_claim_scalar(operation.token.payment_hash, operation.token.amount),
            s=int(operation.secret, 16),
            keyset_id=signature.keyset_id,
        )
        return self._token(
            cred,
            operation.token.amount,
            operation.token.payment_hash,
            operation.token.preimage,
        )

    def prepare_burn(
        self, token: Token, request: str, preimage: str | None = None
    ) -> BurnOperation:
        if preimage is not None:
            token = token.with_preimage(preimage)
        cred = self.credential(token)
        if token.preimage is None:
            raise ClientError(
                "token has no HTLC preimage; supply --preimage with pay or burn"
            )
        pres = present(cred, binding=burn_binding(token.amount, request))
        return BurnOperation(
            id=os.urandom(32).hex(),
            token=token,
            request=BurnRequest(
                amount=token.amount,
                presentation=pres.to_bytes().hex(),
                request=request,
                preimage=token.preimage,
            ),
        )

    def finish_burn(self, operation: BurnOperation) -> BurnResponse:
        response = BurnResponse.model_validate_json(
            self._post("/v1/burn", operation.request)
        )
        if (
            response.amount != operation.token.amount
            or response.payment_hash != operation.token.payment_hash
        ):
            raise ClientError("mint returned an inconsistent redemption receipt")
        if (
            hashlib.sha256(bytes.fromhex(response.preimage)).hexdigest()
            != response.payment_hash
        ):
            raise ClientError("mint returned an invalid redemption preimage")
        if operation.request.preimage is not None:
            from .lightning import invoice_details

            payout_hash, _ = invoice_details(operation.request.request)
            if response.preimage != operation.request.preimage:
                raise ClientError("mint changed the backing HTLC preimage")
            if (
                response.payout_hash != payout_hash
                or response.payout_preimage is None
                or preimage_hash(response.payout_preimage) != payout_hash
            ):
                raise ClientError("mint returned an invalid payout receipt")
        return response

    def check(self, nullifiers: list[str]) -> CheckResponse:
        return CheckResponse.model_validate_json(
            self._post("/v1/checkstate", CheckRequest(nullifiers=nullifiers))
        )

    @staticmethod
    def nullifier(token: Token) -> str:
        cred = Credential.from_bytes(bytes.fromhex(token.credential))
        return (G_NULL * cred.s).format().hex()

    def close(self) -> None:
        self.http.close()
