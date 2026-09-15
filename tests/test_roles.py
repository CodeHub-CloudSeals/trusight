"""Roles gate actions server-side, not by hiding buttons.

Spec v2 s2 asks for a role model. A role model the UI merely draws is a
diagram, not a control: anyone with the URL and a browser console can post
the approval the button was hidden for. "Who may release steel" is a
governance question in a product sold on governance, so the tests here go
through the API with the button entirely out of the picture.

They also pin the boundary of what this build claims: the session is a demo
user picker, not authentication, and the endpoint that serves it says so.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trustsight.api import projects as prj
from trustsight.api.main import app

ADMIN = "priya.raman@demo-client.com"
ESTIMATOR = "tom.keller@demo-client.com"
REVIEWER = "ana.duarte@demo-client.com"
VIEWER = "commercial@demo-client.com"


def client(user: str | None = None) -> TestClient:
    return TestClient(app, headers={"x-trustsight-user": user} if user else {})


def _run(c: TestClient, scenario: str = "clarification") -> str:
    return c.post("/runs", json={"project_id": "atlantic-demo",
                                 "scenario": scenario}).json()["run_id"]


def test_the_sign_in_endpoint_does_not_claim_to_authenticate():
    body = client().get("/session/users").json()
    assert body["authentication"] == "demonstration"
    assert "no password" in body["note"].lower()
    # and it never ships a password field or a credential of any kind
    assert not any("password" in k for u in body["users"] for k in u)


def test_an_unsigned_request_cannot_write():
    c = client()
    assert c.post("/api/projects", json={"name": "x"}).status_code == 401


def test_a_viewer_cannot_answer_a_clarification():
    run_id = _run(client(ADMIN))
    r = client(VIEWER).post(f"/runs/{run_id}/clarifications", json={
        "field_name": "run_length_mm", "value": 12250, "element_type": "pile",
        "role": "spiral", "approver": "Commercial Team", "rationale": "looks right"})
    assert r.status_code == 403
    assert "Viewer" in r.json()["detail"]


def test_a_viewer_cannot_release_by_calling_the_api_directly():
    """The button is hidden for a Viewer. That is not what stops them."""
    admin = client(ADMIN)
    run_id = _run(admin, "approval")
    run = admin.get(f"/runs/{run_id}").json()
    assert run["pending_step"] == 19
    refused = client(VIEWER).post(f"/runs/{run_id}/approve", json={
        "token": run["pending_token"], "approver": "Commercial Team",
        "answer": {"subject": "rulebook_approval", "decision": "approved"},
        "rationale": "signing this off"})
    assert refused.status_code == 403
    # nothing released, and the run is still waiting for someone who may
    assert admin.get(f"/runs/{run_id}/workspace").json()["released_mass_kg"] == 0
    assert admin.get(f"/runs/{run_id}").json()["pending_step"] == 19


def test_an_estimator_answers_but_does_not_sign_the_rulebook():
    """The separation the spec asks for: doing the work is not approving it."""
    admin = client(ADMIN)
    run_id = _run(admin, "approval")
    run = admin.get(f"/runs/{run_id}").json()
    assert client(ESTIMATOR).post(f"/runs/{run_id}/approve", json={
        "token": run["pending_token"], "approver": "Tom Keller",
        "answer": {"subject": "rulebook_approval", "decision": "approved"},
        "rationale": "done"}).status_code == 403
    # the reviewer may, and the release follows
    assert client(REVIEWER).post(f"/runs/{run_id}/approve", json={
        "token": run["pending_token"], "approver": "Ana Duarte",
        "answer": {"subject": "rulebook_approval", "decision": "approved"},
        "rationale": "assumption sheet reviewed"}).status_code == 200
    assert admin.get(f"/runs/{run_id}/workspace").json()["released_mass_kg"] > 0


def test_a_reviewer_cannot_create_projects_and_an_admin_can():
    assert client(REVIEWER).post("/api/projects",
                                 json={"name": "Reviewer project"}).status_code == 403
    r = client(ADMIN).post("/api/projects", json={
        "name": "Role Test Tower", "client": "Demo", "site": "Ontario",
        "estimator": "Tom Keller", "manual_baseline_minutes": 40})
    assert r.status_code == 200
    body = r.json()
    assert body["project_id"] == "role-test-tower"
    assert body["created_by"] == prj.USERS[ADMIN].email
    assert body["tenant_id"] == prj.DEMO_TENANT


def test_a_project_from_another_tenant_is_not_found_rather_than_forbidden():
    """A 403 confirms the id exists. For cross-tenant reads, 404 is correct."""
    c = client(ADMIN)
    c.post("/api/projects", json={"name": "Tenant Probe"})
    other = prj.DemoUser("spy@other-tenant.com", "Spy", "admin", "other-tenant")
    prj.USERS[other.email] = other
    try:
        r = client(other.email).get("/api/projects/tenant-probe")
        assert r.status_code == 404
        assert client(other.email).get("/api/projects").json() == []
    finally:
        prj.USERS.pop(other.email, None)


@pytest.mark.parametrize("channel,status", [
    ("web_upload", "live"), ("api", "live"),
    ("s3", "roadmap"), ("sftp", "roadmap"), ("sharepoint", "roadmap"),
])
def test_intake_channels_state_whether_they_actually_work(channel, status):
    """A connector card drawn as available and never demonstrated is a lie
    a technical buyer will remember for the rest of the meeting."""
    got = {c["key"]: c["status"] for c in client().get("/api/channels").json()}
    assert got[channel] == status
