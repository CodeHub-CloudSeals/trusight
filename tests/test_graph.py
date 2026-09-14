"""Element identity, merge and duplicate detection. Spec section 3."""
from trustsight.graph.knowledge_graph import ProjectKnowledgeGraph
from trustsight.models.core import (
    CountingPattern, Element, ElementIdentity, ElementType, Geometry, Placement,
    Provenance, SheetRef,
)


def _pile(mark, sheet, instances=6, **kw):
    ident = ElementIdentity(project_id="p", element_type=ElementType.PILE, mark=mark)
    return Element(
        identity=ident, element_type=ElementType.PILE, mark=mark,
        instances=instances,
        instance_basis=Provenance(pattern=CountingPattern.SCHEDULE_QTY),
        sheets=[SheetRef(document_id="d", sheet_no=sheet)],
        **kw,
    )


def test_same_element_across_three_sheets_counts_once():
    g = ProjectKnowledgeGraph("p")
    for sheet in ("S101", "S103", "S105"):
        g.add(_pile("P1", sheet))
    assert g.stats()["elements"] == 1
    assert len(g.elements["p|pile|P1"].sheets) == 3


def test_conflicting_instance_counts_are_preserved_not_resolved():
    g = ProjectKnowledgeGraph("p")
    g.add(_pile("P1", "S101", instances=6))
    g.add(_pile("P1", "S103", instances=8))
    el = g.elements["p|pile|P1"]
    assert el.conflicts and "6" in el.conflicts[0] and "8" in el.conflicts[0]
    ready, problems = el.ready_for_calculation()
    assert not ready


def test_provisional_identity_never_merges():
    g = ProjectKnowledgeGraph("p")
    ident = ElementIdentity(project_id="p", element_type=ElementType.PILE)
    assert ident.is_provisional
    g.add(Element(identity=ident, element_type=ElementType.PILE))
    g.add(Element(identity=ident, element_type=ElementType.PILE))
    assert g.stats()["provisional"] == 2
    assert g.stats()["elements"] == 0


def test_spatial_duplicate_flagged():
    g = ProjectKnowledgeGraph("p")
    a = _pile("P1", "S101", geometry=Geometry(placements=[Placement(x=0, y=0)]))
    b = _pile("P2", "S103", geometry=Geometry(placements=[Placement(x=10, y=0)]))
    g.add(a)
    g.add(b)
    assert g.find_spatial_duplicates(tolerance_mm=50)


def test_element_without_instance_basis_cannot_calculate():
    ident = ElementIdentity(project_id="p", element_type=ElementType.PILE, mark="P9")
    el = Element(identity=ident, element_type=ElementType.PILE)
    ready, problems = el.ready_for_calculation()
    assert not ready
    assert any("instance_basis" in p for p in problems)
