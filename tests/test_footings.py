"""Corpus regression for the ``footing_v1`` playbook.

Skipped when the corpus is absent. Set TRUSTSIGHT_CORPUS to the directory
containing <project>/Input*.pdf and <project>/Output*.pdf.
"""
import os
from pathlib import Path

import pytest

from trustsight.engine.rulebook import Rulebook
from trustsight.evidence.fabric import EvidenceChain
from trustsight.extraction import playbooks
from trustsight.extraction.barlist import parse_bar_list
from trustsight.extraction.footings import read_footing_project
from trustsight.graph.knowledge_graph import ProjectKnowledgeGraph
from trustsight.knowledge.project import ProjectKnowledge, Scope
from trustsight.services.pipeline import (
    PipelineContext, answer, build_runner, rerun,
)
from trustsight.workflow.runner import Run

CORPUS = os.getenv("TRUSTSIGHT_CORPUS")
pytestmark = pytest.mark.skipif(
    not CORPUS or not Path(CORPUS).exists(), reason="reference corpus not available"
)


def _kingston() -> Path:
    found = next(Path(CORPUS).glob("*Kingston*"), None)
    if found is None:
        pytest.skip("Kingston project not in this corpus")
    return found


def test_playbook_selection_picks_footing_for_kingston():
    assert playbooks.select(_kingston()).name == "footing_v1"


def test_playbook_selection_picks_pile_for_atlantic():
    atlantic = next(Path(CORPUS).glob("*Atlanic*"), None)
    if atlantic is None:
        pytest.skip("Atlantic project not in this corpus")
    assert playbooks.select(atlantic).name == "pile_v1"


def test_extraction_reads_the_mat_but_not_the_cutting_length():
    result = read_footing_project(_kingston(), "kingston")
    assert len(result.elements) == 1
    element = result.elements[0]

    # three RFI documents, one foundation sketch each
    assert element.instances == 3
    reinforcement = element.reinforcement[0]
    # "6 - 20M BARS (TOP & BOTTOM, EW)" -> 6 x 2 layers x 2 directions
    assert reinforcement.count == 24
    assert reinforcement.bar_size.value == "20M"

    # the sketch states no shape, so no cutting length may be invented
    assert not reinforcement.legs
    assert any("cutting length" in u for u in result.unresolved)

    fields = {f.field for f in result.facts}
    assert {"footing_plan_mm", "mat_reinforcement"} <= fields


def test_two_stated_covers_defer_to_the_rulebook():
    """The sketch annotates more than one CLR value and marks no governing
    face, so the reader must not pick one."""
    result = read_footing_project(_kingston(), "kingston")
    covers = [f.value for f in result.facts if f.field == "cover_mm"]
    if len(covers) > 1:
        assert result.elements[0].reinforcement[0].cover_mm is None
        assert any("more than one cover" in u for u in result.unresolved)


def test_kingston_matches_the_reference_after_approval():
    project = _kingston()
    reference = parse_bar_list(next(project.glob("Output*.pdf")))

    rulebook = Rulebook(
        version="kingston-1.0", project_id="kingston", cover_mm={"footing": 40},
        stock_length_mm=9000, rounding_mm=5, spacing_convention="floor_plus_one",
        approved_by="test",
    )
    ctx = PipelineContext(
        project_id="kingston", document=project, rulebook=rulebook,
        graph=ProjectKnowledgeGraph("kingston"), chain=EvidenceChain("t"),
        knowledge=ProjectKnowledge("kingston"),
    )

    build_runner(ctx).execute(Run(project_id="kingston"))
    assert ctx.playbook == "footing_v1"
    # pass 1: the bars are counted but nothing can be released
    assert ctx.schedule is not None
    assert not ctx.schedule.items
    assert ctx.questions

    scope = Scope(project_id="kingston", element_type="footing",
                  mark="CS-01", role="each_way")
    for field_name, value in (("legs", {"B": 400, "C": 1120, "D": 400}),
                              ("bend_type", "17"),
                              ("shape_code", "20A01")):
        answer(ctx, field_name=field_name, value=value, scope=scope,
               approver="test", rationale="reference detailing convention")

    rerun(ctx, "kingston")
    assert len(ctx.schedule.items) == 1
    item = ctx.schedule.items[0]
    assert item.quantity == sum(i.quantity for i in reference.items) == 72
    assert item.cutting_length_mm == 1920
    assert item.size.value == "20M"
    assert item.mark == "20A01"
    assert abs(item.total_mass_kg - reference.total_mass_kg) < 0.05


def test_selector_does_not_claim_projects_it_cannot_read():
    """Coverage must be stated honestly.

    Matching on the word FOUNDATION alone claims three of the five reference
    projects and delivers one. The selector has to require the anchor each
    reader actually depends on.
    """
    readable = {}
    for project in sorted(p for p in Path(CORPUS).iterdir() if p.is_dir()):
        try:
            readable[project.name] = playbooks.select(project).name
        except playbooks.NoPlaybook:
            readable[project.name] = None

    claimed = {k: v for k, v in readable.items() if v}
    assert len(claimed) == 2, f"selector claims {len(claimed)} projects: {claimed}"
    assert any("Kingston" in k and v == "footing_v1" for k, v in claimed.items())
    assert any("Atlanic" in k and v == "pile_v1" for k, v in claimed.items())
