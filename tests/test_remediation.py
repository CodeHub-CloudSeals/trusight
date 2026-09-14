"""Tests for the controls the demo promises.

Each maps to an item in the remediation plan. These exist because the
behaviours they check are the ones a client will probe.
"""
import os
from pathlib import Path

import pytest

from trustsight.engine.calculator import (
    QuantityConflict, RebarCalculator, derive_spacing_count,
)
from trustsight.engine.controls import ControlStatus, evaluate
from trustsight.engine.rulebook import RuleNotFound, Rulebook
from trustsight.engine.spatial import build_scene
from trustsight.evidence.fabric import ClaimType, EvidenceChain, ReleaseState
from trustsight.graph.knowledge_graph import ProjectKnowledgeGraph
from trustsight.knowledge.project import ProjectKnowledge, Scope
from trustsight.models.core import (
    BarSize, CountingPattern, Element, ElementIdentity, ElementType, Geometry,
    GateVector, Placement, Provenance, RebarRole, Reinforcement, SheetRef,
    SourceTier,
)
from trustsight.workflow.runner import Run, WorkflowRunner

APPROVED = Rulebook(
    version="t-1.0", project_id="t", cover_mm={"pile": 75},
    lap_multiplier={BarSize.M30: 40}, stock_length_mm=9000, rounding_mm=5,
    spacing_convention="floor_plus_one", approved_by="engineer@client",
)
UNAPPROVED = Rulebook(
    version="t-0.1", project_id="t", cover_mm={"pile": 75},
    stock_length_mm=9000, rounding_mm=5, spacing_convention="floor_plus_one",
)
SHEET = SheetRef(document_id="d.pdf", sheet_no="S101")


def _pile(*, spiral_cover: int | None = 75, spiral_run: int | None = 12250,
          spiral_count: int | None = None) -> Element:
    ident = ElementIdentity(project_id="t", element_type=ElementType.PILE, mark="P1")
    longitudinal = Reinforcement(
        bar_size=BarSize.M30, role=RebarRole.LONGITUDINAL, count=12,
        bend_type="2", legs={"A": 510, "B": 11955}, shape_code="30A01",
        cover_condition="pile", source=[SHEET], source_tier=SourceTier.NATIVE_TEXT,
    )
    spiral = Reinforcement(
        bar_size=BarSize.M15, role=RebarRole.SPIRAL, count=spiral_count,
        spacing_mm=350, run_length_mm=spiral_run, cover_mm=spiral_cover,
        cover_condition="pile" if spiral_cover is None else None,
        bend_type="T3", legs={"A": 140, "B": 140, "C": 2545, "G": 300, "O": 810},
        shape_code="15A01", source=[SHEET], source_tier=SourceTier.NATIVE_TEXT,
    )
    return Element(
        identity=ident, element_type=ElementType.PILE, mark="P1", instances=6,
        instance_basis=Provenance(pattern=CountingPattern.SCHEDULE_QTY, evidence=[SHEET]),
        geometry=Geometry(primitive="cylinder", params={"diameter": 1000, "length": 11150},
                          placements=[Placement(x=i * 3000, y=0) for i in range(6)]),
        reinforcement=[longitudinal, spiral], sheets=[SHEET],
    )


# -- partial release -------------------------------------------------------
def test_partial_release_when_spiral_cover_missing():
    """One unresolved spiral must not suppress resolved longitudinal bars."""
    element = _pile(spiral_cover=None, spiral_run=None)
    element.reinforcement[1].cover_condition = None
    element.reinforcement[1].unknown_fields = ["cover_mm", "run_length_mm"]
    schedule = RebarCalculator(APPROVED).calculate("t", [element])
    sizes = {i.size for i in schedule.items}
    assert BarSize.M30 in sizes, "longitudinal bars should still be produced"
    assert BarSize.M15 not in sizes, "spiral should be an exception"
    assert any("spiral" in e for e in schedule.exceptions)


def test_element_blocker_stops_every_item():
    element = _pile()
    element.instance_basis = None
    schedule = RebarCalculator(APPROVED).calculate("t", [element])
    assert not schedule.items
    assert any("instance_basis" in e for e in schedule.exceptions)


# -- count vs spacing ------------------------------------------------------
def test_spacing_and_explicit_count_conflict():
    """36 explicit against 35 spacing-derived must raise, not silently prefer."""
    assert derive_spacing_count(11955, 350, "floor_plus_one") == 35
    r = _pile(spiral_count=36, spiral_run=11955).reinforcement[1]
    with pytest.raises(QuantityConflict) as exc:
        RebarCalculator(APPROVED).bar_count(r, 6)
    assert "36" in str(exc.value) and "35" in str(exc.value)


