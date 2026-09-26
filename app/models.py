"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Plan(Base):
    __tablename__ = "plans"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    iana_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    required_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint("required_seconds >= 0", name="ck_plans_required_nonneg"),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("event_id", "plan_version", name="uq_events_event_id_plan"),
        Index("ix_events_plan_student", "plan_version", "student_id"),
    )


class Freeze(Base):
    __tablename__ = "freezes"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    freeze_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    event_cutoff_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class ReviewCase(Base):
    """异常学时复核案件：规则、证据、处理人、修正事件与血缘的持久化载体。"""

    __tablename__ = "review_cases"

    case_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    rule: Mapped[str] = mapped_column(String(40), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    fingerprints: Mapped[list] = mapped_column(JSON, nullable=False)
    source_event_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    lineage: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    child_case_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    merged_into: Mapped[str | None] = mapped_column(String(64), nullable=True)
    assignee_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    suggested_correction_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    applied_correction_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    adjudication_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    correction_event_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    resolution: Mapped[str | None] = mapped_column(String(16), nullable=True)
    resolution_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    evidence: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    history: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_review_cases_version_pos"),
        Index("ix_review_cases_plan_student", "plan_version", "student_id"),
    )
