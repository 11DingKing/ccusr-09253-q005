"""复核案件 API：检测、认领、补证、裁决、复开、合并与拆分。

所有接口要求 X-Actor-Id / X-Actor-Role（reviewer 或 admin）请求头；
领域错误由 main.py 中注册的异常处理器映射为 HTTP 状态码。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from . import case_services
from .case_services import KNOWN_ROLES, Actor
from .db import get_db
from .review_cases import CaseState
from .schemas import (
    CaseAdjudicateIn,
    CaseDetectIn,
    CaseDetectOut,
    CaseEvidenceIn,
    CaseMergeIn,
    CaseMergeOut,
    CaseOut,
    CaseReasonIn,
    CaseSplitIn,
    CaseSplitOut,
)

router = APIRouter(prefix="/api/plans/{plan_version}/cases", tags=["review-cases"])


def require_actor(
    x_actor_id: str | None = Header(default=None),
    x_actor_role: str | None = Header(default=None),
) -> Actor:
    """解析并校验操作者身份头，缺失或非法时返回 401。"""
    if x_actor_id is None or not x_actor_id.strip():
        raise HTTPException(status_code=401, detail="X-Actor-Id header is required")
    role = (x_actor_role or "").strip()
    if role not in KNOWN_ROLES:
        raise HTTPException(
            status_code=401,
            detail="X-Actor-Role header must be 'reviewer' or 'admin'",
        )
    return Actor(actor_id=x_actor_id.strip(), role=role)


@router.post("/detect", response_model=CaseDetectOut)
def detect_cases(
    plan_version: str,
    body: CaseDetectIn | None = None,
    db: Session = Depends(get_db),
    actor: Actor = Depends(require_actor),
) -> Any:
    return case_services.detect_cases(
        db,
        plan_version=plan_version,
        actor=actor,
        overlong_threshold_seconds=(
            body.overlong_threshold_seconds if body is not None else None
        ),
    )


@router.get("", response_model=list[CaseOut])
def list_cases(
    plan_version: str,
    student_id: str | None = Query(default=None),
    state: str | None = Query(default=None),
    db: Session = Depends(get_db),
    actor: Actor = Depends(require_actor),
) -> Any:
    if state is not None and state not in {s.value for s in CaseState}:
        raise HTTPException(status_code=400, detail=f"unknown case state '{state}'")
    return case_services.list_cases(
        db, plan_version, student_id=student_id, state=state
    )


@router.get("/{case_id}", response_model=CaseOut)
def get_case(
    plan_version: str,
    case_id: str,
    db: Session = Depends(get_db),
    actor: Actor = Depends(require_actor),
) -> Any:
    return case_services.get_case(db, plan_version, case_id)


@router.post("/{case_id}/claim", response_model=CaseOut)
def claim_case(
    plan_version: str,
    case_id: str,
    db: Session = Depends(get_db),
    actor: Actor = Depends(require_actor),
) -> Any:
    return case_services.claim_case(
        db, plan_version=plan_version, case_id=case_id, actor=actor
    )


@router.post("/{case_id}/evidence", response_model=CaseOut)
def add_evidence(
    plan_version: str,
    case_id: str,
    body: CaseEvidenceIn,
    db: Session = Depends(get_db),
    actor: Actor = Depends(require_actor),
) -> Any:
    return case_services.add_evidence(
        db,
        plan_version=plan_version,
        case_id=case_id,
        actor=actor,
        note=body.note,
        attachments=body.attachments,
    )


@router.post("/{case_id}/adjudicate", response_model=CaseOut)
def adjudicate_case(
    plan_version: str,
    case_id: str,
    body: CaseAdjudicateIn,
    db: Session = Depends(get_db),
    actor: Actor = Depends(require_actor),
) -> Any:
    return case_services.adjudicate_case(
        db,
        plan_version=plan_version,
        case_id=case_id,
        actor=actor,
        verdict=body.verdict,
        reason=body.reason,
        correction_seconds=body.correction_seconds,
    )


@router.post("/{case_id}/reopen", response_model=CaseOut)
def reopen_case(
    plan_version: str,
    case_id: str,
    body: CaseReasonIn,
    db: Session = Depends(get_db),
    actor: Actor = Depends(require_actor),
) -> Any:
    return case_services.reopen_case(
        db,
        plan_version=plan_version,
        case_id=case_id,
        actor=actor,
        reason=body.reason,
    )


@router.post("/{case_id}/merge", response_model=CaseMergeOut)
def merge_case(
    plan_version: str,
    case_id: str,
    body: CaseMergeIn,
    db: Session = Depends(get_db),
    actor: Actor = Depends(require_actor),
) -> Any:
    return case_services.merge_cases(
        db,
        plan_version=plan_version,
        source_case_id=case_id,
        target_case_id=body.target_case_id,
        actor=actor,
        reason=body.reason,
    )


@router.post("/{case_id}/split", response_model=CaseSplitOut)
def split_case(
    plan_version: str,
    case_id: str,
    body: CaseSplitIn,
    db: Session = Depends(get_db),
    actor: Actor = Depends(require_actor),
) -> Any:
    return case_services.split_case(
        db,
        plan_version=plan_version,
        case_id=case_id,
        actor=actor,
        reason=body.reason,
        source_event_ids=body.source_event_ids,
    )
