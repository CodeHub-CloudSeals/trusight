"""An unstated bar shape is a question, never a default.

Found by walking the client runbook through the UI on a real corpus project
rather than through the API. The seeded walkthrough sets a bend type, so it
reconciled exactly; the real project does not, and nothing ever asked for
one. The catalogue keys straight bars on "" and a missing bend type resolved
to the same entry, so the cutting length became column B alone:

    longitudinal  A 510 + B 11,955  ->  11,955 mm   (the hook dropped)
    spiral        A/B/C/G/O          ->     140 mm   (four legs dropped)

4,777.4 kg instead of 5,991.4 kg, and both lines marked *released*. A wrong
number that passes its release gates is the one failure this product exists
to prevent, so these tests pin the refusal rather than the result.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from trustsight.engine.calculator import RebarCalculator
from trustsight.engine.rulebook import Rulebook
from trustsight.evidence.fabric import EvidenceChain
from trustsight.graph.knowledge_graph import ProjectKnowledgeGraph
from trustsight.knowledge.project import ProjectKnowledge, Scope
from trustsight.models.core import BarSize, RebarRole, Reinforcement
from trustsight.services.pipeline import (
    PipelineContext, answer, build_runner, rerun,
)
from trustsight.shapes.catalogue import ShapeResolutionError, resolve
from trustsight.workflow.runner import Run

CORPUS = os.getenv("TRUSTSIGHT_CORPUS")


def _rulebook() -> Rulebook:
    return Rulebook(version="t-1.0", project_id="t", cover_mm={"pile": 75},
                    stock_length_mm=9000, rounding_mm=5,
                    spacing_convention="floor_plus_one", approved_by="test")


def _bar(bend_type: str | None) -> Reinforcement:
    return Reinforcement(role=RebarRole.LONGITUDINAL, bar_size=BarSize.M30,
                         count=12, bend_type=bend_type,
                         legs={"A": 510, "B": 11955})


def test_an_unstated_bend_type_is_refused_not_assumed_straight():
    calc = RebarCalculator(_rulebook())
    with pytest.raises(ShapeResolutionError) as exc:
        calc.cutting_length(_bar(None))
    assert "not stated" in str(exc.value)
    # and the specific wrong answer it used to give is not produced
    assert "11955" not in str(exc.value)


def test_an_explicitly_straight_bar_still_works():
    """"" means the bar list said straight; None means nobody said."""
    assert resolve("", {"B": 4000}).cutting_length({"B": 4000}) == 4000
    calc = RebarCalculator(_rulebook())
    straight = Reinforcement(role=RebarRole.LONGITUDINAL, bar_size=BarSize.M30,
                             count=4, bend_type="", legs={"B": 4000})
    assert calc.cutting_length(straight) == 4000


def test_a_stated_shape_sums_the_columns_that_shape_defines():
    calc = RebarCalculator(_rulebook())
    assert calc.cutting_length(_bar("2")) == 12465  # A + B, hook included


def test_a_mistyped_bend_type_is_rejected_not_run():
    """A typo on stage must not end the walkthrough.

    The shape is now something a presenter types, and an unchecked value
    reached the catalogue: a number raised AttributeError and the run went
    to FAILED. Reject it at the door, naming the shapes that exist.
    """
    from fastapi.testclient import TestClient

    from trustsight.api.main import app

    client = TestClient(app, headers={'x-trustsight-user': 'priya.raman@demo-client.com'})
    run_id = client.post("/runs", json={"project_id": "atlantic-demo"}).json()["run_id"]
    for bad in (12250, "ZZZ9", "", None, ["2"]):
        r = client.post(f"/runs/{run_id}/clarifications", json={
            "field_name": "bend_type", "value": bad, "element_type": "pile",
            "role": "longitudinal", "approver": "t", "rationale": "t"})
        assert r.status_code == 422, f"{bad!r} was accepted"
        assert "bend type" in r.json()["detail"].lower()
    # and the run is untouched by the rejections
    assert client.get(f"/runs/{run_id}").json()["state"] != "failed"
    # a catalogued shape is accepted
    assert client.post(f"/runs/{run_id}/clarifications", json={
        "field_name": "bend_type", "value": " 2 ", "element_type": "pile",
        "role": "longitudinal", "approver": "t",
        "rationale": "single end hook"}).status_code == 200


def test_the_catalogue_never_raises_a_bare_attribute_error():
    """Defence in depth: the endpoint validates, and so does the catalogue."""
    for bad in (12250, 2.5, ["2"], {"t": "2"}):
        with pytest.raises(ShapeResolutionError):
            resolve(bad, {"A": 1, "B": 2})  # type: ignore[arg-type]


@pytest.mark.skipif(not CORPUS or not Path(CORPUS).exists(),
                    reason="reference corpus not available")
def test_the_screen_flow_alone_reaches_the_reference():
    """Answer only what the product asks, in the order it asks.

    The regression this guards is the gap between the API flow (which a test
    can feed extra facts) and the flow a presenter actually walks. Anything
    the calculation needs must be asked for, or the demo diverges from every
    script written against it.
    """
    project = next(Path(CORPUS).glob("*Atlanic*"), None)
    if project is None:
        pytest.skip("Atlantic project not in this corpus")
    ctx = PipelineContext(
        project_id="atlantic", document=project, rulebook=_rulebook(),
        graph=ProjectKnowledgeGraph("atlantic"), chain=EvidenceChain("t"),
        knowledge=ProjectKnowledge("atlantic"))
    build_runner(ctx).execute(Run(project_id="atlantic"))

    answers = {
        ("legs", "longitudinal"): {"A": 510, "B": 11955},
        ("legs", "spiral"): {"A": 140, "B": 140, "C": 2545, "G": 300, "O": 810},
        ("run_length_mm", "spiral"): 12250,
        ("bend_type", "longitudinal"): "2",
        ("bend_type", "spiral"): "T3",
    }
    asked: list[tuple[str, str]] = []
    for _ in range(10):
        if not ctx.questions:
            break
        q = ctx.questions[0]
        key = (q["field"], q["role"])
        assert key in answers, f"the run asked something unanswerable: {key}"
        asked.append(key)
        answer(ctx, field_name=q["field"], value=answers[key],
               scope=Scope(project_id="atlantic", element_type="pile",
                           mark="P-A", role=q["role"]),
               approver="test", rationale="screen flow")
        rerun(ctx, "atlantic")

    assert ("bend_type", "longitudinal") in asked, (
        "the shape must be asked for; without it the cutting length is built "
        "from whichever columns happen to be populated")
    assert ctx.schedule is not None
    assert round(ctx.schedule.total_mass_kg, 1) == 5991.4
    generated = {(i.size.value, i.cutting_length_mm): i.quantity
                 for i in ctx.schedule.items}
    assert generated == {("30M", 12465): 72, ("15M", 3125): 216}
