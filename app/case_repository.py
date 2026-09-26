"""复核案件的持久化与并发控制。

所有写路径使用「条件更新 + 版本号」的乐观锁：
UPDATE ... WHERE case_id = ? AND version = ?（认领另加状态约束），
影响行数为 0 时回滚并返回 False，由服务层转换为冲突错误。
"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .models import Event as EventModel
from .models import ReviewCase
from .review_cases import OPEN_STATES, CaseState


def get_case(db: Session, case_id: str) -> ReviewCase | None:
    return db.get(ReviewCase, case_id)


def list_cases(
    db: Session,
    plan_version: str,
    *,
    student_id: str | None = None,
    state: str | None = None,
) -> list[ReviewCase]:
    stmt = select(ReviewCase).where(ReviewCase.plan_version == plan_version)
    if student_id is not None:
        stmt = stmt.where(ReviewCase.student_id == student_id)
    if state is not None:
        stmt = stmt.where(ReviewCase.state == state)
    stmt = stmt.order_by(ReviewCase.case_id)
    return list(db.execute(stmt).scalars().all())


def fingerprint_index(db: Session, plan_version: str) -> dict[str, str]:
    """计划内已被案件覆盖的异常指纹 -> case_id，用于立案去重。

    合并后源与目标案件都覆盖同一指纹；优先指向未合并（当前持有）的案件，
    让检测结果引用到仍可操作的案件上。
    """
    stmt = select(
        ReviewCase.fingerprints, ReviewCase.case_id, ReviewCase.state
    ).where(ReviewCase.plan_version == plan_version)
    rows = db.execute(stmt).all()
    rows.sort(key=lambda row: (row[2] == CaseState.MERGED.value, row[1]))
    index: dict[str, str] = {}
    for fingerprints, case_id, _state in rows:
        for fingerprint in fingerprints:
            index.setdefault(fingerprint, case_id)
    return index


def insert_cases(db: Session, rows: list[dict[str, Any]]) -> list[str]:
    """批量立案；case_id 冲突（并发或重复检测）时静默跳过。"""
    created: list[str] = []
    for row in rows:
        stmt = (
            sqlite_insert(ReviewCase)
            .values(**row)
            .on_conflict_do_nothing(index_elements=["case_id"])
            .returning(ReviewCase.case_id)
        )
        inserted = db.execute(stmt).scalar_one_or_none()
        if inserted is not None:
            created.append(row["case_id"])
    db.commit()
    return created


def update_case(
    db: Session,
    *,
    case_id: str,
    expected_version: int,
    values: dict[str, Any],
    required_states: Iterable[str] | None = None,
) -> bool:
    """乐观锁条件更新；版本或状态不满足时回滚并返回 False。"""
    stmt = (
        update(ReviewCase)
        .where(ReviewCase.case_id == case_id)
        .where(ReviewCase.version == expected_version)
    )
    if required_states is not None:
        stmt = stmt.where(ReviewCase.state.in_(list(required_states)))
    stmt = stmt.values(**values)
    rowcount = db.execute(stmt).rowcount
    if rowcount != 1:
        db.rollback()
        return False
    db.commit()
    return True


def apply_adjudication(
    db: Session,
    *,
    case_id: str,
    expected_version: int,
    values: dict[str, Any],
    correction_event: dict[str, Any] | None,
) -> bool:
    """裁决落库：修正事件与案件状态在同一事务中提交。"""
    if correction_event is not None:
        stmt = sqlite_insert(EventModel).values(**correction_event)
        stmt = stmt.on_conflict_do_nothing(index_elements=["event_id", "plan_version"])
        db.execute(stmt)
    stmt = (
        update(ReviewCase)
        .where(ReviewCase.case_id == case_id)
        .where(ReviewCase.version == expected_version)
        .values(**values)
    )
    rowcount = db.execute(stmt).rowcount
    if rowcount != 1:
        db.rollback()
        return False
    db.commit()
    return True


def apply_merge(
    db: Session,
    *,
    source_id: str,
    source_version: int,
    source_values: dict[str, Any],
    target_id: str,
    target_version: int,
    target_values: dict[str, Any],
) -> bool:
    """合并落库：源案件与目标案件在同一事务中更新。"""
    source_stmt = (
        update(ReviewCase)
        .where(ReviewCase.case_id == source_id)
        .where(ReviewCase.version == source_version)
        .values(**source_values)
    )
    target_stmt = (
        update(ReviewCase)
        .where(ReviewCase.case_id == target_id)
        .where(ReviewCase.version == target_version)
        .values(**target_values)
    )
    if db.execute(source_stmt).rowcount != 1:
        db.rollback()
        return False
    if db.execute(target_stmt).rowcount != 1:
        db.rollback()
        return False
    db.commit()
    return True


def apply_split(
    db: Session,
    *,
    parent_id: str,
    parent_version: int,
    parent_values: dict[str, Any],
    child_row: dict[str, Any],
) -> bool:
    """拆分落库：子案件插入与父案件更新在同一事务中提交。"""
    stmt = (
        sqlite_insert(ReviewCase)
        .values(**child_row)
        .on_conflict_do_nothing(index_elements=["case_id"])
        .returning(ReviewCase.case_id)
    )
    if db.execute(stmt).scalar_one_or_none() is None:
        db.rollback()
        return False
    parent_stmt = (
        update(ReviewCase)
        .where(ReviewCase.case_id == parent_id)
        .where(ReviewCase.version == parent_version)
        .values(**parent_values)
    )
    if db.execute(parent_stmt).rowcount != 1:
        db.rollback()
        return False
    db.commit()
    return True


def open_cases_by_student(
    db: Session, plan_version: str
) -> dict[str, list[ReviewCase]]:
    """按学员分组的未决案件，供快照标注使用。"""
    stmt = (
        select(ReviewCase)
        .where(ReviewCase.plan_version == plan_version)
        .where(ReviewCase.state.in_([s.value for s in OPEN_STATES]))
        .order_by(ReviewCase.case_id)
    )
    grouped: dict[str, list[ReviewCase]] = {}
    for row in db.execute(stmt).scalars().all():
        grouped.setdefault(row.student_id, []).append(row)
    return grouped
