"""Version-3 client; all secrets and commitments can be saved before a POST."""

import os
import time
from typing import Literal

import httpx
from pydantic import model_validator

from .crypto.bls import PublicKey, curve_order
from .crypto.ps import (
    G1,
    G_NULL,
    Credential,
    MintPublicKeyPS,
    present,
    present_private,
    prove_linear,
    prove_owner_secret,
    unblind_issued,
    verify_credential,
)
from .keyset import COMMON_H_DST, common_h_statements, denominations, keyset_id
from .models import (
    BlindOutput,
    ChangeOutput,
    CheckRequest,
    CheckResponse,
    Hex32,
    KeysetResponse,
    MeltInput,
    MeltQuoteRequest,
    MeltQuoteResponse,
    MeltRequest,
    MeltResponse,
    MintRequest,
    Output,
    PendingHTLC,
    PendingHTLCsResponse,
    QuoteRequest,
    QuoteResponse,
    SignaturesResponse,
    SwapInput,
    SwapRequest,
    Token,
    TokenProof,
    WireModel,
    htlc_claim_scalar,
    melt_binding,
    preimage_hash,
    swap_binding,
)


class MintOperation(WireModel):
    kind: Literal["mint"] = "mint"
    id: Hex32
    quote: QuoteResponse
    secrets: list[Hex32]
    request: MintRequest
    preimage: Hex32 | None = None

    @model_validator(mode="after")
    def validate_recovery(self):
        if (
            len(self.secrets) != len(self.request.outputs)
            or self.request.quote != self.quote.quote
        ):
            raise ValueError("incomplete mint recovery material")
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
    secrets: list[Hex32]
    blindings: list[Hex32]
    request: SwapRequest
    backing: PendingHTLC

    @model_validator(mode="after")
    def validate_recovery(self):
        if (
            len(self.secrets) != len(self.request.outputs)
            or len(self.blindings) != len(self.request.outputs)
            or len(self.token.proofs) != len(self.request.inputs)
        ):
            raise ValueError("incomplete swap recovery material")
        return self


class MeltOperation(WireModel):
    kind: Literal["melt"] = "melt"
    id: Hex32
    token: Token
    quote: MeltQuoteResponse
    secrets: list[Hex32]
    request: MeltRequest

    @model_validator(mode="after")
    def validate_recovery(self):
        if (
            len(self.secrets) != len(self.request.change)
            or self.request.quote != self.quote.quote
            or len(self.token.proofs) != len(self.request.inputs)
        ):
            raise ValueError("incomplete melt recovery material")
        return self


class ClientError(Exception):
    pass


def fresh_secret() -> int:
    while True:
        scalar = int.from_bytes(os.urandom(32), "big")
        if 0 < scalar < curve_order:
            return scalar


def output(amount: int) -> tuple[Output, str]:
    secret = fresh_secret()
    point, proof = prove_owner_secret(secret)
    return Output(
        amount=amount, commitment=point.format().hex(), proof=proof.to_bytes().hex()
    ), f"{secret:064x}"


