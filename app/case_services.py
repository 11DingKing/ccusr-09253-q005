"""复核案件应用服务：检测、认领、补证、裁决、复开、合并与拆分。

权限模型：操作者通过 Actor（reviewer / admin）标识。
- 检测、认领：reviewer 或 admin；
- 补证、裁决：案件处理人本人或 admin（未认领案件任何 reviewer 可补证）；
- 复开、合并、拆分：仅 admin。
裁决通过时生成 case_correction 修正事件并关联到案件；
复开后再次裁决按「目标修正总额 - 已落地修正」的差额追加事件，保证收敛。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy.orm import Session

from . import case_repository
from .core.replay import EventType
from .models import ReviewCase
from .repository import get_plan, load_events
from .review_cases import (
    CLAIMABLE_STATES,
    OPEN_STATES,
    REOPENABLE_STATES,
    CaseConflictError,
    CaseError,
    CaseNotFoundError,
    CasePermissionError,
    CaseState,
    CaseStateError,
    CaseValidationError,
    case_id_for_fingerprint,
    detect_anomalies,
    split_fingerprint,
)
from .services import PlanNotFoundError

ROLE_ADMIN = "admin"
ROLE_REVIEWER = "reviewer"
KNOWN_ROLES = frozenset({ROLE_ADMIN, ROLE_REVIEWER})

VERDICT_UPHELD = "upheld"
VERDICT_REJECTED = "rejected"


@dataclass(frozen=True)
class Actor:
    """API 层解析出的操作者身份。"""

    actor_id: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_plan(db: Session, plan_version: str) -> None:
    if get_plan(db, plan_version) is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")


def _load_case(db: Session, plan_version: str, case_id: str) -> ReviewCase:
    case = case_repository.get_case(db, case_id)
    if case is None or case.plan_version != plan_version:
        raise CaseNotFoundError(
            f"case '{case_id}' does not exist in plan '{plan_version}'"
        )
    return case


def _require_reviewer(actor: Actor) -> None:
    if actor.role not in KNOWN_ROLES:
        raise CasePermissionError("仅复核员或管理员可以执行该操作")


def _require_admin(actor: Actor) -> None:
    if not actor.is_admin:
        raise CasePermissionError("仅管理员可以执行该操作")


def _require_assignee_or_admin(case: ReviewCase, actor: Actor, action: str) -> None:
    if actor.is_admin:
        return
    if case.assignee_id is None:
        return
    if case.assignee_id != actor.actor_id:
        raise CasePermissionError(f"仅案件处理人或管理员可以{action}")


def _history_entry(
    case: ReviewCase,
    *,
    action: str,
    actor: Actor,
    reason: str,
    to_state: str | None = None,
) -> dict[str, Any]:
    return {
        "seq": len(case.history) + 1,
        "action": action,
        "actor_id": actor.actor_id,
        "from_state": case.state,
        "to_state": to_state if to_state is not None else case.state,
        "reason": reason,
        "at": _iso(_now()),
    }


def case_to_dict(case: ReviewCase) -> dict[str, Any]:
    """序列化案件，供 API 响应与测试断言使用。"""
    return {
        "case_id": case.case_id,
        "plan_version": case.plan_version,
        "student_id": case.student_id,
        "rule": case.rule,
        "state": case.state,
        "fingerprints": list(case.fingerprints),
        "source_event_ids": list(case.source_event_ids),
        "lineage": list(case.lineage),
        "child_case_ids": list(case.child_case_ids),
        "merged_into": case.merged_into,
        "assignee_id": case.assignee_id,
        "suggested_correction_seconds": case.suggested_correction_seconds,
        "applied_correction_seconds": case.applied_correction_seconds,
        "correction_event_ids": list(case.correction_event_ids),
        "resolution": case.resolution,
        "resolution_reason": case.resolution_reason,
        "evidence": list(case.evidence),
        "history": list(case.history),
        "version": case.version,
        "created_at": case.created_at,
        "updated_at": case.updated_at,
    }


def detect_cases(
    db: Session,
    *,
    plan_version: str,
    actor: Actor,
    overlong_threshold_seconds: int | None = None,
) -> dict[str, Any]:
    """扫描事件流并立案；同一异常（指纹）无论案件处于何状态都不会重复立案。"""
    _require_reviewer(actor)
    _require_plan(db, plan_version)
    events = load_events(db, plan_version)
    anomalies = detect_anomalies(
        events,
        plan_version=plan_version,
        overlong_threshold_seconds=overlong_threshold_seconds,
    )
    covered = case_repository.fingerprint_index(db, plan_version)
    now = _now()
    rows: list[dict[str, Any]] = []
    for anomaly in anomalies:
        if anomaly.fingerprint in covered:
            continue
        rows.append(
            {
                "case_id": anomaly.case_id,
                "plan_version": plan_version,
                "student_id": anomaly.student_id,
                "rule": anomaly.rule.value,
                "state": CaseState.DETECTED.value,
                "fingerprints": [anomaly.fingerprint],
                "source_event_ids": list(anomaly.source_event_ids),
                "lineage": [],
                "child_case_ids": [],
                "suggested_correction_seconds": anomaly.suggested_correction_seconds,
                "applied_correction_seconds": 0,
                "adjudication_seq": 0,
                "correction_event_ids": [],
                "evidence": [],
                "history": [
                    {
                        "seq": 1,
                        "action": "detect",
                        "actor_id": actor.actor_id,
                        "from_state": None,
                        "to_state": CaseState.DETECTED.value,
                        "reason": anomaly.detail,
                        "at": _iso(now),
                    }
                ],
                "version": 1,
                "created_at": now,
                "updated_at": now,
            }
        )
    created_ids = set(case_repository.insert_cases(db, rows))
    # 并发检测下重新读取覆盖索引，保证响应中的已存在列表准确。
    covered = case_repository.fingerprint_index(db, plan_version)
    existing: list[str] = []
    for anomaly in anomalies:
        case_id = covered.get(anomaly.fingerprint)
        if case_id is not None and case_id not in created_ids:
            existing.append(case_id)
    created = [
        case_to_dict(case_repository.get_case(db, case_id))
        for case_id in sorted(created_ids)
    ]
    return {
        "created": created,
        "existing": sorted(set(existing)),
        "total_anomalies": len(anomalies),
    }


def list_cases(
    db: Session,
    plan_version: str,
    *,
    student_id: str | None = None,
    state: str | None = None,
) -> list[dict[str, Any]]:
    _require_plan(db, plan_version)
    rows = case_repository.list_cases(
        db, plan_version, student_id=student_id, state=state
    )
    return [case_to_dict(row) for row in rows]


def get_case(db: Session, plan_version: str, case_id: str) -> dict[str, Any]:
    _require_plan(db, plan_version)
    return case_to_dict(_load_case(db, plan_version, case_id))


def claim_case(
    db: Session, *, plan_version: str, case_id: str, actor: Actor
) -> dict[str, Any]:
    """认领案件；并发认领通过数据库条件更新保证只有一人成功。"""
    _require_reviewer(actor)
    _require_plan(db, plan_version)
    case = _load_case(db, plan_version, case_id)
    if CaseState(case.state) not in CLAIMABLE_STATES:
        raise CaseStateError(f"案件当前状态 {case.state} 不可认领")
    entry = _history_entry(
        case, action="claim", actor=actor, reason="认领案件",
        to_state=CaseState.CLAIMED.value,
    )
    ok = case_repository.update_case(
        db,
        case_id=case_id,
        expected_version=case.version,
        values={
            "state": CaseState.CLAIMED.value,
            "assignee_id": actor.actor_id,
            "history": list(case.history) + [entry],
            "version": case.version + 1,
            "updated_at": _now(),
        },
        required_states=[s.value for s in CLAIMABLE_STATES],
    )
    if not ok:
        raise CaseConflictError("案件已被他人认领或状态已变化，请刷新后重试")
    return case_to_dict(_load_case(db, plan_version, case_id))


def add_evidence(
    db: Session,
    *,
    plan_version: str,
    case_id: str,
    actor: Actor,
    note: str,
    attachments: Sequence[str] = (),
) -> dict[str, Any]:
    """补证：为未决案件追加证据材料。"""
    _require_reviewer(actor)
    note = note.strip()
    if not note:
        raise CaseValidationError("补证说明不能为空")
    _require_plan(db, plan_version)
    case = _load_case(db, plan_version, case_id)
    if CaseState(case.state) not in OPEN_STATES:
        raise CaseStateError(f"案件当前状态 {case.state} 不可补证")
    _require_assignee_or_admin(case, actor, "补证")
    item = {
        "evidence_id": f"EV-{case_id}-{len(case.evidence) + 1}",
        "actor_id": actor.actor_id,
        "note": note,
        "attachments": [a.strip() for a in attachments if a.strip()],
        "created_at": _iso(_now()),
    }
    entry = _history_entry(case, action="evidence", actor=actor, reason=note)
    ok = case_repository.update_case(
        db,
        case_id=case_id,
        expected_version=case.version,
        values={
            "evidence": list(case.evidence) + [item],
            "history": list(case.history) + [entry],
            "version": case.version + 1,
            "updated_at": _now(),
        },
    )
    if not ok:
        raise CaseConflictError("案件状态已变化，请刷新后重试")
    return case_to_dict(_load_case(db, plan_version, case_id))


def adjudicate_case(
    db: Session,
    *,
    plan_version: str,
    case_id: str,
    actor: Actor,
    verdict: str,
    reason: str,
    correction_seconds: int | None = None,
) -> dict[str, Any]:
    """裁决案件：通过则按差额生成 case_correction 修正事件，驳回则关闭。"""
    _require_reviewer(actor)
    reason = reason.strip()
    if not reason:
        raise CaseValidationError("裁决必须填写理由")
    if verdict not in {VERDICT_UPHELD, VERDICT_REJECTED}:
        raise CaseValidationError("裁决结论必须是 upheld 或 rejected")
    _require_plan(db, plan_version)
    case = _load_case(db, plan_version, case_id)
    if CaseState(case.state) != CaseState.CLAIMED:
        raise CaseStateError("案件必须先认领才能裁决")
    _require_assignee_or_admin(case, actor, "裁决")

    if verdict == VERDICT_REJECTED:
        entry = _history_entry(
            case, action="adjudicate", actor=actor, reason=reason,
            to_state=CaseState.DISMISSED.value,
        )
        ok = case_repository.update_case(
            db,
            case_id=case_id,
            expected_version=case.version,
            values={
                "state": CaseState.DISMISSED.value,
                "resolution": VERDICT_REJECTED,
                "resolution_reason": reason,
                "history": list(case.history) + [entry],
                "version": case.version + 1,
                "updated_at": _now(),
            },
        )
    else:
        target = (
            correction_seconds
            if correction_seconds is not None
            else case.suggested_correction_seconds
        )
        delta = target - case.applied_correction_seconds
        seq = case.adjudication_seq + 1
        event_id = f"casecorr-{case_id}-{seq}"
        correction_event = None
        if delta != 0:
            correction_event = {
                "event_id": event_id,
                "plan_version": plan_version,
                "student_id": case.student_id,
                "event_type": EventType.CASE_CORRECTION.value,
                "payload": {
                    "adjustment_seconds": delta,
                    "reason": reason,
                    "case_id": case_id,
                    "rule": case.rule,
                },
            }
        entry = _history_entry(
            case, action="adjudicate", actor=actor, reason=reason,
            to_state=CaseState.RESOLVED.value,
        )
        ok = case_repository.apply_adjudication(
            db,
            case_id=case_id,
            expected_version=case.version,
            values={
                "state": CaseState.RESOLVED.value,
                "resolution": VERDICT_UPHELD,
                "resolution_reason": reason,
                "applied_correction_seconds": target,
                "adjudication_seq": seq,
                "correction_event_ids": list(case.correction_event_ids)
                + ([event_id] if correction_event is not None else []),
                "history": list(case.history) + [entry],
                "version": case.version + 1,
                "updated_at": _now(),
            },
            correction_event=correction_event,
        )
    if not ok:
        raise CaseConflictError("案件状态已变化，请刷新后重试")
    return case_to_dict(_load_case(db, plan_version, case_id))


def reopen_case(
    db: Session, *, plan_version: str, case_id: str, actor: Actor, reason: str
) -> dict[str, Any]:
    """复开已裁决案件；已落地的修正事件不回滚，由后续裁决按差额收敛。"""
    _require_admin(actor)
    reason = reason.strip()
    if not reason:
        raise CaseValidationError("复开必须填写理由")
    _require_plan(db, plan_version)
    case = _load_case(db, plan_version, case_id)
    if CaseState(case.state) not in REOPENABLE_STATES:
        raise CaseStateError(f"案件当前状态 {case.state} 不可复开")
    entry = _history_entry(
        case, action="reopen", actor=actor, reason=reason,
        to_state=CaseState.REOPENED.value,
    )
    ok = case_repository.update_case(
        db,
        case_id=case_id,
        expected_version=case.version,
        values={
            "state": CaseState.REOPENED.value,
            "assignee_id": None,
            "resolution": None,
            "resolution_reason": None,
            "history": list(case.history) + [entry],
            "version": case.version + 1,
            "updated_at": _now(),
        },
    )
    if not ok:
        raise CaseConflictError("案件状态已变化，请刷新后重试")
    return case_to_dict(_load_case(db, plan_version, case_id))


def merge_cases(
    db: Session,
    *,
    plan_version: str,
    source_case_id: str,
    target_case_id: str,
    actor: Actor,
    reason: str,
) -> dict[str, Any]:
    """把源案件合并进目标案件；目标吸收其指纹与来源事件，血缘可追溯。"""
    _require_admin(actor)
    reason = reason.strip()
    if not reason:
        raise CaseValidationError("合并必须填写理由")
    if source_case_id == target_case_id:
        raise CaseValidationError("不能将案件合并到自身")
    _require_plan(db, plan_version)
    source = _load_case(db, plan_version, source_case_id)
    target = _load_case(db, plan_version, target_case_id)
    if CaseState(source.state) not in OPEN_STATES:
        raise CaseStateError(f"源案件当前状态 {source.state} 不可被合并")
    if CaseState(target.state) not in OPEN_STATES:
        raise CaseStateError(f"目标案件当前状态 {target.state} 不能接收合并")

    lineage: list[str] = []
    for item in list(target.lineage) + [source.case_id] + list(source.lineage):
        if item not in lineage:
            lineage.append(item)
    target_entry = _history_entry(
        target, action="merge", actor=actor,
        reason=f"合并案件 {source.case_id}：{reason}",
    )
    source_entry = _history_entry(
        source, action="merge", actor=actor,
        reason=f"并入案件 {target.case_id}：{reason}",
        to_state=CaseState.MERGED.value,
    )
    ok = case_repository.apply_merge(
        db,
        source_id=source.case_id,
        source_version=source.version,
        source_values={
            "state": CaseState.MERGED.value,
            "merged_into": target.case_id,
            "history": list(source.history) + [source_entry],
            "version": source.version + 1,
            "updated_at": _now(),
        },
        target_id=target.case_id,
        target_version=target.version,
        target_values={
            "fingerprints": sorted(set(target.fingerprints) | set(source.fingerprints)),
            "source_event_ids": sorted(
                set(target.source_event_ids) | set(source.source_event_ids)
            ),
            "lineage": lineage,
            "history": list(target.history) + [target_entry],
            "version": target.version + 1,
            "updated_at": _now(),
        },
    )
    if not ok:
        raise CaseConflictError("案件状态已变化，请刷新后重试")
    return {
        "source": case_to_dict(_load_case(db, plan_version, source_case_id)),
        "target": case_to_dict(_load_case(db, plan_version, target_case_id)),
    }


def split_case(
    db: Session,
    *,
    plan_version: str,
    case_id: str,
    actor: Actor,
    reason: str,
    source_event_ids: Sequence[str],
) -> dict[str, Any]:
    """从父案件拆出子案件；子案件记录血缘，父案件登记子案件 id。"""
    _require_admin(actor)
    reason = reason.strip()
    if not reason:
        raise CaseValidationError("拆分必须填写理由")
    subset = sorted({eid.strip() for eid in source_event_ids if eid.strip()})
    if not subset:
        raise CaseValidationError("拆分必须指定至少一个来源事件")
    _require_plan(db, plan_version)
    parent = _load_case(db, plan_version, case_id)
    if CaseState(parent.state) not in OPEN_STATES:
        raise CaseStateError(f"案件当前状态 {parent.state} 不可拆分")
    unknown = sorted(set(subset) - set(parent.source_event_ids))
    if unknown:
        raise CaseValidationError(f"事件不属于原案件: {unknown}")

    fingerprint = split_fingerprint(parent.case_id, subset)
    child_id = case_id_for_fingerprint(fingerprint)
    if case_repository.get_case(db, child_id) is not None:
        raise CaseConflictError("相同的拆分已存在")
    now = _now()
    child_row = {
        "case_id": child_id,
        "plan_version": plan_version,
        "student_id": parent.student_id,
        "rule": parent.rule,
        "state": CaseState.DETECTED.value,
        "fingerprints": [fingerprint],
        "source_event_ids": subset,
        "lineage": list(parent.lineage) + [parent.case_id],
        "child_case_ids": [],
        "suggested_correction_seconds": 0,
        "applied_correction_seconds": 0,
        "adjudication_seq": 0,
        "correction_event_ids": [],
        "evidence": [],
        "history": [
            {
                "seq": 1,
                "action": "split",
                "actor_id": actor.actor_id,
                "from_state": None,
                "to_state": CaseState.DETECTED.value,
                "reason": f"自案件 {parent.case_id} 拆出：{reason}",
                "at": _iso(now),
            }
        ],
        "version": 1,
        "created_at": now,
        "updated_at": now,
    }
    parent_entry = _history_entry(
        parent, action="split", actor=actor,
        reason=f"拆出案件 {child_id}：{reason}",
    )
    ok = case_repository.apply_split(
        db,
        parent_id=parent.case_id,
        parent_version=parent.version,
        parent_values={
            "child_case_ids": list(parent.child_case_ids) + [child_id],
            "history": list(parent.history) + [parent_entry],
            "version": parent.version + 1,
            "updated_at": now,
        },
        child_row=child_row,
    )
    if not ok:
        raise CaseConflictError("案件状态已变化，请刷新后重试")
    return {
        "parent": case_to_dict(_load_case(db, plan_version, case_id)),
        "child": case_to_dict(_load_case(db, plan_version, child_id)),
    }


__all__ = [
    "Actor",
    "ROLE_ADMIN",
    "ROLE_REVIEWER",
    "KNOWN_ROLES",
    "VERDICT_UPHELD",
    "VERDICT_REJECTED",
    "CaseError",
    "CaseNotFoundError",
    "CaseValidationError",
    "CasePermissionError",
    "CaseStateError",
    "CaseConflictError",
    "detect_cases",
    "list_cases",
    "get_case",
    "claim_case",
    "add_evidence",
    "adjudicate_case",
    "reopen_case",
    "merge_cases",
    "split_case",
    "case_to_dict",
]