def test_agreeing_count_and_spacing_passes():
    r = _pile(spiral_count=35, spiral_run=11955).reinforcement[1]
    assert RebarCalculator(APPROVED).bar_count(r, 6) == 210


# -- cover resolution ------------------------------------------------------
def test_cover_is_not_resolved_from_an_unrelated_condition():
    """A pile must never inherit a slab_top cover because no pile rule exists."""
    book = Rulebook(version="x", project_id="t", cover_mm={"slab_top": 50},
                    spacing_convention="floor", approved_by="e")
    with pytest.raises(RuleNotFound):
        book.resolve_cover("pile", "spiral", None)


def test_stated_cover_beats_the_rulebook():
    calc = RebarCalculator(APPROVED)
    element = _pile(spiral_cover=60)
    value, key = calc.resolve_cover(element, element.reinforcement[1])
    assert (value, key) == (60, "stated_on_drawing")


# -- release policy --------------------------------------------------------
def _chain(*, approved: bool, gate: GateVector) -> EvidenceChain:
    c = EvidenceChain("r")
    c.append(claim_type=ClaimType.EXTRACTION, subject="S", value=1)
    c.append(claim_type=ClaimType.INTERPRETATION, subject="S", value=1)
    c.append(claim_type=ClaimType.RULE_APPLICATION, subject="S", value=1,
             rulebook_approved=approved)
    c.append(claim_type=ClaimType.CALCULATION, subject="S", value=1, gates=gate)
    return c


PASSING = GateVector(g1_fields_complete=True, g2_no_conflict=True, g3_rule_resolved=True,
                     g4_source_quality=SourceTier.NATIVE_TEXT, g5_pattern_known=True)
REVIEWING = GateVector(g1_fields_complete=True, g2_no_conflict=True, g3_rule_resolved=False,
                       g4_source_quality=SourceTier.NATIVE_TEXT, g5_pattern_known=True)


def test_unapproved_rulebook_not_releasable():
    d = _chain(approved=False, gate=PASSING).release_status("S")
    assert d.state is ReleaseState.REVIEW
    assert "rulebook" in d.reason


def test_review_gate_requires_approval_record():
    d = _chain(approved=True, gate=REVIEWING).release_status("S")
    assert d.state is ReleaseState.REVIEW


def test_approval_after_calculation_releases_a_review_item():
    c = _chain(approved=True, gate=REVIEWING)
    c.append(claim_type=ClaimType.APPROVAL, subject="S", value=True,
             approver="engineer@client", rationale="checked against detail 4/S104")
    assert c.release_status("S").state is ReleaseState.RELEASED


def test_correction_after_calculation_blocks_release():
    c = _chain(approved=True, gate=PASSING)
    c.correct(c.records[-1].record_id, subject="S", value=2)
    assert c.release_status("S").state is ReleaseState.BLOCK


def test_incomplete_chain_blocks():
    c = EvidenceChain("r")
    c.append(claim_type=ClaimType.EXTRACTION, subject="S", value=1)
    d = c.release_status("S")
    assert d.state is ReleaseState.BLOCK and "calculation" in d.missing_claims


# -- workflow strictness ---------------------------------------------------
def test_strict_runner_refuses_to_skip_a_step():
    run = WorkflowRunner(strict=True).execute(Run(project_id="t"))
    assert run.state.value == "failed"
    assert "no registered handler" in run.results[1].error


def test_scaffold_mode_still_available_for_tests():
    run = WorkflowRunner(strict=False).execute(Run(project_id="t"))
    assert run.state.value == "completed"


# -- controls --------------------------------------------------------------
def test_controls_have_evidence_or_rationale():
    element = _pile()
    schedule = RebarCalculator(APPROVED).calculate("t", [element])
    results = evaluate([element], schedule, APPROVED, _chain(approved=True, gate=PASSING))
    assert results
    for c in results:
        assert c.rationale, f"{c.control_id} has no rationale"
        if c.status is ControlStatus.PASS:
            assert c.rulebook_ver or c.evidence


def test_control_c002_flags_a_count_conflict():
    element = _pile(spiral_count=36, spiral_run=11955)
    schedule = RebarCalculator(APPROVED).calculate("t", [element])
    results = {c.control_id: c for c in evaluate([element], schedule, APPROVED, None)}
    assert results["C-002"].status is ControlStatus.REVIEW


# -- project knowledge -----------------------------------------------------
def test_approved_knowledge_is_scoped_and_reused():
    pk = ProjectKnowledge("t")
    pk.approve("cover_mm", 50, Scope(project_id="t", element_type="pile", mark="P1"),
               "estimator@client", "confirmed on call")
    assert pk.lookup("cover_mm", element_type="pile", mark="P1").value == 50
    assert pk.lookup("cover_mm", element_type="pile", mark="P2") is None
    assert pk.reuse_count == 1


