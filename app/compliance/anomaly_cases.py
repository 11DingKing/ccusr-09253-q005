"""异常学时复核案件的领域状态机、检测规则与审计工具（纯逻辑层）。

负向修正、超长签到与重叠活动统一进入复核案件：检测立案后由处理人认领、
补证、裁决，必要时复开；案件可合并或拆分，原始来源始终可追溯。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any, Iterable, Mapping, Sequence


class CaseState(StrEnum):
    DETECTED = "detected"
    CLAIMED = "claimed"
    ADJUDICATED = "adjudicated"
    REOPENED = "reopened"
    MERGED = "merged"
    SPLIT = "split"


OPEN_STATES: frozenset[CaseState] = frozenset(
    {CaseState.DETECTED, CaseState.CLAIMED, CaseState.REOPENED}
)

ALLOWED_TRANSITIONS: Mapping[CaseState, frozenset[CaseState]] = {
    CaseState.DETECTED: frozenset(
        {CaseState.CLAIMED, CaseState.MERGED, CaseState.SPLIT}
    ),
    CaseState.CLAIMED: frozenset(
        {CaseState.ADJUDICATED, CaseState.MERGED, CaseState.SPLIT}
    ),
    CaseState.ADJUDICATED: frozenset({CaseState.REOPENED}),
    CaseState.REOPENED: frozenset(
        {CaseState.CLAIMED, CaseState.MERGED, CaseState.SPLIT}
    ),
    CaseState.MERGED: frozenset(),
    CaseState.SPLIT: frozenset(),
}


class Rule(StrEnum):
    NEGATIVE_ADJUSTMENT = "negative_adjustment"
    OVERSIZED_CHECKIN = "oversized_checkin"
    OVERLAPPING_ACTIVITY = "overlapping_activity"
    MERGED = "merged"
    SPLIT = "split"


DETECTION_RULES: tuple[Rule, ...] = (
    Rule.NEGATIVE_ADJUSTMENT,
    Rule.OVERSIZED_CHECKIN,
    Rule.OVERLAPPING_ACTIVITY,
)


class Verdict(StrEnum):
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"


DEFAULT_MAX_CHECKIN_SECONDS = 12 * 60 * 60

REVIEWER_ROLE = "reviewer"
ADMIN_ROLE = "admin"
ACTOR_ROLES = frozenset({REVIEWER_ROLE, ADMIN_ROLE})


class CaseError(ValueError):
    """封装案件领域状态与业务约束。"""


class CaseStateError(CaseError):
    """状态机不允许的变更。"""


class CasePermissionError(CaseError):
    """操作人权限不足。"""


class CaseConcurrencyError(CaseError):
    """并发修改冲突。"""


@dataclass(frozen=True)
class EventView:
    """检测所需的事件最小视图。"""

    event_id: str
    event_type: str
    student_id: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class Detection:
    """一条待立案的异常检测结果。"""

    rule: Rule
    student_id: str
    dedup_key: str
    source_event_ids: tuple[str, ...]
    disputed_seconds: int
    note: str


def case_id_for(plan_version: str, dedup_key: str) -> str:
    """由计划与去重键推导确定性的案件标识。"""
    raw = f"{plan_version}|{dedup_key}".encode("utf-8")
    return "C-" + sha256(raw).hexdigest()[:16]


def _fingerprint(case_id: str, sequence: int, action: str, actor: str, detail: str) -> str:
    raw = f"{case_id}|{sequence}|{action}|{actor}|{detail}".encode("utf-8")
    return sha256(raw).hexdigest()


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def make_audit_entry(
    *,
    case_id: str,
    sequence: int,
    action: str,
    actor_id: str,
    occurred_at: datetime | None = None,
    from_state: str = "",
    to_state: str = "",
    detail: str = "",
) -> dict[str, Any]:
    moment = occurred_at or utcnow()
    return {
        "sequence": sequence,
        "action": action,
        "actor_id": actor_id,
        "occurred_at": _iso(moment),
        "from_state": from_state,
        "to_state": to_state,
        "detail": detail,
        "fingerprint": _fingerprint(case_id, sequence, action, actor_id, detail),
    }


def make_evidence_entry(
    *,
    sequence: int,
    actor_id: str,
    note: str,
    attachments: Sequence[str] = (),
    added_at: datetime | None = None,
) -> dict[str, Any]:
    moment = added_at or utcnow()
    return {
        "sequence": sequence,
        "actor_id": actor_id,
        "note": note,
        "attachments": list(attachments),
        "added_at": _iso(moment),
    }


def verify_audit_chain(audit: Sequence[Mapping[str, Any]]) -> bool:
    """审计链序号连续且指纹不重复。"""
    expected = 1
    seen: set[str] = set()
    for entry in audit:
        fingerprint = str(entry.get("fingerprint", ""))
        if entry.get("sequence") != expected or not fingerprint or fingerprint in seen:
            return False
        seen.add(fingerprint)
        expected += 1
    return True


def require_transition(current: CaseState, target: CaseState) -> None:
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise CaseStateError(f"案件状态不允许从 {current.value} 变更为 {target.value}")


def require_case_actor(
    *, actor_id: str, actor_role: str, assignee_id: str | None, action: str
) -> None:
    """仅案件处理人或管理员可以执行案件操作。"""
    if actor_role == ADMIN_ROLE:
        return
    if assignee_id and actor_id == assignee_id:
        return
    raise CasePermissionError(f"只有案件处理人或管理员可以{action}")


def require_admin(actor_role: str, action: str) -> None:
    if actor_role != ADMIN_ROLE:
        raise CasePermissionError(f"只有管理员可以{action}")


def detect_anomalies(
    events: Iterable[EventView],
    *,
    max_checkin_seconds: int = DEFAULT_MAX_CHECKIN_SECONDS,
    rules: Iterable[Rule] | None = None,
) -> list[Detection]:
    """扫描事件流，输出全部异常检测结果（纯函数，不修改任何状态）。

    是否立案由调用方根据去重键与已吸收事件集合决定。
    """
    if max_checkin_seconds <= 0:
        raise CaseError("签到时长上限必须大于零")
    enabled = frozenset(rules) if rules is not None else frozenset(DETECTION_RULES)
    detections: list[Detection] = []
    checkins_by_student: dict[str, list[tuple[str, datetime, datetime]]] = {}

    for event in sorted(events, key=lambda item: item.event_id):
        if event.event_type == "leave_correction":
            if Rule.NEGATIVE_ADJUSTMENT not in enabled:
                continue
            seconds = int(event.payload.get("adjustment_seconds", 0))
            if seconds < 0:
                detections.append(
                    Detection(
                        rule=Rule.NEGATIVE_ADJUSTMENT,
                        student_id=event.student_id,
                        dedup_key=f"negative_adjustment:{event.event_id}",
                        source_event_ids=(event.event_id,),
                        disputed_seconds=-seconds,
                        note=f"负向修正 {seconds} 秒，待复核",
                    )
                )
        elif event.event_type == "checkin":
            start = datetime.fromisoformat(str(event.payload["check_in_at"]))
            end = datetime.fromisoformat(str(event.payload["check_out_at"]))
            duration = int((end - start).total_seconds())
            if Rule.OVERSIZED_CHECKIN in enabled and duration > max_checkin_seconds:
                detections.append(
                    Detection(
                        rule=Rule.OVERSIZED_CHECKIN,
                        student_id=event.student_id,
                        dedup_key=f"oversized_checkin:{event.event_id}",
                        source_event_ids=(event.event_id,),
                        disputed_seconds=duration - max_checkin_seconds,
                        note=f"签到时长 {duration} 秒超出上限 {max_checkin_seconds} 秒",
                    )
                )
            checkins_by_student.setdefault(event.student_id, []).append(
                (event.event_id, start, end)
            )

    if Rule.OVERLAPPING_ACTIVITY in enabled:
        for student_id, entries in checkins_by_student.items():
            entries.sort(key=lambda item: (item[1], item[0]))
            for index, (first_id, _first_start, first_end) in enumerate(entries):
                for other_id, other_start, other_end in entries[index + 1 :]:
                    if other_start >= first_end:
                        break
                    overlap = int(
                        (min(first_end, other_end) - other_start).total_seconds()
                    )
                    if overlap <= 0:
                        continue
                    low, high = sorted((first_id, other_id))
                    detections.append(
                        Detection(
                            rule=Rule.OVERLAPPING_ACTIVITY,
                            student_id=student_id,
                            dedup_key=f"overlap:{low}:{high}",
                            source_event_ids=(low, high),
                            disputed_seconds=overlap,
                            note=f"签到 {low} 与 {high} 重叠 {overlap} 秒",
                        )
                    )
    return detections
