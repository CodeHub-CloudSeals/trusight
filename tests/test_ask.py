"""Ask TrustSight answers from the run or says nothing.

The feature is only safe because of the second half. This build configures
no model, so every answer is a query over records that exist. The tests that
matter most here are the ones asserting it declines: a query bar that
invents an engineering fact in front of a client refutes the product it is
attached to, and it would do so in one sentence.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trustsight.api.main import app

ADMIN = "priya.raman@demo-client.com"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, headers={"x-trustsight-user": ADMIN})


@pytest.fixture
def run_id(client: TestClient) -> str:
    return client.post("/runs", json={"project_id": "atlantic-demo"}).json()["run_id"]


def ask(client: TestClient, run_id: str, q: str) -> dict:
    r = client.post(f"/runs/{run_id}/ask", json={"question": q})
    assert r.status_code == 200, r.text
    return r.json()


def test_it_declines_what_it_cannot_ground(client, run_id):
    for q in ("what will steel prices do next quarter",
              "should we bid this job",
              "write me a poem about rebar"):
        a = ask(client, run_id, q)
        assert a["intent"] is None
        assert a["rows"] == []
        assert "will not guess" in a["text"]
        # and it says what it can do instead of trailing off
        assert a["examples"]


def test_the_endpoint_states_that_no_model_is_configured(client):
    body = client.get("/ask/examples").json()
    assert body["model_configured"] is False
    assert "declined rather than guessed" in body["note"]


def test_blocked_is_answered_from_the_run_not_from_a_template(client, run_id):
    a = ask(client, run_id, "what is blocked?")
    assert a["intent"] == "blocked"
    assert a["rows"], "the seeded run starts with open clarifications"
    fields = {r["what"] for r in a["rows"]}
    assert "run length mm" in fields or "legs" in fields
    assert a["goto"] == "review"


def test_released_reports_zero_before_anything_releases(client, run_id):
    a = ask(client, run_id, "how much has released?")
    assert a["intent"] == "released"
    assert "Nothing has released" in a["text"]


def test_rulebook_answer_names_the_version_in_force(client, run_id):
    a = ask(client, run_id, "which rulebook is in force?")
    assert a["intent"] == "rulebook"
    assert "atlantic-1.0" in a["text"]
    assert any(c["kind"] == "rulebook" for c in a["citations"])


def test_every_answer_is_marked_grounded_and_carries_somewhere_to_look(client, run_id):
    for q in ("what is blocked", "which route did this take",
              "is the evidence chain intact", "what did this cost in time",
              "what do the controls show"):
        a = ask(client, run_id, q)
        assert a["grounded"] is True
        assert a["text"].strip()


def test_ask_is_refused_on_a_recorded_run(client):
    """A recording has no live context; answering from it would imply one."""
    r = client.post("/runs/recorded-anything/ask", json={"question": "what is blocked"})
    assert r.status_code == 409


def test_an_empty_question_offers_examples_rather_than_erroring(client, run_id):
    a = ask(client, run_id, "   ")
    assert a["examples"]
    assert a["rows"] == []
