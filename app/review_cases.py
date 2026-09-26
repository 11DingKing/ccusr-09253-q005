"""异常学时复核案件的领域状态机、检测规则与指纹去重。

负向修正、超长签到和重叠活动统一进入复核案件：异常先立案，
经认领、补证、裁决后才生成最终修正事件，未决案件不直接改变学员总学时。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Iterable, Mapping, Sequence

from .core.clock import elapsed_seconds, to_utc
from .core.replay import Event, EventType


class CaseState(StrEnum):
    DETECTED = "detected"
    CLAIMED = "claimed"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"
    REOPENED = "reopened"
    MERGED = "merged"


OPEN_STATES: frozenset[CaseState] = frozenset(
    {CaseState.DETECTED, CaseState.CLAIMED, CaseState.REOPENED}
)
CLAIMABLE_STATES: frozenset[CaseState] = frozenset(
    {CaseState.DETECTED, CaseState.REOPENED}
)
REOPENABLE_STATES: frozenset[CaseState] = frozenset(
    {CaseState.RESOLVED, CaseState.DISMISSED}
)

ALLOWED_TRANSITIONS: Mapping[CaseState, frozenset[CaseState]] = {
    CaseState.DETECTED: frozenset({CaseState.CLAIMED, CaseState.MERGED}),
    CaseState.CLAIMED: frozenset(
        {CaseState.RESOLVED, CaseState.DISMISSED, CaseState.MERGED}
    ),
    CaseState.REOPENED: frozenset({CaseState.CLAIMED, CaseState.MERGED}),
    CaseState.RESOLVED: frozenset({CaseState.REOPENED}),
    CaseState.DISMISSED: frozenset({CaseState.REOPENED}),
    CaseState.MERGED: frozenset(),
}


class RuleKind(StrEnum):
    NEGATIVE_CORRECTION = "negative_correction"
    OVERLONG_CHECKIN = "overlong_checkin"
    OVERLAPPING_ACTIVITIES = "overlapping_activities"


DEFAULT_OVERLONG_THRESHOLD_SECONDS = 12 * 3600


class CaseError(Exception):
    """复核案件领域错误基类，status_code 供 API 层映射。"""

    status_code = 400


class CaseNotFoundError(CaseError):
    status_code = 404


class CaseValidationError(CaseError):
    status_code = 400


class CasePermissionError(CaseError):
    status_code = 403


class CaseStateError(CaseError):
    status_code = 409


class CaseConflictError(CaseError):
    status_code = 409


def ensure_transition(current: CaseState, target: CaseState) -> None:
    """校验状态机流转，非法流转抛出 CaseStateError。"""
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise CaseStateError(f"不允许从 {current} 变更到 {target}")


def anomaly_fingerprint(plan_version: str, rule: RuleKind, key: str) -> str:
    """同一异常的确定性指纹，用于立案去重。"""
    raw = f"{plan_version}|{rule.value}|{key}".encode("utf-8")
    return sha256(raw).hexdigest()


def split_fingerprint(parent_case_id: str, source_event_ids: Sequence[str]) -> str:
    """拆分子案件的确定性指纹：同一父案件拆出同一子集幂等。"""
    raw = f"{parent_case_id}|split|{','.join(sorted(source_event_ids))}".encode("utf-8")
    return sha256(raw).hexdigest()


def case_id_for_fingerprint(fingerprint: str) -> str:
    return f"C-{fingerprint[:20]}"


@dataclass(frozen=True)
class Anomaly:
    """一次检测到的异常，携带立案所需的全部确定性信息。"""

    rule: RuleKind
    plan_version: str
    student_id: str
    fingerprint: str
    source_event_ids: tuple[str, ...]
    suggested_correction_seconds: int
    detail: str

    @property
    def case_id(self) -> str:
        return case_id_for_fingerprint(self.fingerprint)


def _overlap_clusters(
    rows: list[tuple[str, datetime, datetime]],
) -> list[list[tuple[str, datetime, datetime]]]:
    """按时间相交关系把同一学员的签到聚成重叠簇（端点相接不算重叠）。"""
    ordered = sorted(rows, key=lambda r: (r[1], r[0]))
    clusters: list[list[tuple[str, datetime, datetime]]] = []
    current = [ordered[0]]
    current_end = ordered[0][2]
    for row in ordered[1:]:
        if row[1] < current_end:
            current.append(row)
            if row[2] > current_end:
                current_end = row[2]
        else:
            clusters.append(current)
            current = [row]
            current_end = row[2]
    clusters.append(current)
    return [cluster for cluster in clusters if len(cluster) >= 2]


def detect_anomalies(
    events: Iterable[Event],
    *,
    plan_version: str,
    overlong_threshold_seconds: int | None = None,
) -> list[Anomaly]:
    """执行确定性的异常检测：负向修正、超长签到、重叠活动。"""
    threshold = (
        overlong_threshold_seconds
        if overlong_threshold_seconds is not None
        else DEFAULT_OVERLONG_THRESHOLD_SECONDS
    )
    if threshold <= 0:
        raise CaseValidationError("超长签到阈值必须大于零")

    anomalies: list[Anomaly] = []
    checkins_by_student: dict[str, list[tuple[str, datetime, datetime]]] = {}

    sorted_events = sorted(
        (e for e in events if e.plan_version == plan_version),
        key=lambda e: e.event_id,
    )
    for event in sorted_events:
        if event.event_type == EventType.LEAVE_CORRECTION:
            seconds = int(event.payload.get("adjustment_seconds", 0))
            if seconds < 0:
                anomalies.append(
                    Anomaly(
                        rule=RuleKind.NEGATIVE_CORRECTION,
                        plan_version=plan_version,
                        student_id=event.student_id,
                        fingerprint=anomaly_fingerprint(
                            plan_version, RuleKind.NEGATIVE_CORRECTION, event.event_id
                        ),
                        source_event_ids=(event.event_id,),
                        suggested_correction_seconds=-seconds,
                        detail=(
                            f"负向修正事件 {event.event_id} 调整 {seconds} 秒，"
                            "需复核是否撤销"
                        ),
                    )
                )
        elif event.event_type == EventType.CHECKIN:
            start = to_utc(datetime.fromisoformat(event.payload["check_in_at"]))
            end = to_utc(datetime.fromisoformat(event.payload["check_out_at"]))
            checkins_by_student.setdefault(event.student_id, []).append(
                (event.event_id, start, end)
            )
            duration = elapsed_seconds(start, end)
            if duration > threshold:
                anomalies.append(
                    Anomaly(
                        rule=RuleKind.OVERLONG_CHECKIN,
                        plan_version=plan_version,
                        student_id=event.student_id,
                        fingerprint=anomaly_fingerprint(
                            plan_version, RuleKind.OVERLONG_CHECKIN, event.event_id
                        ),
                        source_event_ids=(event.event_id,),
                        suggested_correction_seconds=-(duration - threshold),
                        detail=(
                            f"签到事件 {event.event_id} 时长 {duration} 秒，"
                            f"超过阈值 {threshold} 秒"
                        ),
                    )
                )

    for student_id, rows in sorted(checkins_by_student.items()):
        for cluster in _overlap_clusters(rows):
            ids = tuple(sorted(row[0] for row in cluster))
            anomalies.append(
                Anomaly(
                    rule=RuleKind.OVERLAPPING_ACTIVITIES,
                    plan_version=plan_version,
                    student_id=student_id,
                    fingerprint=anomaly_fingerprint(
                        plan_version,
                        RuleKind.OVERLAPPING_ACTIVITIES,
                        ",".join(ids),
                    ),
                    source_event_ids=ids,
                    suggested_correction_seconds=0,
                    detail=(
                        f"学员存在 {len(ids)} 个时间重叠的签到活动："
                        + ", ".join(ids)
                    ),
                )
            )

    return sorted(anomalies, key=lambda a: a.fingerprint)
