"""The ROI screen is the easiest place in this product to lie.

Every test here pins a refusal rather than a feature: the cases where the
honest answer is "no number", and the case where a correct result must not
under-report itself. All four are mistakes this file was written after
making.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from trustsight.engine.rulebook import Rulebook
from trustsight.evidence.fabric import EvidenceChain
from trustsight.graph.knowledge_graph import ProjectKnowledgeGraph
from trustsight.knowledge.project import ProjectKnowledge, Scope
from trustsight.services import roi as roi_service
from trustsight.services.pipeline import (
    PipelineContext, answer, build_runner, rerun,
)
from trustsight.workflow.runner import Run

CORPUS = os.getenv("TRUSTSIGHT_CORPUS")


def _context(baseline: int | None = None) -> PipelineContext:
    return PipelineContext(
        project_id="atlantic", document=Path("unused.pdf"),
        rulebook=Rulebook(version="t-1.0", project_id="atlantic",
                          cover_mm={"pile": 75}, stock_length_mm=9000,
                          rounding_mm=5, spacing_convention="floor_plus_one",
                          approved_by="test"),
        graph=ProjectKnowledgeGraph("atlantic"), chain=EvidenceChain("t"),
        knowledge=ProjectKnowledge("atlantic"),
        manual_baseline_minutes=baseline,
    )


def test_no_baseline_means_no_saving_is_claimed():
    """The default must be silence, not an assumed industry figure."""
    report = roi_service.report(Run(project_id="atlantic"), _context())
    assert report["saving"]["available"] is False
    assert "no manual baseline" in report["saving"]["reason"].lower()
    # and nothing in the payload smuggles a percentage back in
    assert "pct_saved" not in report["saving"]


def test_a_baseline_alone_does_not_produce_a_saving():
    """A run that released nothing has saved nobody any time.

    This is the screen that once read "100% time saved" beside "0 of 2
    reference lines produced". The saving is gated on released work, not on
    the clock.
    """
    report = roi_service.report(Run(project_id="atlantic"), _context(baseline=40))
    assert report["claims"]["released"] == 0
    assert report["saving"]["available"] is False
    assert "released" in report["saving"]["reason"]


def test_accuracy_is_absent_rather_than_assumed_when_there_is_no_reference():
    report = roi_service.report(Run(project_id="atlantic"), _context(), benchmark=None)
    assert report["accuracy"] is None


def test_recall_counts_lines_never_produced():
    """A missed line is a safety failure and must not average away."""
    benchmark = {
        "label": "ref", "mass_kg": 100.0,
        "rows": [
            {"mark": "A", "size": "30M", "reference_quantity": 10,
             "reference_length_mm": 1000, "generated_quantity": 10,
             "generated_length_mm": 1000, "match": True},
            {"mark": "B", "size": "15M", "reference_quantity": 5,
             "reference_length_mm": 500, "generated_quantity": None,
             "generated_length_mm": None, "match": False},
        ],
    }
    acc = roi_service.report(Run(project_id="a"), _context(),
                             benchmark=benchmark, generated_mass_kg=60.0)["accuracy"]
    assert acc["element_recall"] == 0.5
    assert acc["lines"][1]["outcome"] == "not_generated"
    assert acc["mass_variance_kg"] == -40.0


def test_decision_history_survives_a_rulebook_sign_off():
    """The timeline must render whatever shape an approval record has.

    Two kinds of approval reach the chain: a clarification answer carrying
    {"field", "value"}, and a rulebook sign-off carrying neither. The screen
    read ``value.field`` on both, so the Review tab threw the moment the
    rulebook was signed — the screen the demo lands on immediately after.
    """
    from fastapi.testclient import TestClient

    from trustsight.api.main import app

    client = TestClient(app, headers={'x-trustsight-user': 'priya.raman@demo-client.com'})
    run_id = client.post("/runs", json={"project_id": "atlantic-demo",
                                        "scenario": "approval"}).json()["run_id"]
    run = client.get(f"/runs/{run_id}").json()
    assert run["pending_step"] == 19, "this scenario must stop at the gate"
    client.post(f"/runs/{run_id}/approve", json={
        "token": run["pending_token"], "approver": "engineer",
        "answer": {"subject": "rulebook_approval", "decision": "approved"},
        "rationale": "assumption sheet reviewed"})

    history = client.get(f"/runs/{run_id}/workspace").json()["history"]
    assert history, "a sign-off is a human decision and belongs in the timeline"
    for row in history:
        assert row["label"], "every row needs something to print"
        assert "detail" in row
    # and the per-element copies written for release_status are not shown as
    # separate decisions — one signature is one decision
    assert sum(1 for r in history if r["kind"] == "rulebook") <= 1
    assert len(history) == 1


@pytest.mark.skipif(not CORPUS or not Path(CORPUS).exists(),
                    reason="reference corpus not available")
def test_exact_result_is_not_reported_as_a_mismatch():
    """A drawing that states no bar mark must still reconcile.

    The comparison matched generated lines to the reference by bar mark. The
    Atlantic drawings never state one, so every generated mark was None and
    a run that was correct to the millimetre reported 0 of 2 lines matching
    — the product understating itself in front of a client.
    """
    from trustsight.api import demo  # noqa: F401  (import guard only)

    project = next(Path(CORPUS).glob("*Atlanic*"), None)
    if project is None:
        pytest.skip("Atlantic project not in this corpus")
    ctx = PipelineContext(
        project_id="atlantic", document=project,
        rulebook=Rulebook(version="a-1.0", project_id="atlantic",
                          cover_mm={"pile": 75}, stock_length_mm=9000,
                          rounding_mm=5, spacing_convention="floor_plus_one",
                          approved_by="test"),
        graph=ProjectKnowledgeGraph("atlantic"), chain=EvidenceChain("t"),
        knowledge=ProjectKnowledge("atlantic"))
    build_runner(ctx).execute(Run(project_id="atlantic"))
    for field, value, role in (
            ("legs", {"A": 510, "B": 11955}, "longitudinal"),
            ("bend_type", "2", "longitudinal"),
            ("run_length_mm", 12250, "spiral"),
            ("legs", {"A": 140, "B": 140, "C": 2545, "G": 300, "O": 810}, "spiral"),
            ("bend_type", "T3", "spiral")):
        answer(ctx, field_name=field, value=value,
               scope=Scope(project_id="atlantic", element_type="pile",
                           mark="P-A", role=role),
               approver="test", rationale="recorded answer")
    rerun(ctx, "atlantic")

    assert ctx.schedule is not None
    generated = {(i.size.value, i.cutting_length_mm): i.quantity
                 for i in ctx.schedule.items}
    # the two reference lines, matched on physical identity rather than label
    assert generated[("30M", 12465)] == 72
    assert generated[("15M", 3125)] == 216
    assert all(i.mark is None for i in ctx.schedule.items), (
        "these drawings state no bar mark; if that changes, the comparison "
        "should tighten to the mark again")
