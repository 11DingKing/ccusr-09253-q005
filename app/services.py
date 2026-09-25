"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from .compliance.anomaly_cases import (
    OPEN_STATES,
    CaseConcurrencyError,
    CaseError,
    CaseState,
    CaseStateError,
    EventView,
    Rule,
    Verdict,
    case_id_for,
    detect_anomalies,
    make_audit_entry,
    make_evidence_entry,
    require_admin,
    require_case_actor,
    require_transition,
)
from .core.snapshot import Snapshot, build_snapshot, diff_snapshots, explain_student
from .models import AnomalyCase
from .repository import (
    conditional_transition,
    get_case,
    get_event,
    get_freeze,
    get_plan,
    insert_case,
    insert_events,
    insert_freeze,
    list_cases,
    load_events,
    load_events_up_to,
    max_event_id,
    update_case_fields,
    upsert_plan,
)


class PlanNotFoundError(Exception):
    pass


class FreezeConflictError(Exception):
    pass


class FreezeNotFoundError(Exception):
    pass


class CaseNotFoundError(Exception):
    pass


def get_plan_plain(db: Session, plan_version: str) -> dict[str, Any] | None:
    plan = get_plan(db, plan_version)
    if plan is None:
        return None
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def ensure_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> dict[str, Any]:
    plan = upsert_plan(
        db,
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def _require_plan(db: Session, plan_version: str):
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    return plan


def import_events(
    db: Session, *, plan_version: str, events: list[dict[str, Any]]
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    accepted, duplicates = insert_events(
        db, plan_version=plan_version, events=events
    )
    return {
        "accepted": len(accepted),
        "duplicates": duplicates,
        "rejected": [],
    }


def _case_overlay(
    db: Session, plan_version: str
) -> tuple[frozenset[str], dict[str, list[dict[str, Any]]], dict[str, str]]:
    """汇总案件对学时快照的影响：挂起的修正事件、未决案件标注、挂起事件归属。"""
    rows = list_cases(db, plan_version)
    resolution_ids = {r.resolution_event_id for r in rows if r.resolution_event_id}
    held: set[str] = set()
    for row in rows:
        held.update(row.absorbed_event_ids or [])
    # 只有案件当前裁决关联的最终修正事件才计入学时，其余被吸收事件一律挂起。
    held -= resolution_ids

    open_states = {state.value for state in OPEN_STATES}
    open_by_student: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.state not in open_states:
            continue
        open_by_student.setdefault(row.student_id, []).append(
            {
                "case_id": row.case_id,
                "rule": row.rule,
                "state": row.state,
                "disputed_seconds": row.disputed_seconds,
                "source_event_ids": list(row.source_event_ids or []),
                "assignee_id": row.assignee_id,
            }
        )

    held_case_map: dict[str, str] = {}
    for row in rows:
        for event_id in row.absorbed_event_ids or []:
            if event_id in held and event_id not in held_case_map:
                held_case_map[event_id] = row.case_id
    return frozenset(held), open_by_student, held_case_map


def current_snapshot(db: Session, plan_version: str) -> Snapshot:
    plan = _require_plan(db, plan_version)
    events = load_events(db, plan_version)
    held, open_by_student, held_case_map = _case_overlay(db, plan_version)
    return build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        held_adjustment_event_ids=held,
        open_cases_by_student=open_by_student,
        held_case_map=held_case_map,
    )


def student_progress(
    db: Session, plan_version: str, student_id: str
) -> dict[str, Any] | None:
    snap = current_snapshot(db, plan_version)
    return explain_student(snap, student_id)


def freeze_semester(
    db: Session, *, plan_version: str, freeze_id: str
) -> tuple[Snapshot, bool]:
    """执行确定性的业务处理。"""
    plan = _require_plan(db, plan_version)
    existing = get_freeze(db, plan_version, freeze_id)
    if existing is not None:
        return Snapshot.from_dict(existing.snapshot), False

    cutoff = max_event_id(db, plan_version)
    events = load_events(db, plan_version)
    held, open_by_student, held_case_map = _case_overlay(db, plan_version)
    snap = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        freeze_id=freeze_id,
        event_cutoff_id=cutoff,
        held_adjustment_event_ids=held,
        open_cases_by_student=open_by_student,
        held_case_map=held_case_map,
    )
    row = insert_freeze(
        db,
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snap.to_dict(),
        event_cutoff_id=cutoff,
    )
    if row is None:
        existing = get_freeze(db, plan_version, freeze_id)
        assert existing is not None
        return Snapshot.from_dict(existing.snapshot), False
    return snap, True


