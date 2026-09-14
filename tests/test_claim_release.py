"""Release is decided per claim, not per element.

The invariant under test: one unresolved spiral must not suppress a resolved
longitudinal bar on the same pile. Keying the release decision on the element
forces both claims to share an answer, and the only safe shared answer is to
block the resolved one too — which is how a correct number fails to reach an
estimate.
"""
import os
from pathlib import Path

import pytest

from trustsight.engine.rulebook import Rulebook
from trustsight.evidence.fabric import EvidenceChain, ReleaseState, claim_subject
from trustsight.graph.knowledge_graph import ProjectKnowledgeGraph
from trustsight.knowledge.project import ProjectKnowledge, Scope
from trustsight.services.pipeline import (
    PipelineContext, answer, build_runner, rerun,
)
from trustsight.workflow import routes
from trustsight.workflow.runner import Run

CORPUS = os.getenv("TRUSTSIGHT_CORPUS")
pytestmark = pytest.mark.skipif(
    not CORPUS or not Path(CORPUS).exists(), reason="reference corpus not available"
)


def _atlantic_context() -> PipelineContext:
    project = next(Path(CORPUS).glob("*Atlanic*"), None)
    if project is None:
        pytest.skip("Atlantic project not in this corpus")
    rulebook = Rulebook(
        version="atlantic-1.0", project_id="atlantic", cover_mm={"pile": 75},
        stock_length_mm=9000, rounding_mm=5,
        spacing_convention="floor_plus_one", approved_by="test",
    )
    return PipelineContext(
        project_id="atlantic", document=project, rulebook=rulebook,
        graph=ProjectKnowledgeGraph("atlantic"), chain=EvidenceChain("t"),
        knowledge=ProjectKnowledge("atlantic"),
    )


def test_claim_subject_separates_two_claims_on_one_element():
    assert claim_subject("p|pile|P1", "abc") != claim_subject("p|pile|P1", "def")
    # element-wide facts keep a subject of their own
    assert claim_subject("p|pile|P1") == "p|pile|P1"


def test_one_claim_releases_while_another_stays_blocked():
    ctx = _atlantic_context()
    build_runner(ctx).execute(Run(project_id="atlantic"))

    # answer the longitudinal basis only; the spiral run length stays unstated
    for field, value in (("legs", {"A": 510, "B": 11955}),
                         ("bend_type", "2"), ("shape_code", "30A01")):
        answer(ctx, field_name=field, value=value,
               scope=Scope(project_id="atlantic", element_type="pile",
                           mark="P-A", role="longitudinal"),
               approver="test", rationale="longitudinal only")
    rerun(ctx, "atlantic")

    released = [i for i in ctx.schedule.items
                if ctx.chain.release_status(
                    claim_subject(i.element_key, i.claim_id),
                    rulebook_approved=True).state is ReleaseState.RELEASED]
    assert len(released) == 1, "the resolved longitudinal claim must release"
    assert released[0].size.value == "30M"
    assert released[0].quantity == 72

    # and the spiral is still an open exception on the same element
    assert any("spiral" in e for e in ctx.schedule.exceptions)
    assert all("longitudinal" not in e for e in ctx.schedule.exceptions)


def test_routes_take_visibly_different_step_sequences():
    """Three routes over one business case must not look identical."""
    sequences = {}
    for name in ("structured", "semi_structured"):
        ctx = _atlantic_context()
        ctx.path_mode = name
        run = build_runner(ctx).execute(Run(project_id="atlantic"))
        sequences[name] = run.completed_steps()
        assert ctx.route == name

    assert sequences["structured"] != sequences["semi_structured"]
    # the semi-structured route is the one that correlates and clarifies
    extra = set(sequences["semi_structured"]) - set(sequences["structured"])
    assert extra, "semi-structured must run steps the structured route does not"


def test_unavailable_route_is_refused_not_half_run():
    route = routes.get("unstructured")
    assert route.available is False
    assert route.unavailable_reason
    # and the reason names what is missing rather than being decorative
    assert "interpretation" in route.unavailable_reason


def test_steps_outside_the_route_are_marked_not_applicable():
    ctx = _atlantic_context()
    ctx.path_mode = "structured"
    run = build_runner(ctx).execute(Run(project_id="atlantic"))
    reasons = {no: status.value for no, status in run.skipped.items()}
    assert reasons, "skipped steps must carry a reason, not vanish"
    assert "not_applicable" in reasons.values()
    # step 11 clarification is implemented but not part of the structured route
    assert reasons.get(11) == "not_applicable"
