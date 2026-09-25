"""服务端业务模块。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .replay import (
    CheckinRecord,
    Event,
    ReplayState,
    StudentProgress,
    explain_checkin,
    replay,
)


@dataclass
class Snapshot:
    plan_version: str
    freeze_id: str | None
    timezone: str
    required_seconds: int
    generated_at: str
    event_cutoff_id: str | None
    students: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_version": self.plan_version,
            "freeze_id": self.freeze_id,
            "timezone": self.timezone,
            "required_seconds": self.required_seconds,
            "generated_at": self.generated_at,
            "event_cutoff_id": self.event_cutoff_id,
            "students": self.students,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Snapshot":
        return cls(
            plan_version=data["plan_version"],
            freeze_id=data.get("freeze_id"),
            timezone=data["timezone"],
            required_seconds=data["required_seconds"],
            generated_at=data["generated_at"],
            event_cutoff_id=data.get("event_cutoff_id"),
            students=list(data.get("students", [])),
        )


def _student_to_dict(
    progress: StudentProgress,
    tz_name: str,
    *,
    open_cases: list[dict[str, Any]] | None = None,
    held_case_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    case_map = held_case_map or {}
    cases = [dict(case) for case in (open_cases or [])]
    return {
        "student_id": progress.student_id,
        "confirmed_seconds": progress.confirmed_seconds,
        "pending_seconds": progress.pending_seconds,
        "adjustment_seconds": progress.adjustment_seconds,
        "total_seconds": progress.total_seconds,
        "lesson_units": progress.lesson_units,
        "pending_lesson_units": progress.pending_lesson_units,
        "meets_requirement": progress.meets_requirement,
        "daily": [
            {"academic_day": d.academic_day, "seconds": d.seconds}
            for d in progress.daily
        ],
        "checkins": [explain_checkin(c, tz_name) for c in progress.checkins],
        "adjustments": [
            {
                "event_id": a.event_id,
                "seconds": a.seconds,
                "reason": a.reason,
            }
            for a in progress.adjustments
        ],
        "held_adjustments": [
            {
                "event_id": a.event_id,
                "seconds": a.seconds,
                "reason": a.reason,
                "case_id": case_map.get(a.event_id),
            }
            for a in progress.held_adjustments
        ],
        "open_cases": cases,
        "pending_review_seconds": sum(
            int(case.get("disputed_seconds", 0)) for case in cases
        ),
    }


def build_snapshot(
    events: list[Event],
    *,
    plan_version: str,
    timezone_name: str,
    required_seconds: int,
    freeze_id: str | None = None,
    event_cutoff_id: str | None = None,
    generated_at: datetime | None = None,
    held_adjustment_event_ids: frozenset[str] | None = None,
    open_cases_by_student: dict[str, list[dict[str, Any]]] | None = None,
    held_case_map: dict[str, str] | None = None,
) -> Snapshot:
    """执行确定性的业务处理。"""
    state: ReplayState = replay(
        events,
        plan_version=plan_version,
        timezone_name=timezone_name,
        required_seconds=required_seconds,
        up_to_event_id=event_cutoff_id,
        held_adjustment_event_ids=held_adjustment_event_ids,
    )
    if generated_at is None:
        generated_at = datetime.now(timezone.utc)
    generated_at = generated_at.astimezone(timezone.utc)

    cases_by_student = open_cases_by_student or {}
    students = [
        _student_to_dict(
            state.students[sid],
            timezone_name,
            open_cases=cases_by_student.get(sid),
            held_case_map=held_case_map,
        )
        for sid in sorted(state.students)
    ]

    return Snapshot(
        plan_version=plan_version,
        freeze_id=freeze_id,
        timezone=timezone_name,
        required_seconds=required_seconds,
        generated_at=generated_at.isoformat().replace("+00:00", "Z"),
        event_cutoff_id=event_cutoff_id,
        students=students,
    )


def _index_students(snapshot: Snapshot) -> dict[str, dict[str, Any]]:
    return {s["student_id"]: s for s in snapshot.students}


def diff_snapshots(old: Snapshot, new: Snapshot) -> dict[str, Any]:
    """执行确定性的业务处理。"""
    old_map = _index_students(old)
    new_map = _index_students(new)
    all_ids = sorted(set(old_map) | set(new_map))

    student_changes: list[dict[str, Any]] = []
    for sid in all_ids:
        before = old_map.get(sid)
        after = new_map.get(sid)
        if before is None and after is not None:
            student_changes.append(
                {
                    "student_id": sid,
                    "change_type": "added",
                    "before": None,
                    "after": {
                        "total_seconds": after["total_seconds"],
                        "lesson_units": after["lesson_units"],
                        "meets_requirement": after["meets_requirement"],
                    },
                }
            )
            continue
        if after is None and before is not None:
            student_changes.append(
                {
                    "student_id": sid,
                    "change_type": "removed",
                    "before": {
                        "total_seconds": before["total_seconds"],
                        "lesson_units": before["lesson_units"],
                        "meets_requirement": before["meets_requirement"],
                    },
                    "after": None,
                }
            )
            continue

        assert before is not None and after is not None
        fields = (
            "confirmed_seconds",
            "pending_seconds",
            "adjustment_seconds",
            "total_seconds",
            "lesson_units",
            "pending_lesson_units",
            "meets_requirement",
            "pending_review_seconds",
        )
        changed_fields = {}
        for field_name in fields:
            if before.get(field_name) != after.get(field_name):
                changed_fields[field_name] = {
                    "before": before.get(field_name),
                    "after": after.get(field_name),
                }
        if changed_fields:
            student_changes.append(
                {
                    "student_id": sid,
                    "change_type": "modified",
                    "fields": changed_fields,
                }
            )

    return {
        "plan_version": old.plan_version,
        "old_freeze_id": old.freeze_id,
        "new_freeze_id": new.freeze_id,
        "old_generated_at": old.generated_at,
        "new_generated_at": new.generated_at,
        "old_event_cutoff_id": old.event_cutoff_id,
        "new_event_cutoff_id": new.event_cutoff_id,
        "student_changes": student_changes,
        "students_affected": len(student_changes),
    }


def explain_student(
    snapshot: Snapshot, student_id: str
) -> dict[str, Any] | None:
    """执行确定性的业务处理。"""
    for student in snapshot.students:
        if student["student_id"] == student_id:
            return student
    return None