def get_frozen_snapshot(
    db: Session, plan_version: str, freeze_id: str
) -> Snapshot:
    _require_plan(db, plan_version)
    row = get_freeze(db, plan_version, freeze_id)
    if row is None:
        raise FreezeNotFoundError(
            f"freeze '{freeze_id}' for plan '{plan_version}' does not exist"
        )
    return Snapshot.from_dict(row.snapshot)


def explain_frozen_student(
    db: Session, plan_version: str, freeze_id: str, student_id: str
) -> dict[str, Any] | None:
    snap = get_frozen_snapshot(db, plan_version, freeze_id)
    return explain_student(snap, student_id)


def diff_freezes(
    db: Session, plan_version: str, old_freeze_id: str, new_freeze_id: str
) -> dict[str, Any]:
    old = get_frozen_snapshot(db, plan_version, old_freeze_id)
    new = get_frozen_snapshot(db, plan_version, new_freeze_id)
    return diff_snapshots(old, new)


# ---------------------------------------------------------------------------
# 异常学时复核案件
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


def _case_to_dict(row: AnomalyCase) -> dict[str, Any]:
    lineage = {"parents": [], "children": []}
    lineage.update(row.lineage or {})
    return {
        "case_id": row.case_id,
        "plan_version": row.plan_version,
        "student_id": row.student_id,
        "rule": row.rule,
        "state": row.state,
        "assignee_id": row.assignee_id,
        "dedup_key": row.dedup_key,
        "disputed_seconds": row.disputed_seconds,
        "source_event_ids": list(row.source_event_ids or []),
        "absorbed_event_ids": list(row.absorbed_event_ids or []),
        "resolution_event_id": row.resolution_event_id,
        "verdict": row.verdict,
        "evidence": [dict(entry) for entry in (row.evidence or [])],
        "audit": [dict(entry) for entry in (row.audit or [])],
        "lineage": lineage,
        "version": row.version,
        "created_at": _iso_or_none(row.created_at),
        "updated_at": _iso_or_none(row.updated_at),
    }


def _get_case_or_404(db: Session, plan_version: str, case_id: str) -> AnomalyCase:
    row = get_case(db, plan_version, case_id)
    if row is None:
        raise CaseNotFoundError(
            f"case '{case_id}' for plan '{plan_version}' does not exist"
        )
    return row


def _append_audit(
    row: AnomalyCase,
    *,
    action: str,
    actor_id: str,
    detail: str,
    from_state: str | None = None,
    to_state: str | None = None,
) -> list[dict[str, Any]]:
    entries = [dict(entry) for entry in (row.audit or [])]
    entries.append(
        make_audit_entry(
            case_id=row.case_id,
            sequence=len(entries) + 1,
            action=action,
            actor_id=actor_id,
            from_state=from_state if from_state is not None else row.state,
            to_state=to_state if to_state is not None else row.state,
            detail=detail,
        )
    )
    return entries


def _save_case(
    db: Session, row: AnomalyCase, changes: dict[str, Any]
) -> AnomalyCase:
    if not update_case_fields(
        db,
        row.plan_version,
        row.case_id,
        expected_version=row.version,
        changes=changes,
    ):
        raise CaseConcurrencyError("案件已被并发修改，请刷新后重试")
    saved = get_case(db, row.plan_version, row.case_id)
    assert saved is not None
    return saved


