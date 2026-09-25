"""异常学时复核案件：状态机、检测、权限、并发与恢复测试。"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import services
from app.compliance import anomaly_cases as domain
from tests.conftest import SHANGHAI_PLAN, TestSessionLocal, test_engine

PV = SHANGHAI_PLAN["plan_version"]
REVIEWER = {"X-Actor-Id": "reviewer-1"}
REVIEWER2 = {"X-Actor-Id": "reviewer-2"}
ADMIN = {"X-Actor-Id": "admin-1", "X-Actor-Role": "admin"}


def _create_plan(client):
    resp = client.post("/api/plans", json=SHANGHAI_PLAN)
    assert resp.status_code == 201, resp.text


def _checkin(eid, student, start, end, activity_type="regular"):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": "A1",
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
    }


def _correction(eid, student, seconds, reason=""):
    return {
        "event_id": eid,
        "event_type": "leave_correction",
        "student_id": student,
        "payload": {"adjustment_seconds": seconds, "reason": reason},
    }


def _import(client, events):
    resp = client.post(f"/api/plans/{PV}/events", json={"events": events})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _detect(client, **overrides):
    body = {"max_checkin_seconds": 43200}
    body.update(overrides)
    resp = client.post(f"/api/plans/{PV}/cases/detect", json=body, headers=REVIEWER)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _seed_negative_case(client, seconds=-1800):
    """两名学员各一条 2 小时签到，S1 另有一笔负向修正；返回案件。"""
    _create_plan(client)
    _import(
        client,
        [
            _checkin(
                "E-01", "S1",
                "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00",
            ),
            _correction("E-NEG", "S1", seconds, "迟到扣减"),
        ],
    )
    return _detect(client)["created"][0]


def _claim(client, case_id, headers=REVIEWER):
    resp = client.post(f"/api/plans/{PV}/cases/{case_id}/claim", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _progress(client, student="S1"):
    resp = client.get(f"/api/plans/{PV}/students/{student}/progress")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_detect_files_each_anomaly_once_and_annotates_snapshot(client):
    _create_plan(client)
    _import(
        client,
        [
            _checkin(
                "E-OK", "S1",
                "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00",
            ),
            _correction("E-NEG", "S1", -1800, "迟到扣减"),
            _checkin(  # 14 小时，超出 12 小时上限
                "E-BIG", "S2",
                "2024-03-15T06:00:00+08:00", "2024-03-15T20:00:00+08:00",
            ),
            _checkin(
                "E-O1", "S3",
                "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00",
            ),
            _checkin(
                "E-O2", "S3",
                "2024-03-15T09:00:00+08:00", "2024-03-15T11:00:00+08:00",
            ),
        ],
    )
    result = _detect(client)
    assert result["scanned_events"] == 5
    assert result["existing"] == []
    by_rule = {case["rule"]: case for case in result["created"]}
    assert set(by_rule) == {
        "negative_adjustment",
        "oversized_checkin",
        "overlapping_activity",
    }
    assert by_rule["negative_adjustment"]["source_event_ids"] == ["E-NEG"]
    assert by_rule["negative_adjustment"]["disputed_seconds"] == 1800
    assert by_rule["oversized_checkin"]["disputed_seconds"] == 14 * 3600 - 43200
    assert by_rule["overlapping_activity"]["disputed_seconds"] == 3600
    assert by_rule["overlapping_activity"]["source_event_ids"] == ["E-O1", "E-O2"]
    for case in result["created"]:
        assert case["state"] == "detected"
        assert case["assignee_id"] is None
        assert case["evidence"][0]["sequence"] == 1
        assert domain.verify_audit_chain(case["audit"])

    # 同一异常不得重复立案。
    again = _detect(client)
    assert again["created"] == []
    assert len(again["existing"]) == 3

    # 列表与详情查询。
    listed = client.get(f"/api/plans/{PV}/cases").json()
    assert len(listed) == 3
    only_s3 = client.get(f"/api/plans/{PV}/cases?student_id=S3").json()
    assert [c["rule"] for c in only_s3] == ["overlapping_activity"]
    detail = client.get(
        f"/api/plans/{PV}/cases/{by_rule['negative_adjustment']['case_id']}"
    ).json()
    assert detail["dedup_key"] == "negative_adjustment:E-NEG"

    # 未决案件在快照中清晰标注，但挂起的负向修正不计入已确认学时。
    snap = client.get(f"/api/plans/{PV}/snapshot").json()
    students = {s["student_id"]: s for s in snap["students"]}
    s1 = students["S1"]
    assert s1["confirmed_seconds"] == 7200
    assert s1["adjustment_seconds"] == 0
    assert s1["total_seconds"] == 7200
    assert [h["event_id"] for h in s1["held_adjustments"]] == ["E-NEG"]
    assert s1["held_adjustments"][0]["case_id"] == (
        by_rule["negative_adjustment"]["case_id"]
    )
    assert s1["pending_review_seconds"] == 1800
    assert [c["case_id"] for c in s1["open_cases"]] == [
        by_rule["negative_adjustment"]["case_id"]
    ]
    # 超长与重叠只标注争议秒数，不暂扣已确认学时。
    assert students["S2"]["confirmed_seconds"] == 14 * 3600
    assert students["S2"]["pending_review_seconds"] == 14 * 3600 - 43200
    assert students["S3"]["confirmed_seconds"] == 3 * 3600
    assert students["S3"]["pending_review_seconds"] == 3600


def test_claim_supplement_adjudicate_applies_final_correction(client):
    case = _seed_negative_case(client)
    cid = case["case_id"]

    claimed = _claim(client, cid)
    assert claimed["state"] == "claimed"
    assert claimed["assignee_id"] == "reviewer-1"

    # 同一处理人重复认领是幂等的。
    resp = client.post(f"/api/plans/{PV}/cases/{cid}/claim", headers=REVIEWER)
    assert resp.status_code == 200

    supplemented = client.post(
        f"/api/plans/{PV}/cases/{cid}/evidence",
        headers=REVIEWER,
        json={"note": "导师确认迟到 30 分钟", "attachments": ["doc://mentor-note"]},
    ).json()
    assert len(supplemented["evidence"]) == 2
    assert supplemented["evidence"][1]["note"] == "导师确认迟到 30 分钟"
    assert supplemented["evidence"][1]["attachments"] == ["doc://mentor-note"]

    adjudicated = client.post(
        f"/api/plans/{PV}/cases/{cid}/adjudicate",
        headers=REVIEWER,
        json={
            "verdict": "confirmed",
            "reason": "证据充分",
            "correction": {
                "event_id": "E-FIX",
                "adjustment_seconds": -1800,
                "reason": "案件裁决扣减",
            },
        },
    ).json()
    assert adjudicated["state"] == "adjudicated"
    assert adjudicated["verdict"] == "confirmed"
    assert adjudicated["resolution_event_id"] == "E-FIX"
    assert set(adjudicated["absorbed_event_ids"]) == {"E-NEG", "E-FIX"}
    assert domain.verify_audit_chain(adjudicated["audit"])
    assert [e["action"] for e in adjudicated["audit"]] == [
        "detect",
        "claim",
        "evidence",
        "adjudicate",
    ]

    # 最终修正事件计入学时；原负向修正被案件吸收，不再重复扣减。
    progress = _progress(client)
    assert progress["adjustment_seconds"] == -1800
    assert progress["total_seconds"] == 7200 - 1800
    assert [a["event_id"] for a in progress["adjustments"]] == ["E-FIX"]
    assert [h["event_id"] for h in progress["held_adjustments"]] == ["E-NEG"]
    assert progress["open_cases"] == []
    assert progress["pending_review_seconds"] == 0

    # 裁决后再次检测不会为 E-FIX 立案（它已被案件吸收）。
    assert _detect(client)["created"] == []


def test_adjudicate_dismissed_keeps_source_out_of_totals(client):
    case = _seed_negative_case(client)
    cid = case["case_id"]
    _claim(client, cid)

    # 驳回不允许附带修正事件。
    bad = client.post(
        f"/api/plans/{PV}/cases/{cid}/adjudicate",
        headers=REVIEWER,
        json={
            "verdict": "dismissed",
            "reason": "误报",
            "correction": {"event_id": "E-X", "adjustment_seconds": -1},
        },
    )
    assert bad.status_code == 400

    dismissed = client.post(
        f"/api/plans/{PV}/cases/{cid}/adjudicate",
        headers=REVIEWER,
        json={"verdict": "dismissed", "reason": "误报"},
    ).json()
    assert dismissed["verdict"] == "dismissed"
    assert dismissed["resolution_event_id"] is None

    progress = _progress(client)
    assert progress["adjustment_seconds"] == 0
    assert progress["total_seconds"] == 7200
    assert [h["event_id"] for h in progress["held_adjustments"]] == ["E-NEG"]
    assert progress["open_cases"] == []

    # 驳回后再次检测不会重复立案。
    assert _detect(client)["created"] == []


def test_reopen_supersedes_resolution_until_readjudicated(client):
    case = _seed_negative_case(client)
    cid = case["case_id"]
    _claim(client, cid)
    client.post(
        f"/api/plans/{PV}/cases/{cid}/adjudicate",
        headers=REVIEWER,
        json={
            "verdict": "confirmed",
            "reason": "确认",
            "correction": {"event_id": "E-FIX1", "adjustment_seconds": -1800},
        },
    )
    assert _progress(client)["total_seconds"] == 7200 - 1800

    reopened = client.post(
        f"/api/plans/{PV}/cases/{cid}/reopen",
        headers=ADMIN,
        json={"reason": "学员申诉"},
    ).json()
    assert reopened["state"] == "reopened"
    assert reopened["verdict"] is None
    assert reopened["resolution_event_id"] is None

    # 复开后原最终修正事件随即挂起，不再计入学时。
    progress = _progress(client)
    assert progress["total_seconds"] == 7200
    assert sorted(h["event_id"] for h in progress["held_adjustments"]) == [
        "E-FIX1",
        "E-NEG",
    ]
    assert progress["open_cases"][0]["state"] == "reopened"

    # 复开期间重新检测不会为挂起事件重复立案。
    assert _detect(client)["created"] == []

    _claim(client, cid)
    final = client.post(
        f"/api/plans/{PV}/cases/{cid}/adjudicate",
        headers=REVIEWER,
        json={
            "verdict": "confirmed",
            "reason": "改判",
            "correction": {"event_id": "E-FIX2", "adjustment_seconds": -900},
        },
    ).json()
    assert final["resolution_event_id"] == "E-FIX2"
    assert domain.verify_audit_chain(final["audit"])
    assert [e["action"] for e in final["audit"]] == [
        "detect",
        "claim",
        "adjudicate",
        "reopen",
        "claim",
        "adjudicate",
    ]

    progress = _progress(client)
    assert progress["adjustment_seconds"] == -900
    assert progress["total_seconds"] == 7200 - 900
    assert [a["event_id"] for a in progress["adjustments"]] == ["E-FIX2"]
    assert sorted(h["event_id"] for h in progress["held_adjustments"]) == [
        "E-FIX1",
        "E-NEG",
    ]


def test_merge_cases_preserves_lineage_and_blocks_refiling(client):
    _create_plan(client)
    _import(
        client,
        [
            _checkin(
                "E-01", "S1",
                "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00",
            ),
            _correction("E-N1", "S1", -600, "迟到"),
            _correction("E-N2", "S1", -900, "早退"),
        ],
    )
    created = _detect(client)["created"]
    assert len(created) == 2
    ids = [c["case_id"] for c in created]

    # 普通处理人无权合并。
    denied = client.post(
        f"/api/plans/{PV}/cases/merge",
        headers=REVIEWER,
        json={"case_ids": ids, "reason": "并案处理"},
    )
    assert denied.status_code == 403

    merged = client.post(
        f"/api/plans/{PV}/cases/merge",
        headers=ADMIN,
        json={"case_ids": ids, "reason": "并案处理"},
    ).json()
    assert merged["rule"] == "merged"
    assert merged["state"] == "detected"
    assert merged["disputed_seconds"] == 1500
    assert merged["source_event_ids"] == ["E-N1", "E-N2"]
    assert sorted(merged["lineage"]["parents"]) == sorted(ids)

    # 来源案件进入终态并指向合并后的案件，原始来源仍可追踪。
    for source_id in ids:
        source = client.get(f"/api/plans/{PV}/cases/{source_id}").json()
        assert source["state"] == "merged"
        assert source["lineage"]["children"] == [merged["case_id"]]
        assert source["source_event_ids"]  # 原始来源保留

    # 合并后同一异常不会重复立案。
    assert _detect(client)["created"] == []

    snap = client.get(f"/api/plans/{PV}/snapshot").json()
    s1 = snap["students"][0]
    assert s1["pending_review_seconds"] == 1500
    assert len(s1["open_cases"]) == 1
    assert s1["total_seconds"] == 7200


def test_split_case_preserves_lineage(client):
    _create_plan(client)
    _import(
        client,
        [
            _checkin(
                "E-01", "S1",
                "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00",
            ),
            _correction("E-N1", "S1", -600, "迟到"),
            _correction("E-N2", "S1", -900, "早退"),
        ],
    )
    ids = [c["case_id"] for c in _detect(client)["created"]]
    merged = client.post(
        f"/api/plans/{PV}/cases/merge",
        headers=ADMIN,
        json={"case_ids": ids, "reason": "并案"},
    ).json()

    # 普通处理人无权拆分。
    denied = client.post(
        f"/api/plans/{PV}/cases/{merged['case_id']}/split",
        headers=REVIEWER,
        json={
            "groups": [
                {"source_event_ids": ["E-N1"], "disputed_seconds": 600},
                {"source_event_ids": ["E-N2"], "disputed_seconds": 900},
            ],
            "reason": "分开复核",
        },
    )
    assert denied.status_code == 403

    # 覆盖不完整会被拒绝。
    incomplete = client.post(
        f"/api/plans/{PV}/cases/{merged['case_id']}/split",
        headers=ADMIN,
        json={
            "groups": [
                {"source_event_ids": ["E-N1"], "disputed_seconds": 600},
                {"source_event_ids": ["E-N1"], "disputed_seconds": 900},
            ],
            "reason": "错误拆分",
        },
    )
    assert incomplete.status_code == 400

    result = client.post(
        f"/api/plans/{PV}/cases/{merged['case_id']}/split",
        headers=ADMIN,
        json={
            "groups": [
                {"source_event_ids": ["E-N1"], "disputed_seconds": 600},
                {"source_event_ids": ["E-N2"], "disputed_seconds": 900},
            ],
            "reason": "分开复核",
        },
    ).json()
    parent = result["parent"]
    assert parent["state"] == "split"
    assert len(result["children"]) == 2
    by_sources = {
        tuple(child["source_event_ids"]): child for child in result["children"]
    }
    assert by_sources[("E-N1",)]["disputed_seconds"] == 600
    assert by_sources[("E-N2",)]["disputed_seconds"] == 900
    for child in result["children"]:
        assert child["lineage"]["parents"] == [merged["case_id"]]
        assert child["state"] == "detected"
    assert sorted(parent["lineage"]["children"]) == sorted(
        child["case_id"] for child in result["children"]
    )

    # 拆分后同一异常仍不会重复立案。
    assert _detect(client)["created"] == []

    snap = client.get(f"/api/plans/{PV}/snapshot").json()
    assert snap["students"][0]["pending_review_seconds"] == 1500
    assert len(snap["students"][0]["open_cases"]) == 2


def test_permission_isolation(client):
    case = _seed_negative_case(client)
    cid = case["case_id"]

    # 缺少操作人身份。
    missing = client.post(f"/api/plans/{PV}/cases/{cid}/claim")
    assert missing.status_code == 400
    bad_role = client.post(
        f"/api/plans/{PV}/cases/{cid}/claim",
        headers={"X-Actor-Id": "x", "X-Actor-Role": "root"},
    )
    assert bad_role.status_code == 400

    _claim(client, cid, headers=REVIEWER)

    # 他人已被认领的案件：非处理人补证、裁决均被拒绝。
    denied_evidence = client.post(
        f"/api/plans/{PV}/cases/{cid}/evidence",
        headers=REVIEWER2,
        json={"note": "越权补证"},
    )
    assert denied_evidence.status_code == 403
    denied_adjudicate = client.post(
        f"/api/plans/{PV}/cases/{cid}/adjudicate",
        headers=REVIEWER2,
        json={"verdict": "dismissed", "reason": "越权裁决"},
    )
    assert denied_adjudicate.status_code == 403

    # 他人重复认领返回冲突。
    conflict = client.post(f"/api/plans/{PV}/cases/{cid}/claim", headers=REVIEWER2)
    assert conflict.status_code == 409

    # 管理员可代为补证；处理人本人可裁决。
    ok_evidence = client.post(
        f"/api/plans/{PV}/cases/{cid}/evidence",
        headers=ADMIN,
        json={"note": "管理员补证"},
    )
    assert ok_evidence.status_code == 200
    ok_adjudicate = client.post(
        f"/api/plans/{PV}/cases/{cid}/adjudicate",
        headers=REVIEWER,
        json={"verdict": "dismissed", "reason": "误报"},
    )
    assert ok_adjudicate.status_code == 200

    # 已结案件不能补证；未认领案件不能裁决。
    closed = client.post(
        f"/api/plans/{PV}/cases/{cid}/evidence",
        headers=ADMIN,
        json={"note": "太迟了"},
    )
    assert closed.status_code == 409


def test_adjudicate_requires_claimed_state(client):
    case = _seed_negative_case(client)
    resp = client.post(
        f"/api/plans/{PV}/cases/{case['case_id']}/adjudicate",
        headers=REVIEWER,
        json={"verdict": "dismissed", "reason": "未认领就裁决"},
    )
    assert resp.status_code == 409


def test_case_not_found_and_plan_not_found(client):
    _create_plan(client)
    assert client.get(f"/api/plans/{PV}/cases/C-nope").status_code == 404
    resp = client.post(f"/api/plans/{PV}/cases/C-nope/claim", headers=REVIEWER)
    assert resp.status_code == 404
    resp = client.post("/api/plans/NOPE/cases/detect", json={}, headers=REVIEWER)
    assert resp.status_code == 404
    resp = client.get("/api/plans/NOPE/cases")
    assert resp.status_code == 404


def test_freeze_marks_open_cases_without_counting_them(client):
    case = _seed_negative_case(client)
    cid = case["case_id"]

    frozen = client.post(f"/api/plans/{PV}/freezes/F-1", json={}).json()
    s1 = frozen["students"][0]
    assert s1["total_seconds"] == 7200
    assert s1["adjustment_seconds"] == 0
    assert s1["pending_review_seconds"] == 1800
    assert s1["open_cases"][0]["case_id"] == cid
    assert s1["open_cases"][0]["state"] == "detected"
    assert s1["held_adjustments"][0]["event_id"] == "E-NEG"

    # 冻结后裁决结案：冻结快照保持不变，实时快照反映最终修正。
    _claim(client, cid)
    client.post(
        f"/api/plans/{PV}/cases/{cid}/adjudicate",
        headers=REVIEWER,
        json={
            "verdict": "confirmed",
            "reason": "确认",
            "correction": {"event_id": "E-FIX", "adjustment_seconds": -1800},
        },
    )
    frozen_again = client.get(f"/api/plans/{PV}/freezes/F-1").json()
    assert frozen_again["students"][0]["total_seconds"] == 7200
    assert frozen_again["students"][0]["open_cases"][0]["state"] == "detected"

    live = client.get(f"/api/plans/{PV}/snapshot").json()
    assert live["students"][0]["total_seconds"] == 7200 - 1800
    assert live["students"][0]["open_cases"] == []

    # 新冻结版本反映结案结果，差异可查。
    client.post(f"/api/plans/{PV}/freezes/F-2", json={})
    diff = client.get(f"/api/plans/{PV}/freezes/F-1/diff/F-2").json()
    change = diff["student_changes"][0]
    assert change["fields"]["total_seconds"] == {"before": 7200, "after": 5400}
    assert change["fields"]["pending_review_seconds"] == {"before": 1800, "after": 0}


def test_concurrent_claim_only_one_wins(client):
    case = _seed_negative_case(client)
    cid = case["case_id"]

    won: list[str] = []
    lost: list[str] = []
    lock = threading.Lock()

    def _claim(actor: str) -> None:
        session = TestSessionLocal()
        try:
            services.claim_case(
                session,
                plan_version=PV,
                case_id=cid,
                actor_id=actor,
                actor_role="reviewer",
            )
            with lock:
                won.append(actor)
        except domain.CaseConcurrencyError:
            with lock:
                lost.append(actor)
        finally:
            session.close()

    threads = [
        threading.Thread(target=_claim, args=(f"reviewer-{i}",)) for i in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(won) == 1
    assert len(lost) == 3
    session = TestSessionLocal()
    try:
        detail = services.get_case_detail(session, PV, cid)
        assert detail["state"] == "claimed"
        assert detail["assignee_id"] == won[0]
        assert domain.verify_audit_chain(detail["audit"])
    finally:
        session.close()


def test_case_workflow_survives_restart(client):
    case = _seed_negative_case(client)
    cid = case["case_id"]
    _claim(client, cid)

    # 模拟服务重启：丢弃全部连接，重新打开同一个 SQLite 文件继续办理。
    test_engine.dispose()
    engine2 = create_engine(
        "sqlite:///./practice_hours_test.db",
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )
    session2 = sessionmaker(bind=engine2, autoflush=False, autocommit=False)()
    try:
        detail = services.get_case_detail(session2, PV, cid)
        assert detail["state"] == "claimed"
        assert detail["assignee_id"] == "reviewer-1"
        assert domain.verify_audit_chain(detail["audit"])

        detail = services.supplement_evidence(
            session2,
            plan_version=PV,
            case_id=cid,
            note="重启后补证",
            attachments=[],
            actor_id="reviewer-1",
            actor_role="reviewer",
        )
        detail = services.adjudicate_case(
            session2,
            plan_version=PV,
            case_id=cid,
            verdict="confirmed",
            reason="重启后裁决",
            correction={"event_id": "E-FIX", "adjustment_seconds": -1800},
            actor_id="reviewer-1",
            actor_role="reviewer",
        )
        assert detail["state"] == "adjudicated"
        assert detail["resolution_event_id"] == "E-FIX"
        assert domain.verify_audit_chain(detail["audit"])
        assert [e["action"] for e in detail["audit"]] == [
            "detect",
            "claim",
            "evidence",
            "adjudicate",
        ]

        snap = services.current_snapshot(session2, PV)
        s1 = [s for s in snap.students if s["student_id"] == "S1"][0]
        assert s1["adjustment_seconds"] == -1800
        assert s1["total_seconds"] == 7200 - 1800
        assert s1["open_cases"] == []
    finally:
        session2.close()
        engine2.dispose()
