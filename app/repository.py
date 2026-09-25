"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.replay import Event as CoreEvent
from .core.replay import EventType
from .models import AnomalyCase, Event as EventModel
from .models import Freeze, Plan


def get_plan(db: Session, plan_version: str) -> Plan | None:
    return db.get(Plan, plan_version)


def upsert_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> Plan:
    stmt = sqlite_insert(Plan).values(
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version"],
        set_={
            "iana_timezone": iana_timezone,
            "required_seconds": required_seconds,
        },
    )
    db.execute(stmt)
    db.commit()
    plan = db.get(Plan, plan_version)
    assert plan is not None
    return plan


def _to_core_event(row: EventModel) -> CoreEvent:
    return CoreEvent(
        event_id=row.event_id,
        plan_version=row.plan_version,
        event_type=EventType(row.event_type),
        student_id=row.student_id,
        payload=dict(row.payload),
        created_at=row.created_at,
    )


def insert_events(
    db: Session,
    *,
    plan_version: str,
    events: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """执行确定性的业务处理。"""
    accepted: list[str] = []
    duplicates: list[str] = []
    for e in events:
        stmt = sqlite_insert(EventModel).values(
            event_id=e["event_id"],
            plan_version=plan_version,
            student_id=e["student_id"],
            event_type=e["event_type"],
            payload=e["payload"],
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["event_id", "plan_version"]
        ).returning(EventModel.id)
        inserted_id = db.execute(stmt).scalar_one_or_none()
        if inserted_id is not None:
            accepted.append(e["event_id"])
        else:
            duplicates.append(e["event_id"])
    db.commit()
    return accepted, duplicates


def get_event(db: Session, plan_version: str, event_id: str) -> EventModel | None:
    stmt = select(EventModel).where(
        EventModel.plan_version == plan_version,
        EventModel.event_id == event_id,
    )
    return db.execute(stmt).scalar_one_or_none()


def load_events(db: Session, plan_version: str) -> list[CoreEvent]:
    stmt = select(EventModel).where(EventModel.plan_version == plan_version)
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def load_events_up_to(
    db: Session, plan_version: str, max_event_id: str
) -> list[CoreEvent]:
    """执行确定性的业务处理。"""
    stmt = (
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .where(EventModel.event_id <= max_event_id)
    )
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def max_event_id(db: Session, plan_version: str) -> str | None:
    stmt = (
        select(EventModel.event_id)
        .where(EventModel.plan_version == plan_version)
        .order_by(EventModel.event_id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def get_freeze(
    db: Session, plan_version: str, freeze_id: str
) -> Freeze | None:
    return db.get(Freeze, (plan_version, freeze_id))


def insert_freeze(
    db: Session,
    *,
    plan_version: str,
    freeze_id: str,
    snapshot: dict[str, Any],
    event_cutoff_id: str | None,
) -> Freeze | None:
    """执行确定性的业务处理。"""
    stmt = sqlite_insert(Freeze).values(
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snapshot,
        event_cutoff_id=event_cutoff_id,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "freeze_id"]
    ).returning(Freeze.plan_version)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return db.get(Freeze, (plan_version, freeze_id))
    return None


def insert_case(db: Session, *, values: dict[str, Any]) -> AnomalyCase | None:
    """插入复核案件；命中主键或去重键约束时返回 None（幂等）。"""
    stmt = sqlite_insert(AnomalyCase).values(**values)
    stmt = stmt.on_conflict_do_nothing().returning(AnomalyCase.case_id)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is None:
        return None
    return get_case(db, values["plan_version"], values["case_id"])


def get_case(db: Session, plan_version: str, case_id: str) -> AnomalyCase | None:
    return db.get(AnomalyCase, (plan_version, case_id))


def list_cases(
    db: Session,
    plan_version: str,
    *,
    state: str | None = None,
    student_id: str | None = None,
) -> list[AnomalyCase]:
    stmt = select(AnomalyCase).where(AnomalyCase.plan_version == plan_version)
    if state is not None:
        stmt = stmt.where(AnomalyCase.state == state)
    if student_id is not None:
        stmt = stmt.where(AnomalyCase.student_id == student_id)
    stmt = stmt.order_by(AnomalyCase.case_id)
    return list(db.execute(stmt).scalars().all())


def update_case_fields(
    db: Session,
    plan_version: str,
    case_id: str,
    *,
    expected_version: int,
    changes: dict[str, Any],
) -> bool:
    """基于乐观锁版本号的条件更新，返回是否命中。"""
    stmt = (
        update(AnomalyCase)
        .where(AnomalyCase.plan_version == plan_version)
        .where(AnomalyCase.case_id == case_id)
        .where(AnomalyCase.version == expected_version)
        .values(
            **changes,
            version=expected_version + 1,
            updated_at=datetime.now(timezone.utc),
        )
    )
    result = db.execute(stmt)
    db.commit()
    return result.rowcount == 1


def conditional_transition(
    db: Session,
    plan_version: str,
    case_id: str,
    *,
    from_states: list[str],
    values: dict[str, Any],
) -> AnomalyCase | None:
    """仅当案件处于指定来源状态时才执行变更（并发认领只会有一个获胜者）。"""
    stmt = (
        update(AnomalyCase)
        .where(AnomalyCase.plan_version == plan_version)
        .where(AnomalyCase.case_id == case_id)
        .where(AnomalyCase.state.in_(list(from_states)))
        .values(
            **values,
            version=AnomalyCase.version + 1,
            updated_at=datetime.now(timezone.utc),
        )
    )
    result = db.execute(stmt)
    db.commit()
    if result.rowcount != 1:
        return None
    return get_case(db, plan_version, case_id)