class Client:
    def __init__(self, mint_url: str, http: httpx.Client | None = None):
        self.mint_url = mint_url.rstrip("/")
        self.http = http or httpx.Client(base_url=self.mint_url, timeout=90)

    @staticmethod
    def _response(response: httpx.Response) -> str:
        if response.status_code != 200:
            raise ClientError(
                f"mint returned HTTP {response.status_code}: {response.text}"
            )
        return response.text

    def _get(self, path: str) -> str:
        return self._response(self.http.get(path))

    def _post(self, path: str, request: WireModel) -> str:
        return self._response(
            self.http.post(path, json=request.model_dump(mode="json"))
        )

    def keyset(self) -> dict[int, MintPublicKeyPS]:
        response = KeysetResponse.model_validate_json(self._get("/v1/keys"))
        keys = {
            amount: MintPublicKeyPS.from_bytes(bytes.fromhex(raw))
            for amount, raw in response.keys.items()
        }
        if keyset_id(keys) != response.id:
            raise ClientError(
                "mint returned inconsistent denomination keyset parameters"
            )
        for public in keys.values():
            public._keyset_id = response.id
        return keys

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
            raise ClientError("mint quote changed the amount or payment hash")
        return response

    def get_quote(self, quote_id: str) -> QuoteResponse:
        return QuoteResponse.model_validate_json(
            self._get(f"/v1/mint/quote/bolt11/{quote_id}")
        )

    def pending_htlcs(self) -> PendingHTLCsResponse:
        return PendingHTLCsResponse.model_validate_json(self._get("/v1/htlcs/pending"))

    def credential(
        self, proof: TokenProof, keys: dict[int, MintPublicKeyPS] | None = None
    ) -> Credential:
        cred = Credential.from_bytes(bytes.fromhex(proof.credential))
        keys = keys or self.keyset()
        if cred.h != htlc_claim_scalar(
            proof.payment_hash, proof.backing_amount
        ) or not verify_credential(keys[proof.amount], cred):
            raise ClientError("invalid token credential, denomination or HTLC backing")
        return cred

    def credentials(self, token: Token, keys=None) -> list[Credential]:
        if token.mint.rstrip("/") != self.mint_url:
            raise ClientError("token belongs to another mint")
        keys = keys or self.keyset()
        return [self.credential(proof, keys) for proof in token.proofs]

    @staticmethod
    def _check_backing(
        proof: TokenProof, snapshot: PendingHTLCsResponse
    ) -> PendingHTLC:
        entries = [p for p in snapshot.htlcs if p.payment_hash == proof.payment_hash]
        if len(entries) != 1 or entries[0].amount != proof.backing_amount:
            raise ClientError("token has no matching pending backing HTLC")
        entry = entries[0]
        if snapshot.block_height is not None:
            if (
                entry.expiry_height is None
                or entry.htlc_expiry_height is None
                or entry.expiry_height > entry.htlc_expiry_height
                or entry.blocks_remaining != entry.expiry_height - snapshot.block_height
                or entry.melt_deadline_height is None
                or entry.melt_deadline_height > entry.expiry_height
                or snapshot.block_height >= entry.melt_deadline_height
            ):
                raise ClientError(
                    "backing HTLC redemption deadline has passed or is inconsistent"
                )
        elif (
            entry.expires_at is None
            or entry.melt_deadline is None
            or entry.melt_deadline > entry.expires_at
            or entry.melt_deadline <= max(int(time.time()), snapshot.checked_at)
        ):
            raise ClientError(
                "backing HTLC redemption deadline has passed or is inconsistent"
            )
        return entry

    def check_backing(
        self, token: Token, snapshot: PendingHTLCsResponse | None = None
    ) -> list[PendingHTLC]:
        self.credentials(token)
        snapshot = snapshot or self.pending_htlcs()
        return [self._check_backing(proof, snapshot) for proof in token.proofs]

    def prepare_mint(self, quote_id: str, preimage: str | None = None) -> MintOperation:
        quote = self.get_quote(quote_id)
        if preimage is not None and preimage_hash(preimage) != quote.payment_hash:
            raise ClientError("HTLC preimage does not match the mint quote")
        prepared = [output(amount) for amount in denominations(quote.amount)]
        return MintOperation(
            id=os.urandom(32).hex(),
            quote=quote,
            secrets=[s for _, s in prepared],
            request=MintRequest(quote=quote_id, outputs=[p for p, _ in prepared]),
            preimage=preimage,
        )

    def _proof(
        self,
        signature,
        secret: str,
        payment_hash: str,
        backing_amount: int,
        preimage: str | None,
        keys,
        blinding: str | None = None,
    ) -> TokenProof:
        public = keys[signature.amount]
        if signature.keyset_id != public.keyset_id:
            raise ClientError("mint changed the signing keyset")
        u = PublicKey(compressed=bytes.fromhex(signature.u), group="G1")
        v = PublicKey(compressed=bytes.fromhex(signature.v), group="G1")
        if blinding is not None:
            v = unblind_issued(v, int(blinding, 16), u)
        cred = Credential(
            u=u,
            v=v,
            h=htlc_claim_scalar(payment_hash, backing_amount),
            s=int(secret, 16),
            keyset_id=signature.keyset_id,
        )
        proof = TokenProof(
            amount=signature.amount,
            payment_hash=payment_hash,
            backing_amount=backing_amount,
            credential=cred.to_bytes().hex(),
            preimage=preimage,
        )
        self.credential(proof, keys)
        return proof

    def finish_mint(self, operation: MintOperation) -> Token:
        response = SignaturesResponse.model_validate_json(
            self._post("/v1/mint/bolt11", operation.request)
        )
        if [p.amount for p in response.signatures] != [
            p.amount for p in operation.request.outputs
        ]:
            raise ClientError("mint returned inconsistent output denominations")
        keys = self.keyset()
        if any(p.keyset_id != operation.quote.keyset_id for p in response.signatures):
            raise ClientError("mint changed the quote's signing keyset")
        return Token(
            mint=self.mint_url,
            proofs=[
                self._proof(
                    signature,
                    secret,
                    operation.quote.payment_hash,
                    operation.quote.amount,
                    operation.preimage,
                    keys,
                )
                for signature, secret in zip(response.signatures, operation.secrets)
            ],
        )

    def prepare_swap(
        self, token: Token, amounts: list[int] | None = None
    ) -> SwapOperation:
        _ = token.payment_hash  # Reject mixed-HTLC swaps locally.
        keys = self.keyset()
        credentials = self.credentials(token, keys)
        snapshot = self.pending_htlcs()
        backing = self._check_backing(token.proofs[0], snapshot)
        for proof in token.proofs:
            self._check_backing(proof, snapshot)
        amounts = denominations(token.amount) if amounts is None else amounts
        if sum(amounts) != token.amount:
            raise ClientError("swap outputs must preserve input value")
        prepared = [output(amount) for amount in amounts]
        blindings = [fresh_secret() for _ in amounts]
        commitments = [
            PublicKey(
                point=(keys[p.amount].Y_h1 * credentials[0].h).point + (G1 * t).point,
                group="G1",
            )
            for (p, _), t in zip(prepared, blindings)
        ]
        outputs = [
            BlindOutput(**p.model_dump(), b=b.format().hex())
            for (p, _), b in zip(prepared, commitments)
        ]
        nullifiers = [self.nullifier(proof) for proof in token.proofs]
        binding = swap_binding(
            [(p.amount, n) for p, n in zip(token.proofs, nullifiers)], outputs
        )
        presentations = [
            present_private(keys[p.amount], cred, binding=binding)
            for p, cred in zip(token.proofs, credentials)
        ]
        statements = common_h_statements(
            [
                (keys[p.amount], pres)
                for p, (pres, _) in zip(token.proofs, presentations)
            ],
            [(keys[p.amount], b) for p, b in zip(outputs, commitments)],
        )
        witnesses = [credentials[0].h] + [o for _, o in presentations] + blindings
        equality_proof = prove_linear(statements, witnesses, COMMON_H_DST, binding)
        return SwapOperation(
            id=os.urandom(32).hex(),
            token=token,
            secrets=[s for _, s in prepared],
            blindings=[f"{t:064x}" for t in blindings],
            request=SwapRequest(
                inputs=[
                    SwapInput(amount=p.amount, presentation=pres.to_bytes().hex())
                    for p, (pres, _) in zip(token.proofs, presentations)
                ],
                outputs=outputs,
                equality_proof=equality_proof.to_bytes().hex(),
            ),
            backing=backing,
        )

    def finish_swap(self, operation: SwapOperation) -> Token:
        response = SignaturesResponse.model_validate_json(
            self._post("/v1/swap", operation.request)
        )
        if [p.amount for p in response.signatures] != [
            p.amount for p in operation.request.outputs
        ]:
            raise ClientError("mint returned inconsistent swap outputs")
        keys = self.keyset()
        source = operation.token.proofs[0]
        return Token(
            mint=self.mint_url,
            proofs=[
                self._proof(
                    signature,
                    secret,
                    source.payment_hash,
                    source.backing_amount,
                    source.preimage,
                    keys,
                    blinding,
                )
                for signature, secret, blinding in zip(
                    response.signatures, operation.secrets, operation.blindings
                )
            ],
        )

    def melt_quote(
        self, request: str, fee_reserve: int | None = None
    ) -> MeltQuoteResponse:
        from .lightning import invoice_details

        response = MeltQuoteResponse.model_validate_json(
            self._post(
                "/v1/melt/quote/bolt11",
                MeltQuoteRequest(request=request, fee_reserve=fee_reserve),
            )
        )
        payout_hash, amount = invoice_details(request)
        if (
            response.request != request
            or response.payment_hash != payout_hash
            or response.amount != amount
            or (fee_reserve is not None and response.fee_reserve != fee_reserve)
        ):
            raise ClientError("mint returned an inconsistent melt quote")
        return response

    def prepare_melt(
        self,
        token: Token,
        quote: MeltQuoteResponse | str,
        fee_reserve: int | None = None,
    ) -> MeltOperation:
        if isinstance(quote, str):
            quote = self.melt_quote(quote, fee_reserve)
        keys = self.keyset()
        credentials = self.credentials(token, keys)
        if token.amount < quote.amount + quote.fee_reserve:
            raise ClientError("tokens do not cover invoice amount and fee reserve")
        self.check_backing(token)
        groups: dict[str, int] = {}
        for proof in token.proofs:
            if proof.preimage is None:
                raise ClientError("token has no backing HTLC preimage")
            groups[proof.payment_hash] = (
                groups.get(proof.payment_hash, 0) + proof.amount
            )
        prepared = [
            (payment_hash, output(1 << i))
            for payment_hash, amount in groups.items()
            for i in range(amount.bit_length())
        ]
        change = [
            ChangeOutput(**p.model_dump(), payment_hash=h) for h, (p, _) in prepared
        ]
        metadata = [
            (p.amount, p.backing_amount, p.preimage or "", self.nullifier(p))
            for p in token.proofs
        ]
        binding = melt_binding(quote, metadata, change)
        return MeltOperation(
            id=os.urandom(32).hex(),
            token=token,
            quote=quote,
            secrets=[secret for _, (_, secret) in prepared],
            request=MeltRequest(
                quote=quote.quote,
                inputs=[
                    MeltInput(
                        amount=p.amount,
                        backing_amount=p.backing_amount,
                        preimage=p.preimage or "",
                        presentation=present(cred, binding=binding).to_bytes().hex(),
                    )
                    for p, cred in zip(token.proofs, credentials)
                ],
                change=change,
            ),
        )

    def finish_melt(self, operation: MeltOperation) -> MeltResponse:
        response = MeltResponse.model_validate_json(
            self._post("/v1/melt/bolt11", operation.request)
        )
        quote = operation.quote
        if (
            response.amount != quote.amount
            or response.payout_hash != quote.payment_hash
            or preimage_hash(response.payout_preimage) != quote.payment_hash
            or response.fee != (response.fee_msat + 999) // 1000
            or response.fee > quote.fee_reserve
        ):
            raise ClientError("mint returned an invalid melt receipt")
        if (
            sum(p.amount for p in response.change) + response.expired_change
            != operation.token.amount - quote.amount - response.fee
        ):
            raise ClientError("mint returned inconsistent fee change")
        self.melt_change(operation, response)
        return response

    def melt_change(
        self, operation: MeltOperation, response: MeltResponse
    ) -> Token | None:
        keys = self.keyset()
        slots = {
            (p.payment_hash, p.amount): secret
            for p, secret in zip(operation.request.change, operation.secrets)
        }
        backing = {p.payment_hash: p for p in operation.token.proofs}
        proofs = []
        seen = set()
        cost = operation.quote.amount + response.fee
        expected = {}
        for payment_hash in dict.fromkeys(
            p.payment_hash for p in operation.token.proofs
        ):
            amount = sum(
                p.amount
                for p in operation.token.proofs
                if p.payment_hash == payment_hash
            )
            charged = min(cost, amount)
            cost -= charged
            expected[payment_hash] = amount - charged
        for signature in response.change:
            if signature.payment_hash is None:
                raise ClientError("mint omitted the backing hash from change")
            slot = (signature.payment_hash, signature.amount)
            if slot not in slots or slot in seen:
                raise ClientError("mint returned unexpected or duplicate change")
            seen.add(slot)
            source = backing[signature.payment_hash]
            proofs.append(
                self._proof(
                    signature,
                    slots[slot],
                    source.payment_hash,
                    source.backing_amount,
                    source.preimage,
                    keys,
                )
            )
        for payment_hash, amount in expected.items():
            returned = sum(p.amount for p in proofs if p.payment_hash == payment_hash)
            if returned not in (0, amount):
                raise ClientError("mint changed the allocation of HTLC change")
        return Token(mint=self.mint_url, proofs=proofs) if proofs else None

    def check(self, nullifiers: list[str]) -> CheckResponse:
        return CheckResponse.model_validate_json(
            self._post("/v1/checkstate", CheckRequest(nullifiers=nullifiers))
        )

    @staticmethod
    def nullifier(proof: TokenProof) -> str:
        credential = Credential.from_bytes(bytes.fromhex(proof.credential))
        return (G_NULL * credential.s).format().hex()

    def close(self):
        self.http.close()
