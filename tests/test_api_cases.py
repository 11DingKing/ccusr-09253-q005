"""复核案件 API：检测、认领、补证、裁决、复开、合并、拆分与冻结标注。"""

from __future__ import annotations

from tests.conftest import SHANGHAI_PLAN

REVIEWER = {"X-Actor-Id": "rev-1", "X-Actor-Role": "reviewer"}
REVIEWER_2 = {"X-Actor-Id": "rev-2", "X-Actor-Role": "reviewer"}
ADMIN = {"X-Actor-Id": "admin-1", "X-Actor-Role": "admin"}

PV = SHANGHAI_PLAN["plan_version"]


def _create_plan(client):
    resp = client.post("/api/plans", json=SHANGHAI_PLAN)
    assert resp.status_code == 201, resp.text


def _import(client, events):
    resp = client.post(f"/api/plans/{PV}/events", json={"events": events})
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


def _detect(client, headers=ADMIN, body=None):
    resp = client.post(f"/api/plans/{PV}/cases/detect", json=body or {}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _seed_all_anomalies(client):
    """负向修正 + 14 小时超长签到 + 一对重叠签到 + 一条正常签到。"""
    _import(
        client,
        [
            _correction("E-NEG", "S1", -1800, "迟到扣减"),
            _checkin("E-LONG", "S2", "2024-03-15T06:00:00+08:00", "2024-03-15T20:00:00+08:00"),
            _checkin("E-OV1", "S3", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
            _checkin("E-OV2", "S3", "2024-03-15T09:00:00+08:00", "2024-03-15T11:00:00+08:00"),
            _checkin("E-OK", "S4", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
        ],
    )


def _cases_by_rule(client):
    resp = client.get(f"/api/plans/{PV}/cases", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    return {c["rule"]: c for c in resp.json()}


def _claim(client, case_id, headers=REVIEWER):
    return client.post(f"/api/plans/{PV}/cases/{case_id}/claim", headers=headers)


def _adjudicate(client, case_id, body, headers=REVIEWER):
    return client.post(
        f"/api/plans/{PV}/cases/{case_id}/adjudicate", json=body, headers=headers
    )


def _progress(client, student):
    resp = client.get(f"/api/plans/{PV}/students/{student}/progress")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_detect_covers_three_rule_kinds_and_never_duplicates(client):
    _create_plan(client)
    _seed_all_anomalies(client)

    result = _detect(client)
    assert result["total_anomalies"] == 3
    assert result["existing"] == []
    assert len(result["created"]) == 3

    by_rule = {c["rule"]: c for c in result["created"]}
    assert set(by_rule) == {
        "negative_correction",
        "overlong_checkin",
        "overlapping_activities",
    }
    negative = by_rule["negative_correction"]
    assert negative["student_id"] == "S1"
    assert negative["source_event_ids"] == ["E-NEG"]
    assert negative["suggested_correction_seconds"] == 1800
    assert negative["state"] == "detected"
    assert negative["history"][0]["action"] == "detect"

    overlong = by_rule["overlong_checkin"]
    assert overlong["source_event_ids"] == ["E-LONG"]
    # 14h 签到，阈值 12h，建议修正 -(2h)。
    assert overlong["suggested_correction_seconds"] == -7200

    overlap = by_rule["overlapping_activities"]
    assert overlap["student_id"] == "S3"
    assert overlap["source_event_ids"] == ["E-OV1", "E-OV2"]
    assert overlap["suggested_correction_seconds"] == 0

    # 同一异常不得重复立案：再次检测只返回已存在案件。
    again = _detect(client)
    assert again["total_anomalies"] == 3
    assert again["created"] == []
    assert sorted(again["existing"]) == sorted(c["case_id"] for c in result["created"])

    # 案件列表与过滤。
    resp = client.get(f"/api/plans/{PV}/cases?student_id=S3", headers=REVIEWER)
    assert [c["rule"] for c in resp.json()] == ["overlapping_activities"]
    resp = client.get(f"/api/plans/{PV}/cases?state=detected", headers=REVIEWER)
    assert len(resp.json()) == 3
    resp = client.get(f"/api/plans/{PV}/cases?state=bogus", headers=REVIEWER)
    assert resp.status_code == 400


def test_overlong_threshold_is_configurable(client):
    _create_plan(client)
    _import(
        client,
        [_checkin("E-1", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00")],
    )
    assert _detect(client)["total_anomalies"] == 0
    result = _detect(client, body={"overlong_threshold_seconds": 3600})
    assert result["total_anomalies"] == 1
    assert result["created"][0]["suggested_correction_seconds"] == -(4 * 3600 - 3600)


def test_full_lifecycle_links_correction_event_and_moves_totals(client):
    _create_plan(client)
    _import(
        client,
        [_checkin("E-LONG", "S2", "2024-03-15T06:00:00+08:00", "2024-03-15T20:00:00+08:00")],
    )
    assert _progress(client, "S2")["total_seconds"] == 14 * 3600

    case_id = _detect(client)["created"][0]["case_id"]

    resp = _claim(client, case_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "claimed"
    assert resp.json()["assignee_id"] == "rev-1"

    resp = client.post(
        f"/api/plans/{PV}/cases/{case_id}/evidence",
        json={"note": "导师确认 18:00 已离岗", "attachments": ["mentor-note-1"]},
        headers=REVIEWER,
    )
    assert resp.status_code == 200, resp.text
    evidence = resp.json()["evidence"]
    assert len(evidence) == 1
    assert evidence[0]["note"] == "导师确认 18:00 已离岗"
    assert evidence[0]["attachments"] == ["mentor-note-1"]
    assert evidence[0]["actor_id"] == "rev-1"

    resp = _adjudicate(client, case_id, {"verdict": "upheld", "reason": "按离岗时间封顶"})
    assert resp.status_code == 200, resp.text
    case = resp.json()
    assert case["state"] == "resolved"
    assert case["resolution"] == "upheld"
    assert case["applied_correction_seconds"] == -7200
    assert len(case["correction_event_ids"]) == 1

    # 最终修正事件进入事件流并关联案件，总学时随之调整。
    progress = _progress(client, "S2")
    assert progress["total_seconds"] == 14 * 3600 - 7200
    assert progress["adjustments"][0]["seconds"] == -7200
    assert progress["adjustments"][0]["case_id"] == case_id
    assert progress["adjustments"][0]["event_id"] == case["correction_event_ids"][0]

    actions = [h["action"] for h in case["history"]]
    assert actions == ["detect", "claim", "evidence", "adjudicate"]


def test_rejected_verdict_leaves_totals_untouched(client):
    _create_plan(client)
    _import(client, [_correction("E-NEG", "S1", -1800, "迟到扣减")])
    case_id = _detect(client)["created"][0]["case_id"]
    assert _claim(client, case_id).status_code == 200

    resp = _adjudicate(
        client, case_id, {"verdict": "rejected", "reason": "扣减依据充分"}
    )
    assert resp.status_code == 200, resp.text
    case = resp.json()
    assert case["state"] == "dismissed"
    assert case["resolution"] == "rejected"
    assert case["correction_event_ids"] == []
    assert case["applied_correction_seconds"] == 0

    progress = _progress(client, "S1")
    assert progress["adjustment_seconds"] == -1800
    assert all(a["case_id"] is None for a in progress["adjustments"])


def test_reopen_then_readjudicate_converges_correction(client):
    _create_plan(client)
    _import(
        client,
        [_checkin("E-LONG", "S2", "2024-03-15T06:00:00+08:00", "2024-03-15T20:00:00+08:00")],
    )
    case_id = _detect(client)["created"][0]["case_id"]
    _claim(client, case_id)
    _adjudicate(client, case_id, {"verdict": "upheld", "reason": "封顶 12 小时"})
    assert _progress(client, "S2")["total_seconds"] == 12 * 3600

    # 复开需要管理员；复开后清空处理人与裁决结果。
    resp = client.post(
        f"/api/plans/{PV}/cases/{case_id}/reopen",
        json={"reason": "学院抽查要求复审"},
        headers=ADMIN,
    )
    assert resp.status_code == 200, resp.text
    case = resp.json()
    assert case["state"] == "reopened"
    assert case["assignee_id"] is None
    assert case["resolution"] is None

    # 另一位复核员认领并改判为不修正：差额 +7200 使总学时收敛回 14h。
    assert _claim(client, case_id, headers=REVIEWER_2).status_code == 200
    resp = _adjudicate(
        client,
        case_id,
        {"verdict": "upheld", "reason": "认定为连续实训", "correction_seconds": 0},
        headers=REVIEWER_2,
    )
    case = resp.json()
    assert case["applied_correction_seconds"] == 0
    assert len(case["correction_event_ids"]) == 2
    assert _progress(client, "S2")["total_seconds"] == 14 * 3600


def test_permission_isolation(client):
    _create_plan(client)
    _seed_all_anomalies(client)
    _detect(client)
    by_rule = _cases_by_rule(client)
    overlong_id = by_rule["overlong_checkin"]["case_id"]
    negative_id = by_rule["negative_correction"]["case_id"]

    # 未携带身份头 -> 401。
    assert client.post(f"/api/plans/{PV}/cases/detect", json={}).status_code == 401
    assert client.post(f"/api/plans/{PV}/cases/{overlong_id}/claim").status_code == 401
    assert client.get(f"/api/plans/{PV}/cases").status_code == 401
    resp = client.post(
        f"/api/plans/{PV}/cases/detect", json={}, headers={"X-Actor-Id": "x", "X-Actor-Role": "student"}
    )
    assert resp.status_code == 401

    # rev-1 认领后，rev-2 不能补证、不能裁决。
    assert _claim(client, overlong_id, headers=REVIEWER).status_code == 200
    resp = client.post(
        f"/api/plans/{PV}/cases/{overlong_id}/evidence",
        json={"note": "越权补证"},
        headers=REVIEWER_2,
    )
    assert resp.status_code == 403
    resp = _adjudicate(
        client, overlong_id, {"verdict": "upheld", "reason": "越权裁决"}, headers=REVIEWER_2
    )
    assert resp.status_code == 403

    # 管理员可代为裁决；但复开、合并、拆分仅管理员可用。
    resp = _adjudicate(
        client, overlong_id, {"verdict": "upheld", "reason": "管理员复核"}, headers=ADMIN
    )
    assert resp.status_code == 200
    resp = client.post(
        f"/api/plans/{PV}/cases/{overlong_id}/reopen",
        json={"reason": "复核员尝试复开"},
        headers=REVIEWER,
    )
    assert resp.status_code == 403
    resp = client.post(
        f"/api/plans/{PV}/cases/{negative_id}/merge",
        json={"target_case_id": overlong_id, "reason": "复核员尝试合并"},
        headers=REVIEWER,
    )
    assert resp.status_code == 403
    resp = client.post(
        f"/api/plans/{PV}/cases/{negative_id}/split",
        json={"source_event_ids": ["E-NEG"], "reason": "复核员尝试拆分"},
        headers=REVIEWER,
    )
    assert resp.status_code == 403


def test_merge_keeps_lineage_and_still_dedupes(client):
    _create_plan(client)
    _seed_all_anomalies(client)
    _detect(client)
    by_rule = _cases_by_rule(client)
    source_id = by_rule["negative_correction"]["case_id"]
    target_id = by_rule["overlong_checkin"]["case_id"]

    resp = client.post(
        f"/api/plans/{PV}/cases/{source_id}/merge",
        json={"target_case_id": target_id, "reason": "同一批实训异常并案处理"},
        headers=ADMIN,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"]["state"] == "merged"
    assert body["source"]["merged_into"] == target_id
    target = body["target"]
    assert source_id in target["lineage"]
    assert set(target["source_event_ids"]) == {"E-LONG", "E-NEG"}
    assert len(target["fingerprints"]) == 2

    # 源案件仍可追溯查询。
    source = client.get(f"/api/plans/{PV}/cases/{source_id}", headers=ADMIN).json()
    assert source["state"] == "merged"
    assert source["merged_into"] == target_id

    # 合并后指纹仍被目标案件覆盖，重复检测不会重新立案。
    again = _detect(client)
    assert again["created"] == []
    assert source_id not in again["existing"]
    assert target_id in again["existing"]

    # 已合并案件是终态，不能再被操作。
    assert _claim(client, source_id).status_code == 409


def test_split_traces_origin_and_validates_subset(client):
    _create_plan(client)
    _import(
        client,
        [
            _checkin("E-OV1", "S3", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
            _checkin("E-OV2", "S3", "2024-03-15T09:00:00+08:00", "2024-03-15T11:00:00+08:00"),
            _checkin("E-OV3", "S3", "2024-03-15T10:30:00+08:00", "2024-03-15T12:00:00+08:00"),
        ],
    )
    case_id = _detect(client)["created"][0]["case_id"]
    case = client.get(f"/api/plans/{PV}/cases/{case_id}", headers=ADMIN).json()
    assert case["source_event_ids"] == ["E-OV1", "E-OV2", "E-OV3"]

    # 拆分出不属于原案件的事件 -> 400。
    resp = client.post(
        f"/api/plans/{PV}/cases/{case_id}/split",
        json={"source_event_ids": ["E-OTHER"], "reason": "无效拆分"},
        headers=ADMIN,
    )
    assert resp.status_code == 400

    resp = client.post(
        f"/api/plans/{PV}/cases/{case_id}/split",
        json={"source_event_ids": ["E-OV1"], "reason": "E-OV1 单独复核"},
        headers=ADMIN,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    child = body["child"]
    assert child["state"] == "detected"
    assert child["source_event_ids"] == ["E-OV1"]
    assert child["lineage"] == [case_id]
    assert child["student_id"] == "S3"
    assert body["parent"]["child_case_ids"] == [child["case_id"]]

    # 同一子集重复拆分 -> 409；原始异常仍不会重复立案。
    resp = client.post(
        f"/api/plans/{PV}/cases/{case_id}/split",
        json={"source_event_ids": ["E-OV1"], "reason": "重复拆分"},
        headers=ADMIN,
    )
    assert resp.status_code == 409
    assert _detect(client)["created"] == []


def test_freeze_marks_pending_cases_without_counting_them(client):
    _create_plan(client)
    _import(
        client,
        [_checkin("E-LONG", "S2", "2024-03-15T06:00:00+08:00", "2024-03-15T20:00:00+08:00")],
    )
    case_id = _detect(client)["created"][0]["case_id"]

    # 冻结时案件未决：快照清晰标注，但总学时不受影响。
    frozen = client.post(f"/api/plans/{PV}/freezes/F-01", json={}).json()
    student = frozen["students"][0]
    assert student["total_seconds"] == 14 * 3600
    assert len(student["pending_cases"]) == 1
    marker = student["pending_cases"][0]
    assert marker["case_id"] == case_id
    assert marker["rule"] == "overlong_checkin"
    assert marker["state"] == "detected"
    assert marker["counts_toward_confirmed"] is False
    assert frozen["pending_cases"][0]["student_id"] == "S2"
    assert frozen["pending_cases"][0]["case_id"] == case_id

    # 冻结快照不可变：裁决落地后 F-01 仍保留未决标注。
    _claim(client, case_id)
    _adjudicate(client, case_id, {"verdict": "upheld", "reason": "封顶 12 小时"})
    frozen_again = client.get(f"/api/plans/{PV}/freezes/F-01").json()
    assert frozen_again["students"][0]["pending_cases"][0]["case_id"] == case_id
    assert frozen_again["students"][0]["total_seconds"] == 14 * 3600

    # 新一轮冻结：修正已计入，未决标注消失。
    f2 = client.post(f"/api/plans/{PV}/freezes/F-02", json={}).json()
    assert f2["students"][0]["total_seconds"] == 12 * 3600
    assert f2["students"][0]["pending_cases"] == []
    assert f2["pending_cases"] == []

    diff = client.get(f"/api/plans/{PV}/freezes/F-01/diff/F-02").json()
    change = diff["student_changes"][0]
    assert change["fields"]["total_seconds"] == {"before": 14 * 3600, "after": 12 * 3600}


def test_state_machine_rejects_illegal_transitions(client):
    _create_plan(client)
    _import(client, [_correction("E-NEG", "S1", -1800, "迟到扣减")])
    case_id = _detect(client)["created"][0]["case_id"]

    # 未认领不能裁决；重复认领冲突；终态不能复开之外的流转。
    resp = _adjudicate(client, case_id, {"verdict": "upheld", "reason": "未认领裁决"})
    assert resp.status_code == 409
    assert _claim(client, case_id).status_code == 200
    assert _claim(client, case_id, headers=REVIEWER_2).status_code == 409
    _adjudicate(client, case_id, {"verdict": "rejected", "reason": "驳回"})
    assert _claim(client, case_id, headers=REVIEWER_2).status_code == 409
    resp = client.post(
        f"/api/plans/{PV}/cases/{case_id}/evidence",
        json={"note": "终态补证"},
        headers=ADMIN,
    )
    assert resp.status_code == 409


def test_case_endpoints_require_existing_plan_and_case(client):
    _create_plan(client)
    resp = client.post("/api/plans/NOPE/cases/detect", json={}, headers=ADMIN)
    assert resp.status_code == 404
    resp = client.get(f"/api/plans/{PV}/cases/C-missing", headers=ADMIN)
    assert resp.status_code == 404