def _new_case_values(
    *,
    plan_version: str,
    case_id: str,
    student_id: str,
    rule: Rule,
    dedup_key: str,
    disputed_seconds: int,
    source_event_ids: list[str],
    absorbed_event_ids: list[str],
    parents: list[str],
    actor_id: str,
    action: str,
    note: str,
    evidence_attachments: list[str],
) -> dict[str, Any]:
    now = _now()
    return {
        "plan_version": plan_version,
        "case_id": case_id,
        "student_id": student_id,
        "rule": rule.value,
        "state": CaseState.DETECTED.value,
        "dedup_key": dedup_key,
        "assignee_id": None,
        "disputed_seconds": disputed_seconds,
        "source_event_ids": source_event_ids,
        "absorbed_event_ids": absorbed_event_ids,
        "resolution_event_id": None,
        "verdict": None,
        "evidence": [
            make_evidence_entry(
                sequence=1,
                actor_id=actor_id,
                note=note,
                attachments=evidence_attachments,
                added_at=now,
            )
        ],
        "audit": [
            make_audit_entry(
                case_id=case_id,
                sequence=1,
                action=action,
                actor_id=actor_id,
                occurred_at=now,
                from_state="",
                to_state=CaseState.DETECTED.value,
                detail=note,
            )
        ],
        "lineage": {"parents": parents, "children": []},
        "version": 1,
        "created_at": now,
        "updated_at": now,
    }


def detect_cases(
    db: Session,
    *,
    plan_version: str,
    max_checkin_seconds: int,
    rules: list[str] | None,
    actor_id: str,
) -> dict[str, Any]:
    """扫描事件流立案；同一异常由去重键保证不会重复立案。"""
    _require_plan(db, plan_version)
    events = load_events(db, plan_version)
    existing_cases = list_cases(db, plan_version)
    absorbed = {
        event_id
        for row in existing_cases
        for event_id in (row.absorbed_event_ids or [])
    }
    known_keys = {row.dedup_key for row in existing_cases}
    views = [
        EventView(
            event_id=event.event_id,
            event_type=str(event.event_type),
            student_id=event.student_id,
            payload=event.payload,
        )
        for event in events
    ]
    rule_filter = [Rule(rule) for rule in rules] if rules else None
    detections = detect_anomalies(
        views,
        max_checkin_seconds=max_checkin_seconds,
        rules=rule_filter,
    )

    created: list[dict[str, Any]] = []
    existing: list[dict[str, Any]] = []
    for detection in detections:
        if detection.dedup_key in known_keys:
            case_id = case_id_for(plan_version, detection.dedup_key)
            row = get_case(db, plan_version, case_id)
            assert row is not None
            existing.append(_case_to_dict(row))
            continue
        # 案件裁决时生成的最终修正事件也会命中规则，但不应再单独立案。
        if absorbed.issuperset(detection.source_event_ids):
            continue
        case_id = case_id_for(plan_version, detection.dedup_key)
        row = insert_case(
            db,
            values=_new_case_values(
                plan_version=plan_version,
                case_id=case_id,
                student_id=detection.student_id,
                rule=detection.rule,
                dedup_key=detection.dedup_key,
                disputed_seconds=detection.disputed_seconds,
                source_event_ids=list(detection.source_event_ids),
                absorbed_event_ids=list(detection.source_event_ids),
                parents=[],
                actor_id=actor_id,
                action="detect",
                note=detection.note,
                evidence_attachments=list(detection.source_event_ids),
            ),
        )
        if row is None:
            row = get_case(db, plan_version, case_id)
            assert row is not None
            existing.append(_case_to_dict(row))
        else:
            created.append(_case_to_dict(row))
    return {
        "created": created,
        "existing": existing,
        "scanned_events": len(events),
    }


