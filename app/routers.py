"""服务端业务模块。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from . import services
from .db import get_db
from .schemas import (
    AdjudicateIn,
    CaseOut,
    DetectCasesIn,
    DetectResultOut,
    DiffOut,
    EventBatchIn,
    EvidenceIn,
    FreezeIn,
    ImportResult,
    MergeCasesIn,
    PlanIn,
    PlanOut,
    ReopenIn,
    SnapshotOut,
    SplitCaseIn,
    SplitResultOut,
    StudentProgressOut,
)

router = APIRouter(prefix="/api")


def _require_actor(
    x_actor_id: Annotated[str | None, Header()] = None,
    x_actor_role: Annotated[str | None, Header()] = None,
) -> tuple[str, str]:
    actor_id = (x_actor_id or "").strip()
    if not actor_id:
        raise HTTPException(status_code=400, detail="X-Actor-Id header is required")
    role = (x_actor_role or "reviewer").strip().lower()
    if role not in {"reviewer", "admin"}:
        raise HTTPException(
            status_code=400, detail="X-Actor-Role must be 'reviewer' or 'admin'"
        )
    return actor_id, role


def _optional_actor(
    x_actor_id: Annotated[str | None, Header()] = None,
) -> str:
    return (x_actor_id or "").strip() or "system"


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
def create_plan(body: PlanIn, db: Session = Depends(get_db)) -> Any:
    return services.ensure_plan(
        db,
        plan_version=body.plan_version,
        iana_timezone=body.iana_timezone,
        required_seconds=body.required_seconds,
    )


@router.get("/plans/{plan_version}", response_model=PlanOut)
def read_plan(plan_version: str, db: Session = Depends(get_db)) -> Any:
    plan = services.get_plan_plain(db, plan_version)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


@router.post(
    "/plans/{plan_version}/events",
    response_model=ImportResult,
    status_code=status.HTTP_201_CREATED,
)
def post_events(
    plan_version: str, body: EventBatchIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.import_events(
            db,
            plan_version=plan_version,
            events=[e.model_dump() for e in body.events],
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/snapshot",
    response_model=SnapshotOut,
)
def get_snapshot(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        snap = services.current_snapshot(db, plan_version)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/students/{student_id}/progress",
    response_model=StudentProgressOut,
)
def get_progress(
    plan_version: str, student_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        result = services.student_progress(db, plan_version, student_id)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.post(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
    status_code=status.HTTP_201_CREATED,
)
def post_freeze(
    plan_version: str,
    freeze_id: str,
    body: FreezeIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        snap, _ = services.freeze_semester(
            db, plan_version=plan_version, freeze_id=freeze_id
        )
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
)
def get_freeze(
    plan_version: str, freeze_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        snap = services.get_frozen_snapshot(db, plan_version, freeze_id)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.FreezeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/explain/{student_id}",
    response_model=StudentProgressOut,
)
def explain_freeze_student(
    plan_version: str,
    freeze_id: str,
    student_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        result = services.explain_frozen_student(
            db, plan_version, freeze_id, student_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/diff/{other_freeze_id}",
    response_model=DiffOut,
)
def get_diff(
    plan_version: str,
    freeze_id: str,
    other_freeze_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.diff_freezes(
            db, plan_version, freeze_id, other_freeze_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 异常学时复核案件
# ---------------------------------------------------------------------------


@router.post(
    "/plans/{plan_version}/cases/detect",
    response_model=DetectResultOut,
    status_code=status.HTTP_201_CREATED,
)
def detect_cases(
    plan_version: str,
    body: DetectCasesIn,
    db: Session = Depends(get_db),
    actor_id: str = Depends(_optional_actor),
) -> Any:
    return services.detect_cases(
        db,
        plan_version=plan_version,
        max_checkin_seconds=body.max_checkin_seconds,
        rules=body.rules,
        actor_id=actor_id,
    )


@router.post(
    "/plans/{plan_version}/cases/merge",
    response_model=CaseOut,
    status_code=status.HTTP_201_CREATED,
)
def merge_cases(
    plan_version: str,
    body: MergeCasesIn,
    db: Session = Depends(get_db),
    actor: tuple[str, str] = Depends(_require_actor),
) -> Any:
    actor_id, actor_role = actor
    return services.merge_cases(
        db,
        plan_version=plan_version,
        case_ids=body.case_ids,
        reason=body.reason,
        actor_id=actor_id,
        actor_role=actor_role,
    )


@router.get("/plans/{plan_version}/cases", response_model=list[CaseOut])
def list_cases(
    plan_version: str,
    state: str | None = None,
    student_id: str | None = None,
    db: Session = Depends(get_db),
) -> Any:
    return services.list_case_dicts(
        db, plan_version, state=state, student_id=student_id
    )


@router.get("/plans/{plan_version}/cases/{case_id}", response_model=CaseOut)
def get_case(
    plan_version: str, case_id: str, db: Session = Depends(get_db)
) -> Any:
    return services.get_case_detail(db, plan_version, case_id)


@router.post("/plans/{plan_version}/cases/{case_id}/claim", response_model=CaseOut)
def claim_case(
    plan_version: str,
    case_id: str,
    db: Session = Depends(get_db),
    actor: tuple[str, str] = Depends(_require_actor),
) -> Any:
    actor_id, actor_role = actor
    return services.claim_case(
        db,
        plan_version=plan_version,
        case_id=case_id,
        actor_id=actor_id,
        actor_role=actor_role,
    )


@router.post("/plans/{plan_version}/cases/{case_id}/evidence", response_model=CaseOut)
def supplement_evidence(
    plan_version: str,
    case_id: str,
    body: EvidenceIn,
    db: Session = Depends(get_db),
    actor: tuple[str, str] = Depends(_require_actor),
) -> Any:
    actor_id, actor_role = actor
    return services.supplement_evidence(
        db,
        plan_version=plan_version,
        case_id=case_id,
        note=body.note,
        attachments=body.attachments,
        actor_id=actor_id,
        actor_role=actor_role,
    )


@router.post("/plans/{plan_version}/cases/{case_id}/adjudicate", response_model=CaseOut)
def adjudicate_case(
    plan_version: str,
    case_id: str,
    body: AdjudicateIn,
    db: Session = Depends(get_db),
    actor: tuple[str, str] = Depends(_require_actor),
) -> Any:
    actor_id, actor_role = actor
    correction = body.correction.model_dump() if body.correction else None
    return services.adjudicate_case(
        db,
        plan_version=plan_version,
        case_id=case_id,
        verdict=body.verdict,
        reason=body.reason,
        correction=correction,
        actor_id=actor_id,
        actor_role=actor_role,
    )


@router.post("/plans/{plan_version}/cases/{case_id}/reopen", response_model=CaseOut)
def reopen_case(
    plan_version: str,
    case_id: str,
    body: ReopenIn,
    db: Session = Depends(get_db),
    actor: tuple[str, str] = Depends(_require_actor),
) -> Any:
    actor_id, actor_role = actor
    return services.reopen_case(
        db,
        plan_version=plan_version,
        case_id=case_id,
        reason=body.reason,
        actor_id=actor_id,
        actor_role=actor_role,
    )


@router.post(
    "/plans/{plan_version}/cases/{case_id}/split",
    response_model=SplitResultOut,
    status_code=status.HTTP_201_CREATED,
)
def split_case(
    plan_version: str,
    case_id: str,
    body: SplitCaseIn,
    db: Session = Depends(get_db),
    actor: tuple[str, str] = Depends(_require_actor),
) -> Any:
    actor_id, actor_role = actor
    return services.split_case(
        db,
        plan_version=plan_version,
        case_id=case_id,
        groups=[group.model_dump() for group in body.groups],
        reason=body.reason,
        actor_id=actor_id,
        actor_role=actor_role,
    )
