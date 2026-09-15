"""COP-102: a query bar that can do things, and the gates in front of them.

The acceptance rule is "answers are grounded in project data and unsafe
actions require confirmation". The tests that matter are the refusals:
asking must never *be* the doing, a role without the capability must be
refused by the API, a confirmation with nothing recorded against it must be
rejected, and "release these bars" must be declined outright — release is
computed from the gate vector, and a product that offers to release on
request has just contradicted its own pitch.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

ADMIN = {"x-trustsight-user": "priya.raman@demo-client.com"}
VIEWER = {"x-trustsight-user": "commercial@demo-client.com"}
ESTIMATOR = {"x-trustsight-user": "tom.keller@demo-client.com"}


@pytest.fixture()
def client():
    from trustsight.api.main import app

    with TestClient(app, headers=ADMIN) as c:
        yield c


@pytest.fixture()
def gated_run(client):
    """A run suspended on the rulebook gate — the only signable state."""
    run_id = client.post("/runs", json={"project_id": "atlantic-demo",
                                        "scenario": "approval"}).json()["run_id"]
    assert client.get(f"/runs/{run_id}").json()["pending_step"] == 19
    return run_id


def _ask(client, run_id, q, headers=None):
    return client.post(f"/runs/{run_id}/ask", json={"question": q},
                       headers=headers or ADMIN)


def test_asking_to_sign_proposes_and_does_not_sign(client, gated_run):
    body = _ask(client, gated_run, "approve the rulebook").json()
    action = body["proposed_action"]
    assert action["action"] == "approve_rulebook"
    assert action["available"] is True
    assert action["irreversible"] is True
    assert action["confirmation_required"]["confirm"] is True

    # nothing happened
    run = client.get(f"/runs/{gated_run}").json()
    assert run["pending_step"] == 19, "asking must not be doing"
    data = client.get(f"/runs/{gated_run}/workspace").json()
    assert not data["rulebook"]["approved_by"]


def test_an_action_without_confirmation_is_refused(client, gated_run):
    r = client.post(f"/runs/{gated_run}/ask/act",
                    json={"action": "approve_rulebook", "rationale": "reviewed"})
    assert r.status_code == 428
    assert "not confirmed" in r.json()["detail"]
    assert client.get(f"/runs/{gated_run}").json()["pending_step"] == 19


def test_a_confirmation_without_a_rationale_is_refused(client, gated_run):
    r = client.post(f"/runs/{gated_run}/ask/act",
                    json={"action": "approve_rulebook", "confirm": True,
                          "rationale": "  "})
    assert r.status_code == 422
    assert "rationale" in r.json()["detail"]
    assert client.get(f"/runs/{gated_run}").json()["pending_step"] == 19


def test_a_viewer_is_refused_by_the_api_not_by_a_hidden_button(client, gated_run):
    r = client.post(f"/runs/{gated_run}/ask/act",
                    json={"action": "approve_rulebook", "confirm": True,
                          "rationale": "I would like to sign this"},
                    headers=VIEWER)
    assert r.status_code == 403
    assert client.get(f"/runs/{gated_run}").json()["pending_step"] == 19


def test_an_estimator_is_told_up_front_that_they_cannot_sign(client, gated_run):
    """Do not invite someone to press a button that will refuse them."""
    body = _ask(client, gated_run, "can you sign the rulebook",
                headers=ESTIMATOR).json()
    action = body["proposed_action"]
    assert action["permitted_for_you"] is False
    assert action["available"] is False
    assert "cannot approve" in action["unavailable_reason"]


def test_a_confirmed_action_by_a_permitted_role_runs_and_is_recorded(client, gated_run):
    r = client.post(f"/runs/{gated_run}/ask/act",
                    json={"action": "approve_rulebook", "confirm": True,
                          "rationale": "assumption sheet reviewed against spec"})
    assert r.status_code == 200, r.text

    data = client.get(f"/runs/{gated_run}/workspace").json()
    assert data["rulebook"]["approved_by"], "the signature must be recorded"
    released = [i for i in data["schedule"]["items"] if i["release"] == "released"]
    assert released, "signing at the gate must actually release something"
    # and the decision is in the timeline with its rationale
    assert any("reviewed" in (h.get("detail") or "") for h in data["history"])


def test_signing_twice_is_refused_rather_than_silently_repeated(client, gated_run):
    first = client.post(f"/runs/{gated_run}/ask/act",
                        json={"action": "approve_rulebook", "confirm": True,
                              "rationale": "assumption sheet reviewed"})
    assert first.status_code == 200
    second = client.post(f"/runs/{gated_run}/ask/act",
                         json={"action": "approve_rulebook", "confirm": True,
                               "rationale": "again"})
    assert second.status_code == 409
    assert "already signed" in second.json()["detail"]


def test_release_is_declined_as_a_command(client, gated_run):
    """The refusal that protects the product's own claim."""
    body = _ask(client, gated_run, "release the bars").json()
    action = body["proposed_action"]
    assert action["action"] is None
    assert action["available"] is False
    assert "computed from the gate vector" in action["unavailable_reason"]

    # and there is no endpoint that would do it
    r = client.post(f"/runs/{gated_run}/ask/act",
                    json={"action": "release", "confirm": True,
                          "rationale": "please"})
    assert r.status_code == 404


def test_generating_the_pack_changes_no_quantity(client):
    run_id = client.post("/runs", json={"project_id": "atlantic-demo",
                                        "scenario": "structured"}).json()["run_id"]
    before = client.get(f"/runs/{run_id}/workspace").json()
    r = client.post(f"/runs/{run_id}/ask/act",
                    json={"action": "generate_pack", "confirm": True,
                          "rationale": "sending to the client"})
    assert r.status_code == 200, r.text
    assert r.json()["outputs"], "a released run should produce a pack"
    after = client.get(f"/runs/{run_id}/workspace").json()
    assert after["released_mass_kg"] == before["released_mass_kg"]
    assert after["rulebook"]["approved_by"] == before["rulebook"]["approved_by"]


def test_an_ordinary_question_proposes_nothing(client):
    # the clarification scenario is the one that actually has open questions;
    # the approval scenario knows everything and waits only on a signature
    run_id = client.post("/runs", json={"project_id": "atlantic-demo",
                                        "scenario": "clarification"}).json()["run_id"]
    body = _ask(client, run_id, "what is blocked and why?").json()
    assert body["proposed_action"] is None
    assert body["rows"], "it should still answer"


def test_an_ungroundable_question_still_declines(client, gated_run):
    body = _ask(client, gated_run, "what will steel prices do next year").json()
    assert "will not guess" in body["text"]
    assert body["proposed_action"] is None