def list_case_dicts(
    db: Session,
    plan_version: str,
    *,
    state: str | None = None,
    student_id: str | None = None,
) -> list[dict[str, Any]]:
    _require_plan(db, plan_version)
    rows = list_cases(db, plan_version, state=state, student_id=student_id)
    return [_case_to_dict(row) for row in rows]


def get_case_detail(db: Session, plan_version: str, case_id: str) -> dict[str, Any]:
    _require_plan(db, plan_version)
    return _case_to_dict(_get_case_or_404(db, plan_version, case_id))


def claim_case(
    db: Session,
    *,
    plan_version: str,
    case_id: str,
    actor_id: str,
    actor_role: str,
) -> dict[str, Any]:
    """认领案件；并发认领由条件更新保证只有一个获胜者。"""
    _require_plan(db, plan_version)
    row = _get_case_or_404(db, plan_version, case_id)
    if row.state == CaseState.CLAIMED.value and row.assignee_id == actor_id:
        return _case_to_dict(row)

    claimed = conditional_transition(
        db,
        plan_version,
        case_id,
        from_states=[CaseState.DETECTED.value, CaseState.REOPENED.value],
        values={"state": CaseState.CLAIMED.value, "assignee_id": actor_id},
    )
    if claimed is None:
        current = get_case(db, plan_version, case_id)
        if current is not None and current.state == CaseState.CLAIMED.value:
            raise CaseConcurrencyError(f"案件已被 {current.assignee_id} 认领")
        require_transition(CaseState(row.state), CaseState.CLAIMED)
        raise CaseConcurrencyError("案件已被并发修改，请刷新后重试")

    audit = _append_audit(
        claimed,
        action="claim",
        actor_id=actor_id,
        from_state=row.state,
        to_state=CaseState.CLAIMED.value,
        detail="认领案件",
    )
    saved = _save_case(db, claimed, {"audit": audit})
    return _case_to_dict(saved)


def supplement_evidence(
    db: Session,
    *,
    plan_version: str,
    case_id: str,
    note: str,
    attachments: list[str],
    actor_id: str,
    actor_role: str,
) -> dict[str, Any]:
    """补证：向未决案件追加证据条目。"""
    _require_plan(db, plan_version)
    row = _get_case_or_404(db, plan_version, case_id)
    if CaseState(row.state) not in OPEN_STATES:
        raise CaseStateError("已结案件不能补证，请先复开")
    if row.assignee_id:
        require_case_actor(
            actor_id=actor_id,
            actor_role=actor_role,
            assignee_id=row.assignee_id,
            action="补证",
        )
    evidence = [dict(entry) for entry in (row.evidence or [])]
    evidence.append(
        make_evidence_entry(
            sequence=len(evidence) + 1,
            actor_id=actor_id,
            note=note,
            attachments=attachments,
        )
    )
    audit = _append_audit(
        row, action="evidence", actor_id=actor_id, detail=f"补充证据：{note}"
    )
    saved = _save_case(db, row, {"evidence": evidence, "audit": audit})
    return _case_to_dict(saved)


