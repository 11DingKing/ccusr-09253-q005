"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .compliance.anomaly_cases import DEFAULT_MAX_CHECKIN_SECONDS


class PlanIn(BaseModel):
    plan_version: str = Field(..., min_length=1, max_length=128)
    iana_timezone: str = Field(..., min_length=1, max_length=64)
    required_seconds: int = Field(0, ge=0)


class PlanOut(BaseModel):
    plan_version: str
    iana_timezone: str
    required_seconds: int


class CheckinPayload(BaseModel):
    activity_id: str = ""
    activity_type: str = "regular"
    check_in_at: datetime
    check_out_at: datetime

    @model_validator(mode="after")
    def _check_order(self) -> "CheckinPayload":
        if self.check_out_at <= self.check_in_at:
            raise ValueError("check_out_at must be after check_in_at")
        return self

    @field_validator("check_in_at", "check_out_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v


class MentorConfirmPayload(BaseModel):
    checkin_event_id: str


class LeaveCorrectionPayload(BaseModel):
    adjustment_seconds: int
    reason: str = ""


class EventIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    event_type: Literal["checkin", "mentor_confirm", "leave_correction"]
    student_id: str = Field(..., min_length=1, max_length=128)
    payload: dict[str, Any]


class EventBatchIn(BaseModel):
    events: list[EventIn]


class EventOut(BaseModel):
    event_id: str
    plan_version: str
    event_type: str
    student_id: str
    payload: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ImportResult(BaseModel):
    accepted: int
    duplicates: list[str]
    rejected: list[dict[str, Any]]


class DailyTotal(BaseModel):
    academic_day: str
    seconds: int


class CheckinExplanation(BaseModel):
    event_id: str
    activity_id: str
    activity_type: str
    status: str
    counts: bool
    check_in_at_utc: str
    check_out_at_utc: str
    raw_seconds: int
    academic_days: list[dict[str, Any]]


class AdjustmentOut(BaseModel):
    event_id: str
    seconds: int
    reason: str


class HeldAdjustmentOut(BaseModel):
    event_id: str
    seconds: int
    reason: str
    case_id: str | None = None


class StudentProgressOut(BaseModel):
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    daily: list[DailyTotal]
    checkins: list[CheckinExplanation]
    adjustments: list[AdjustmentOut]
    held_adjustments: list[HeldAdjustmentOut] = []
    open_cases: list[dict[str, Any]] = []
    pending_review_seconds: int = 0


class SnapshotOut(BaseModel):
    plan_version: str
    freeze_id: str | None
    timezone: str
    required_seconds: int
    generated_at: str
    event_cutoff_id: str | None
    students: list[dict[str, Any]]


class FreezeIn(BaseModel):
    pass


class DiffOut(BaseModel):
    plan_version: str
    old_freeze_id: str | None
    new_freeze_id: str | None
    old_generated_at: str
    new_generated_at: str
    old_event_cutoff_id: str | None
    new_event_cutoff_id: str | None
    student_changes: list[dict[str, Any]]
    students_affected: int


class DetectCasesIn(BaseModel):
    max_checkin_seconds: int = Field(DEFAULT_MAX_CHECKIN_SECONDS, gt=0)
    rules: (
        list[
            Literal[
                "negative_adjustment", "oversized_checkin", "overlapping_activity"
            ]
        ]
        | None
    ) = None


class CaseOut(BaseModel):
    case_id: str
    plan_version: str
    student_id: str
    rule: str
    state: str
    assignee_id: str | None
    dedup_key: str
    disputed_seconds: int
    source_event_ids: list[str]
    absorbed_event_ids: list[str]
    resolution_event_id: str | None
    verdict: str | None
    evidence: list[dict[str, Any]]
    audit: list[dict[str, Any]]
    lineage: dict[str, list[str]]
    version: int
    created_at: str | None
    updated_at: str | None


class DetectResultOut(BaseModel):
    created: list[CaseOut]
    existing: list[CaseOut]
    scanned_events: int


class EvidenceIn(BaseModel):
    note: str = Field(..., min_length=1, max_length=2000)
    attachments: list[str] = Field(default_factory=list, max_length=20)


class CorrectionIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    adjustment_seconds: int
    reason: str = ""

    @field_validator("adjustment_seconds")
    @classmethod
    def _nonzero(cls, v: int) -> int:
        if v == 0:
            raise ValueError("adjustment_seconds must be non-zero")
        return v


class AdjudicateIn(BaseModel):
    verdict: Literal["confirmed", "dismissed"]
    reason: str = Field(..., min_length=1, max_length=2000)
    correction: CorrectionIn | None = None


class ReopenIn(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)


class MergeCasesIn(BaseModel):
    case_ids: list[str] = Field(..., min_length=2)
    reason: str = Field(..., min_length=1, max_length=2000)


class SplitGroupIn(BaseModel):
    source_event_ids: list[str] = Field(..., min_length=1)
    disputed_seconds: int = Field(..., ge=0)


class SplitCaseIn(BaseModel):
    groups: list[SplitGroupIn] = Field(..., min_length=2)
    reason: str = Field(..., min_length=1, max_length=2000)


class SplitResultOut(BaseModel):
    parent: CaseOut
    children: list[CaseOut]
