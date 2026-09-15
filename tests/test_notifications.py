"""NOTIFY-101: the two alerts, and the claim they must not make.

The backlog asks for "email / Teams-style notifications". This build has no
transport, so the only dishonest outcome available is an alert that reads as
though something was sent. Every test here is about that boundary, plus the
rule that an alert must describe the run as it is now — a stale "clarification
required" after the answer landed is worse than silence.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

ADMIN = {"x-trustsight-user": "priya.raman@demo-client.com"}
VIEWER = {"x-trustsight-user": "commercial@demo-client.com"}


@pytest.fixture()
def client():
    from trustsight.api.main import app

    with TestClient(app, headers=ADMIN) as c:
        yield c


def _alerts(client, run_id, headers=None):
    return client.get(f"/runs/{run_id}/notifications",
                      headers=headers or ADMIN).json()


def test_no_alert_claims_to_have_been_sent(client):
    run_id = client.post("/runs", json={"project_id": "atlantic-demo",
                                        "scenario": "clarification"}).json()["run_id"]
    body = _alerts(client, run_id)
    assert body["transport"] == {"email": False, "teams": False,
                                 "note": body["transport"]["note"]}
    assert "not configured" in body["transport"]["note"]
    for a in body["alerts"]:
        assert a["delivery"]["sent_externally"] is False
        assert a["delivery"]["channel"] == "in_app"


def test_an_open_clarification_raises_the_alert_the_spec_names(client):
    run_id = client.post("/runs", json={"project_id": "atlantic-demo",
                                        "scenario": "clarification"}).json()["run_id"]
    kinds = [a["kind"] for a in _alerts(client, run_id)["alerts"]]
    assert "clarification_required" in kinds
    assert "bbs_ready" not in kinds, "nothing has released yet"


def test_a_released_run_raises_the_bbs_ready_alert(client):
    run_id = client.post("/runs", json={"project_id": "atlantic-demo",
                                        "scenario": "structured"}).json()["run_id"]
    alerts = _alerts(client, run_id)["alerts"]
    kinds = [a["kind"] for a in alerts]
    assert "bbs_ready" in kinds
    assert "clarification_required" not in kinds
    ready = next(a for a in alerts if a["kind"] == "bbs_ready")
    assert "kg" in ready["body"]


def test_the_rulebook_gate_raises_its_own_alert_and_clears_when_signed(client):
    run_id = client.post("/runs", json={"project_id": "atlantic-demo",
                                        "scenario": "approval"}).json()["run_id"]
    assert "approval_required" in [a["kind"] for a in _alerts(client, run_id)["alerts"]]

    run = client.get(f"/runs/{run_id}").json()
    client.post(f"/runs/{run_id}/approve", json={
        "token": run["pending_token"], "approver": "Ana Duarte",
        "answer": {"subject": "rulebook_approval", "decision": "approved"},
        "rationale": "assumption sheet reviewed"})

    after = [a["kind"] for a in _alerts(client, run_id)["alerts"]]
    assert "approval_required" not in after, (
        "an alert derived from state must clear when the state does")
    assert "bbs_ready" in after


def test_an_alert_keeps_the_time_it_was_first_raised(client):
    run_id = client.post("/runs", json={"project_id": "atlantic-demo",
                                        "scenario": "clarification"}).json()["run_id"]
    first = _alerts(client, run_id)["alerts"][0]
    again = _alerts(client, run_id)["alerts"][0]
    assert first["raised_at"] == again["raised_at"]
    assert first["id"] == again["id"]


def test_an_alert_can_be_marked_read(client):
    run_id = client.post("/runs", json={"project_id": "atlantic-demo",
                                        "scenario": "clarification"}).json()["run_id"]
    body = _alerts(client, run_id)
    assert body["unread"] == len(body["alerts"])
    alert_id = body["alerts"][0]["id"]
    client.post(f"/runs/{run_id}/notifications/{alert_id}/read")
    after = _alerts(client, run_id)
    assert next(a for a in after["alerts"] if a["id"] == alert_id)["read"] is True
    assert after["unread"] == body["unread"] - 1


def test_an_alert_says_whether_this_person_can_act_on_it(client):
    """A Viewer should not be told to go and answer a clarification."""
    run_id = client.post("/runs", json={"project_id": "atlantic-demo",
                                        "scenario": "clarification"}).json()["run_id"]
    admin = _alerts(client, run_id)["alerts"][0]
    viewer = _alerts(client, run_id, headers=VIEWER)["alerts"][0]
    assert admin["actionable_by_you"] is True
    assert viewer["actionable_by_you"] is False


def test_a_recorded_run_raises_nothing(client):
    ready = client.get("/ready").json()
    recorded = ready["checks"].get("recorded_runs") or []
    if not recorded:
        pytest.skip("no recorded run frozen on this host")
    body = _alerts(client, recorded[0])
    assert body["alerts"] == []
    assert body["recorded"] is True