def adjudicate_case(
    db: Session,
    *,
    plan_version: str,
    case_id: str,
    verdict: str,
    reason: str,
    correction: dict[str, Any] | None,
    actor_id: str,
    actor_role: str,
) -> dict[str, Any]:
    """裁决案件；确认时可关联最终修正事件，该事件自此计入学时。"""
    _require_plan(db, plan_version)
    row = _get_case_or_404(db, plan_version, case_id)
    require_transition(CaseState(row.state), CaseState.ADJUDICATED)
    require_case_actor(
        actor_id=actor_id,
        actor_role=actor_role,
        assignee_id=row.assignee_id,
        action="裁决",
    )
    if verdict == Verdict.DISMISSED.value and correction is not None:
        raise CaseError("驳回案件不能关联修正事件")

    resolution_event_id: str | None = None
    absorbed = list(row.absorbed_event_ids or [])
    if correction is not None:
        resolution_event_id = str(correction["event_id"])
        existing = get_event(db, plan_version, resolution_event_id)
        if existing is not None:
            same_event = (
                existing.event_type == "leave_correction"
                and existing.student_id == row.student_id
                and int(existing.payload.get("adjustment_seconds", 0))
                == int(correction["adjustment_seconds"])
            )
            if not same_event:
                raise CaseError("修正事件标识已被其他内容占用")
        else:
            insert_events(
                db,
                plan_version=plan_version,
                events=[
                    {
                        "event_id": resolution_event_id,
                        "event_type": "leave_correction",
                        "student_id": row.student_id,
                        "payload": {
                            "adjustment_seconds": int(
                                correction["adjustment_seconds"]
                            ),
                            "reason": str(
                                correction.get("reason")
                                or f"案件 {case_id} 裁决修正"
                            ),
                        },
                    }
                ],
            )
        if resolution_event_id not in absorbed:
            absorbed.append(resolution_event_id)

    audit = _append_audit(
        row,
        action="adjudicate",
        actor_id=actor_id,
        to_state=CaseState.ADJUDICATED.value,
        detail=f"裁决 {verdict}：{reason}",
    )
    saved = _save_case(
        db,
        row,
        {
            "state": CaseState.ADJUDICATED.value,
            "verdict": verdict,
            "resolution_event_id": resolution_event_id,
            "absorbed_event_ids": absorbed,
            "audit": audit,
        },
    )
    return _case_to_dict(saved)


def reopen_case(
    db: Session,
    *,
    plan_version: str,
    case_id: str,
    reason: str,
    actor_id: str,
    actor_role: str,
) -> dict[str, Any]:
    """复开已裁决案件；原最终修正事件随即挂起，直至重新裁决。"""
    _require_plan(db, plan_version)
    row = _get_case_or_404(db, plan_version, case_id)
    require_transition(CaseState(row.state), CaseState.REOPENED)
    require_case_actor(
        actor_id=actor_id,
        actor_role=actor_role,
        assignee_id=row.assignee_id,
        action="复开",
    )
    audit = _append_audit(
        row,
        action="reopen",
        actor_id=actor_id,
        to_state=CaseState.REOPENED.value,
        detail=f"复开：{reason}",
    )
    saved = _save_case(
        db,
        row,
        {
            "state": CaseState.REOPENED.value,
            "verdict": None,
            "resolution_event_id": None,
            "audit": audit,
        },
    )
    return _case_to_dict(saved)


def merge_cases(
    db: Session,
    *,
    plan_version: str,
    case_ids: list[str],
    reason: str,
    actor_id: str,
    actor_role: str,
) -> dict[str, Any]:
    """合并同一学员的未决案件；来源案件进入终态并保留谱系。"""
    _require_plan(db, plan_version)
    require_admin(actor_role, "合并案件")
    if len(case_ids) < 2 or len(set(case_ids)) != len(case_ids):
        raise CaseError("合并至少需要两个不同的案件")
    rows = [_get_case_or_404(db, plan_version, case_id) for case_id in case_ids]
    if len({row.student_id for row in rows}) != 1:
        raise CaseError("只能合并同一学员的案件")
    for row in rows:
        if CaseState(row.state) not in OPEN_STATES:
            raise CaseStateError("仅未决案件可以合并")

    new_case_id = "C-" + sha256(
        f"{plan_version}|merge:{uuid4().hex}".encode("utf-8")
    ).hexdigest()[:16]
    sources = sorted({e for row in rows for e in (row.source_event_ids or [])})
    absorbed = sorted({e for row in rows for e in (row.absorbed_event_ids or [])})
    note = f"合并 {len(rows)} 个案件：{reason}"
    row = insert_case(
        db,
        values=_new_case_values(
            plan_version=plan_version,
            case_id=new_case_id,
            student_id=rows[0].student_id,
            rule=Rule.MERGED,
            dedup_key=f"merge:{new_case_id}",
            disputed_seconds=sum(r.disputed_seconds for r in rows),
            source_event_ids=sources,
            absorbed_event_ids=absorbed,
            parents=[r.case_id for r in rows],
            actor_id=actor_id,
            action="merge_create",
            note=note,
            evidence_attachments=[r.case_id for r in rows],
        ),
    )
    assert row is not None

    for source in rows:
        lineage = {"parents": [], "children": []}
        lineage.update(source.lineage or {})
        lineage["children"] = list(lineage["children"]) + [new_case_id]
        audit = _append_audit(
            source,
            action="merge_source",
            actor_id=actor_id,
            to_state=CaseState.MERGED.value,
            detail=f"合并入 {new_case_id}：{reason}",
        )
        _save_case(
            db,
            source,
            {"state": CaseState.MERGED.value, "lineage": lineage, "audit": audit},
        )
    merged = get_case(db, plan_version, new_case_id)
    assert merged is not None
    return _case_to_dict(merged)


