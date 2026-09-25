"""服务端业务模块。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import services
from .compliance.anomaly_cases import (
    CaseConcurrencyError,
    CaseError,
    CasePermissionError,
    CaseStateError,
)
from .routers import router

app = FastAPI(
    title="Practice Hours Guard",
    version="0.1.0",
    description=(
        "Event-sourced practice-hours compliance service. Check-ins, mentor "
        "confirmations and leave corrections are append-only; compliance is "
        "derived by replay and can be frozen into an immutable snapshot."
    ),
)

app.include_router(router)


@app.exception_handler(services.PlanNotFoundError)
async def plan_not_found_handler(
    request: Request, exc: services.PlanNotFoundError
) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(services.CaseNotFoundError)
async def case_not_found_handler(
    request: Request, exc: services.CaseNotFoundError
) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(CasePermissionError)
async def case_permission_handler(
    request: Request, exc: CasePermissionError
) -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.exception_handler(CaseStateError)
async def case_state_handler(request: Request, exc: CaseStateError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(CaseConcurrencyError)
async def case_concurrency_handler(
    request: Request, exc: CaseConcurrencyError
) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(CaseError)
async def case_domain_handler(request: Request, exc: CaseError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
