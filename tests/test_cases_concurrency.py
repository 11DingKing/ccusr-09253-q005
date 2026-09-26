"""复核案件的并发认领、并发检测与重启恢复。"""

from __future__ import annotations

import threading

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import case_services, repository, services
from app.case_services import Actor
from app.models import Base
from app.review_cases import CaseConflictError, CaseStateError
from tests.conftest import SHANGHAI_PLAN, TestSessionLocal

PV = SHANGHAI_PLAN["plan_version"]
ADMIN = Actor(actor_id="admin-1", role="admin")


def _overlong_event(event_id="E-LONG", student="S2"):
    return {
        "event_id": event_id,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": "A1",
            "activity_type": "regular",
            "check_in_at": "2024-03-15T06:00:00+08:00",
            "check_out_at": "2024-03-15T20:00:00+08:00",
        },
    }


def _setup_plan_with_overlong_case(client) -> str:
    resp = client.post("/api/plans", json=SHANGHAI_PLAN)
    assert resp.status_code == 201, resp.text
    resp = client.post(f"/api/plans/{PV}/events", json={"events": [_overlong_event()]})
    assert resp.status_code == 201, resp.text
    resp = client.post(
        f"/api/plans/{PV}/cases/detect",
        json={},
        headers={"X-Actor-Id": "admin-1", "X-Actor-Role": "admin"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["created"][0]["case_id"]


def test_concurrent_claim_only_one_reviewer_wins(client):
    case_id = _setup_plan_with_overlong_case(client)

    won: list[str] = []
    lost: list[str] = []
    lock = threading.Lock()

    def _claim(actor_id: str) -> None:
        session = TestSessionLocal()
        try:
            case_services.claim_case(
                session,
                plan_version=PV,
                case_id=case_id,
                actor=Actor(actor_id=actor_id, role="reviewer"),
            )
            with lock:
                won.append(actor_id)
        except (CaseConflictError, CaseStateError):
            # 竞争失败：要么读到他人已认领的状态，要么条件更新落空。
            with lock:
                lost.append(actor_id)
        finally:
            session.close()

    threads = [
        threading.Thread(target=_claim, args=(f"rev-{i}",)) for i in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # 恰好一人认领成功，其余全部冲突。
    assert len(won) == 1
    assert len(lost) == 5

    session = TestSessionLocal()
    try:
        case = case_services.get_case(session, PV, case_id)
    finally:
        session.close()
    assert case["state"] == "claimed"
    assert case["assignee_id"] == won[0]
    assert case["version"] == 2
    # 审计历史只有一次认领记录，不会出现并发写撕裂。
    assert [h["action"] for h in case["history"]] == ["detect", "claim"]


def test_concurrent_detect_creates_each_case_once(client):
    resp = client.post("/api/plans", json=SHANGHAI_PLAN)
    assert resp.status_code == 201, resp.text
    events = [
        _overlong_event(),
        {
            "event_id": "E-NEG",
            "event_type": "leave_correction",
            "student_id": "S1",
            "payload": {"adjustment_seconds": -1800, "reason": "迟到"},
        },
        {
            "event_id": "E-OV1",
            "event_type": "checkin",
            "student_id": "S3",
            "payload": {
                "activity_id": "A1",
                "activity_type": "regular",
                "check_in_at": "2024-03-15T08:00:00+08:00",
                "check_out_at": "2024-03-15T10:00:00+08:00",
            },
        },
        {
            "event_id": "E-OV2",
            "event_type": "checkin",
            "student_id": "S3",
            "payload": {
                "activity_id": "A1",
                "activity_type": "regular",
                "check_in_at": "2024-03-15T09:00:00+08:00",
                "check_out_at": "2024-03-15T11:00:00+08:00",
            },
        },
    ]
    resp = client.post(f"/api/plans/{PV}/events", json={"events": events})
    assert resp.status_code == 201, resp.text

    created_ids: list[str] = []
    lock = threading.Lock()

    def _detect() -> None:
        session = TestSessionLocal()
        try:
            result = case_services.detect_cases(
                session, plan_version=PV, actor=ADMIN
            )
            with lock:
                created_ids.extend(c["case_id"] for c in result["created"])
        finally:
            session.close()

    threads = [threading.Thread(target=_detect) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # 4 个并发检测合计只立案 3 次，且案件 id 互不重复。
    assert len(created_ids) == 3
    assert len(set(created_ids)) == 3

    session = TestSessionLocal()
    try:
        cases = case_services.list_cases(session, PV)
    finally:
        session.close()
    assert len(cases) == 3


def test_case_state_survives_restart(tmp_path):
    """模拟服务重启：新引擎、新会话，案件状态与后续流转不受影响。"""
    db_file = tmp_path / "restart_cases.db"
    url = f"sqlite:///{db_file}"

    engine_one = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine_one)
    session_one = sessionmaker(bind=engine_one, autoflush=False, autocommit=False)()

    repository.upsert_plan(
        session_one,
        plan_version=PV,
        iana_timezone=SHANGHAI_PLAN["iana_timezone"],
        required_seconds=SHANGHAI_PLAN["required_seconds"],
    )
    repository.insert_events(session_one, plan_version=PV, events=[_overlong_event()])
    detected = case_services.detect_cases(session_one, plan_version=PV, actor=ADMIN)
    case_id = detected["created"][0]["case_id"]
    case_services.claim_case(session_one, plan_version=PV, case_id=case_id, actor=ADMIN)
    case_services.add_evidence(
        session_one,
        plan_version=PV,
        case_id=case_id,
        actor=ADMIN,
        note="导师确认 18:00 离岗",
        attachments=["mentor-note-1"],
    )
    session_one.close()
    engine_one.dispose()

    # “重启”：同一数据库文件上的全新引擎与会话。
    engine_two = create_engine(url, connect_args={"check_same_thread": False})
    session_two = sessionmaker(bind=engine_two, autoflush=False, autocommit=False)()

    case = case_services.get_case(session_two, PV, case_id)
    assert case["state"] == "claimed"
    assert case["assignee_id"] == "admin-1"
    assert case["evidence"][0]["note"] == "导师确认 18:00 离岗"
    assert [h["action"] for h in case["history"]] == ["detect", "claim", "evidence"]

    # 重启后状态机继续流转：裁决落地修正事件，快照反映最新总学时。
    resolved = case_services.adjudicate_case(
        session_two,
        plan_version=PV,
        case_id=case_id,
        actor=ADMIN,
        verdict="upheld",
        reason="按离岗时间封顶",
    )
    assert resolved["state"] == "resolved"
    assert resolved["applied_correction_seconds"] == -7200

    snapshot = services.current_snapshot(session_two, PV)
    student = snapshot.students[0]
    assert student["total_seconds"] == 12 * 3600
    assert student["adjustments"][0]["case_id"] == case_id
    assert student["pending_cases"] == []
    assert snapshot.pending_cases == []

    session_two.close()
    engine_two.dispose()