def split_case(
    db: Session,
    *,
    plan_version: str,
    case_id: str,
    groups: list[dict[str, Any]],
    reason: str,
    actor_id: str,
    actor_role: str,
) -> dict[str, Any]:
    """拆分未决案件；子案件继承规则并记录来源谱系。"""
    _require_plan(db, plan_version)
    require_admin(actor_role, "拆分案件")
    row = _get_case_or_404(db, plan_version, case_id)
    if CaseState(row.state) not in OPEN_STATES:
        raise CaseStateError("仅未决案件可以拆分")
    if len(groups) < 2:
        raise CaseError("拆分至少需要两组来源事件")

    parent_sources = list(row.source_event_ids or [])
    flat = [event_id for group in groups for event_id in group["source_event_ids"]]
    if len(set(flat)) != len(flat) or sorted(flat) != sorted(parent_sources):
        raise CaseError("拆分结果必须完整且不重复地覆盖原案件来源事件")
    if sum(int(group["disputed_seconds"]) for group in groups) != row.disputed_seconds:
        raise CaseError("各组争议秒数之和必须等于原案件")

    child_ids: list[str] = []
    for index, group in enumerate(groups):
        child_id = "C-" + sha256(
            f"{plan_version}|split:{case_id}:{index}:{uuid4().hex}".encode("utf-8")
        ).hexdigest()[:16]
        child_sources = list(group["source_event_ids"])
        child = insert_case(
            db,
            values=_new_case_values(
                plan_version=plan_version,
                case_id=child_id,
                student_id=row.student_id,
                rule=Rule(row.rule),
                dedup_key=f"split:{case_id}:{child_id}",
                disputed_seconds=int(group["disputed_seconds"]),
                source_event_ids=child_sources,
                absorbed_event_ids=child_sources,
                parents=[case_id],
                actor_id=actor_id,
                action="split_create",
                note=f"由案件 {case_id} 拆分：{reason}",
                evidence_attachments=child_sources,
            ),
        )
        assert child is not None
        child_ids.append(child_id)

    lineage = {"parents": [], "children": []}
    lineage.update(row.lineage or {})
    lineage["children"] = list(lineage["children"]) + child_ids
    audit = _append_audit(
        row,
        action="split_parent",
        actor_id=actor_id,
        to_state=CaseState.SPLIT.value,
        detail=f"拆分为 {child_ids}：{reason}",
    )
    saved = _save_case(
        db,
        row,
        {"state": CaseState.SPLIT.value, "lineage": lineage, "audit": audit},
    )
    children = [get_case(db, plan_version, child_id) for child_id in child_ids]
    return {
        "parent": _case_to_dict(saved),
        "children": [
            _case_to_dict(child) for child in children if child is not None
        ],
    }
