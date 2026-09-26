"""服务端业务模块。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import case_services, services
from .case_routers import router as case_router
from .routers import router

app = FastAPI(
    title="Practice Hours Guard",
    version="0.1.0",
    description=(
        "Event-sourced practice-hours compliance service. Check-ins, mentor "
        "confirmations and leave corrections are append-only; compliance is "
        "derived by replay and can be frozen into an immutable snapshot. "
        "Anomalies (negative corrections, over-long check-ins, overlapping "
        "activities) are reviewed through a case workflow before any "
        "correction event is applied."
    ),
)

app.include_router(router)
app.include_router(case_router)


@app.exception_handler(case_services.CaseError)
async def handle_case_error(
    request: Request, exc: case_services.CaseError
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code, content={"detail": str(exc)}
    )


@app.exception_handler(services.PlanNotFoundError)
async def handle_plan_not_found(
    request: Request, exc: services.PlanNotFoundError
) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