def test_more_specific_scope_wins():
    pk = ProjectKnowledge("t")
    pk.approve("cover_mm", 40, Scope(project_id="t", element_type="pile"), "e", "general")
    pk.approve("cover_mm", 75, Scope(project_id="t", element_type="pile", mark="P1"),
               "e", "specific to P1")
    assert pk.lookup("cover_mm", element_type="pile", mark="P1").value == 75


# -- 3D --------------------------------------------------------------------
def test_scene_instantiates_six_placements():
    g = ProjectKnowledgeGraph("t")
    g.add(_pile())
    scene = build_scene(g)
    assert len(scene.nodes) == 1
    assert len(scene.nodes[0].placements) == 6, "six piles, not one element node"


def test_scene_omits_unresolved_reinforcement():
    element = _pile()
    element.reinforcement[1].unknown_fields = ["cover_mm"]
    g = ProjectKnowledgeGraph("t")
    g.add(element)
    scene = build_scene(g)
    assert len(scene.nodes[0].bars) == 1, "unvalidated steel must not be drawn"


# -- ROI -------------------------------------------------------------------
def test_roi_metrics_recorded():
    runner = WorkflowRunner(strict=False)

    @runner.handler("receive_drawings")
    def _h(run):
        return {"_metrics": {"values_extracted": 4, "pages_or_items_processed": 1}}

    run = runner.execute(Run(project_id="t"))
    roi = run.roi()
    assert roi["values_extracted"] == 4
    assert roi["machine_ms"] >= 0
    assert any(s["name"] == "receive_drawings" for s in roi["steps"])


# -- real pipeline ---------------------------------------------------------
CORPUS = os.getenv("TRUSTSIGHT_CORPUS")


@pytest.mark.skipif(not CORPUS or not Path(CORPUS).exists(), reason="corpus absent")
def test_real_drawing_yields_facts_and_asks_for_the_rest():
    from trustsight.extraction.drawings import read_pile_project

    drawing = next(Path(CORPUS).glob("*Atlanic*/Input*.pdf"))
    result = read_pile_project(drawing, "atlantic")
    assert result.elements, "extraction produced no element"
    element = result.elements[0]
    assert element.instances == 6
    assert element.geometry.params["diameter"] == 1000
    fields = {f.field for f in result.facts}
    assert {"pile_diameter_mm", "pile_length_mm", "longitudinal", "transverse"} <= fields
    # the drawing does not state the reinforced run, and the pipeline says so
    assert any("run length" in u for u in result.unresolved)


@pytest.mark.skipif(not CORPUS or not Path(CORPUS).exists(), reason="corpus absent")
def test_pipeline_reproduces_reference_after_clarification():
    from trustsight.evidence.fabric import EvidenceChain as EC
    from trustsight.extraction.barlist import parse_bar_list
    from trustsight.services.pipeline import PipelineContext, answer, build_runner, rerun

    project = next(Path(CORPUS).glob("*Atlanic*"))
    ctx = PipelineContext(
        project_id="atlantic", document=next(project.glob("Input*.pdf")),
        rulebook=APPROVED, graph=ProjectKnowledgeGraph("atlantic"),
        chain=EC("t"), knowledge=ProjectKnowledge("atlantic"),
    )
    run = build_runner(ctx).execute(Run(project_id="atlantic"))
    assert ctx.questions, "pass 1 must ask rather than guess"
    assert not (ctx.schedule and ctx.schedule.items), "nothing releases before answers"

    for field_name, value, role in (
        ("legs", {"A": 510, "B": 11955}, "longitudinal"),
        ("bend_type", "2", "longitudinal"),
        ("run_length_mm", 12250, "spiral"),
        ("legs", {"A": 140, "B": 140, "C": 2545, "G": 300, "O": 810}, "spiral"),
        ("bend_type", "T3", "spiral"),
    ):
        answer(ctx, field_name=field_name, value=value,
               scope=Scope(project_id="atlantic", element_type="pile",
                           mark="P-A", role=role),
               approver="estimator@client", rationale="confirmed with estimator")

    rerun(ctx, "atlantic")
    reference = parse_bar_list(next(project.glob("Output*.pdf")))
    assert ctx.schedule is not None
    assert sum(i.quantity for i in ctx.schedule.items) == sum(
        i.quantity for i in reference.items)
    assert round(ctx.schedule.total_mass_kg, 1) == round(reference.total_mass_kg, 1)
