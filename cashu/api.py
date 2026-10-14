import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .db import BackendError, MintError
from .mint import Mint
from .models import (
    CheckRequest,
    CheckResponse,
    Hex32,
    KeysetResponse,
    MeltQuoteRequest,
    MeltQuoteResponse,
    MeltRequest,
    MeltResponse,
    MintRequest,
    OperatorFundingRequest,
    OperatorFundingResponse,
    PendingHTLCsResponse,
    QuoteRequest,
    QuoteResponse,
    SignaturesResponse,
    SwapRequest,
)


def create_app(
    mint: Mint,
    recovery: bool = True,
    report_balances: bool = False,
    admin_token: str | None = None,
) -> FastAPI:
    async def reconcile():
        while True:
            try:
                await mint.resume_pending_melts()
            except BackendError:
                pass
            await asyncio.sleep(5)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(reconcile()) if recovery else None
        try:
            if report_balances:
                logger = logging.getLogger("uvicorn.error")
                backend = mint.backend
                network = getattr(backend, "network", None)
                label = backend.name + (f", {network}" if network else "")
                logger.info("Backend balances (%s):", label)
                try:
                    balances = await asyncio.wait_for(
                        backend.get_balances(), timeout=15
                    )
                except Exception:
                    # Provider errors can contain API keys or wallet secrets.
                    logger.warning("  Balance unavailable; continuing mint startup")
                else:
                    if balances is None:
                        logger.info("  No real wallet balance (simulated backend)")
                    for name, amount in (balances or {}).items():
                        logger.info("  %s: %s sats", name, f"{amount:,}")
            yield
        finally:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await mint.backend.close()

    app = FastAPI(title="cashu3", version="0.2.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.exception_handler(MintError)
    async def mint_error(request: Request, exc: MintError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    @app.exception_handler(BackendError)
    async def backend_error(request: Request, exc: BackendError):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(ValueError)
    async def invalid_encoding(request: Request, exc: ValueError):
        return JSONResponse(
            {"detail": "invalid cryptographic encoding or invoice"}, status_code=400
        )

    bearer = HTTPBearer(auto_error=False)

    async def authorize_operator(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> None:
        if not admin_token:
            raise HTTPException(status_code=404, detail="Not Found")
        if credentials is None or not secrets.compare_digest(
            credentials.credentials.encode(), admin_token.encode()
        ):
            raise HTTPException(
                status_code=401,
                detail="Invalid operator credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.post(
        "/v1/admin/fund",
        response_model=OperatorFundingResponse,
        include_in_schema=False,
        dependencies=[Depends(authorize_operator)],
    )
    async def fund_mint(request: OperatorFundingRequest, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return await mint.backend.create_funding_invoice(request.amount, request.expiry)

    @app.get("/v1/info")
    async def info():
        cltv_expiry = getattr(mint.backend, "cltv_expiry", 0)
        funding_cltv = cltv_expiry or getattr(
            mint.backend, "funding_htlc_cltv_delta", None
        )
        token_lifetime = (
            cltv_expiry
            - getattr(mint.backend, "hold_expiry_delta", 0)
            - mint.settle_margin_blocks
            if cltv_expiry
            else None
        )
        return {
            "name": "cashu3",
            "unit": "sat",
            "scheme": "PS/BLS12-381",
            "token_version": 3,
            "swap_request_version": 3,
            "swap_rounds": 1,
            "private_swap": True,
            "swap": "same-htlc-many-to-many",
            "redemption": "combined-htlc-melt-with-fee-change",
            "expiry": "tokens-expire-with-backing-htlc",
            "pending_htlcs": "/v1/htlcs/pending",
            "settle_margin_blocks": mint.settle_margin_blocks,
            "settle_margin_seconds": mint.settle_margin_seconds,
            "default_token_lifetime_blocks": token_lifetime,
            "funding_htlc_cltv_delta": funding_cltv,
        }

    @app.get("/v1/keys", response_model=KeysetResponse)
    async def keys():
        return mint.keyset()

    @app.get("/v1/htlcs/pending", response_model=PendingHTLCsResponse)
    async def pending_htlcs(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return await mint.pending_htlcs()

    @app.post("/v1/mint/quote/bolt11", response_model=QuoteResponse)
    async def quote(request: QuoteRequest):
        return await mint.quote(request)

    @app.get("/v1/mint/quote/bolt11/{quote_id}", response_model=QuoteResponse)
    async def get_quote(quote_id: Hex32):
        return await mint.get_quote(quote_id)

    @app.post("/v1/mint/bolt11", response_model=SignaturesResponse)
    async def issue(request: MintRequest):
        return await mint.mint(request)

    @app.post("/v1/swap", response_model=SignaturesResponse)
    async def swap(request: SwapRequest):
        return mint.swap(request)

    @app.post("/v1/checkstate", response_model=CheckResponse)
    async def check(request: CheckRequest):
        return mint.check(request.nullifiers)

    @app.post("/v1/melt/quote/bolt11", response_model=MeltQuoteResponse)
    async def melt_quote(request: MeltQuoteRequest):
        return await mint.melt_quote(request)

    @app.post("/v1/melt/bolt11", response_model=MeltResponse)
    async def melt(request: MeltRequest):
        return await mint.melt(request)

    return app
