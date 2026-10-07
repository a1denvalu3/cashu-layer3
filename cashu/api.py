import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .db import BackendError, MintError
from .mint import Mint
from .models import (
    BurnRequest,
    BurnResponse,
    CheckRequest,
    CheckResponse,
    Hex32,
    KeysetResponse,
    MintRequest,
    QuoteRequest,
    QuoteResponse,
    SignatureResponse,
    SwapRequest,
)


def create_app(mint: Mint, recovery: bool = True) -> FastAPI:
    async def reconcile() -> None:
        while True:
            await mint.resume_pending_burns()
            await asyncio.sleep(5)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(reconcile()) if recovery else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await mint.backend.close()

    app = FastAPI(title="Cashu", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(MintError)
    async def mint_error(request: Request, exc: MintError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    @app.exception_handler(BackendError)
    async def backend_error(request: Request, exc: BackendError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(ValueError)
    async def invalid_encoding(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(
            {"detail": "invalid cryptographic encoding or invoice"}, status_code=400
        )

    @app.get("/v1/info")
    async def info() -> dict:
        return {
            "name": "Cashu",
            "unit": "sat",
            "scheme": "PS/BLS12-381",
            "token_version": 2,
            "signed_attribute": "payment-hash-and-whole-amount",
            "issuance": "accepted-hold-invoice",
            "private_swap": True,
            "swap": "one-token-to-one-token",
            "swap_request_version": 2,
            "swap_rounds": 1,
            "redemption": "whole-amount-bolt11-with-backing-preimage",
        }

    @app.get("/v1/keys", response_model=KeysetResponse)
    async def keys() -> KeysetResponse:
        return mint.keyset()

    @app.post("/v1/mint/quote/bolt11", response_model=QuoteResponse)
    async def quote(request: QuoteRequest) -> QuoteResponse:
        return await mint.quote(request)

    @app.get("/v1/mint/quote/bolt11/{quote_id}", response_model=QuoteResponse)
    async def get_quote(quote_id: Hex32) -> QuoteResponse:
        return await mint.get_quote(quote_id)

    @app.post("/v1/mint/bolt11", response_model=SignatureResponse)
    async def issue(request: MintRequest) -> SignatureResponse:
        return await mint.mint(request)

    @app.post("/v1/swap", response_model=SignatureResponse)
    async def swap(request: SwapRequest) -> SignatureResponse:
        return mint.swap(request)

    @app.post("/v1/checkstate", response_model=CheckResponse)
    async def check(request: CheckRequest) -> CheckResponse:
        return mint.check(request.nullifiers)

    @app.post("/v1/burn", response_model=BurnResponse)
    async def burn(request: BurnRequest) -> BurnResponse:
        return await mint.burn(request)

    return app
