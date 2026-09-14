#!/usr/bin/env python3
"""End-to-end demonstrator: the Atlantic Cages pile case.

Shows the same business goal reaching the same answer through the three
input paths, and shows the unstructured path refusing to guess.

    python scripts/demo_pile.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trustsight.engine.calculator import RebarCalculator  # noqa: E402
from trustsight.engine.rulebook import DEMO_RULEBOOK, Rulebook  # noqa: E402
from trustsight.engine.spatial import build_scene, self_check  # noqa: E402
from trustsight.evidence.fabric import ClaimType, EvidenceChain  # noqa: E402
from trustsight.graph.knowledge_graph import ProjectKnowledgeGraph  # noqa: E402
from trustsight.models.core import (  # noqa: E402
    BarSize, CountingPattern, Element, ElementIdentity, ElementType, Geometry,
    Placement, Provenance, RebarRole, Reinforcement, SheetRef, SourceTier,
)

RULE = "-" * 78


def pile(*, cover_known: bool, tier: SourceTier, conflict: bool = False) -> Element:
    """Six P1 piles: 12-30M longitudinal, 15M ties @350."""
    ident = ElementIdentity(project_id="atlantic", element_type=ElementType.PILE, mark="P1")
    plan = SheetRef(document_id="DR-612-E-1319.pdf", sheet_no="S101")
    section = SheetRef(document_id="DR-612-E-1319.pdf", sheet_no="S103")

    longitudinal = Reinforcement(
        bar_size=BarSize.M30, role=RebarRole.LONGITUDINAL, count=12,
        bend_type="2", legs={"A": 510, "B": 11955},
        shape_code="30A01", cover_mm=75 if cover_known else None, lap_mm=0,
        source=[section], source_tier=tier,
    )
    ties = Reinforcement(
        bar_size=BarSize.M15, role=RebarRole.SPIRAL, count=36,
        spacing_mm=350, run_length_mm=11955,
        bend_type="T3", legs={"A": 140, "B": 140, "C": 2545, "G": 300, "O": 810},
        shape_code="15A01", cover_mm=75 if cover_known else None, lap_mm=0,
        source=[section], source_tier=tier,
    )
    el = Element(
        identity=ident, element_type=ElementType.PILE, mark="P1",
        instances=6,
        instance_basis=Provenance(
            pattern=CountingPattern.SCHEDULE_QTY,
            evidence=[plan], detail="pile schedule: 6 off, type P1",
        ),
        geometry=Geometry(
            primitive="cylinder", params={"diameter": 600, "length": 8000},
            placements=[Placement(x=i * 2000, y=0, sheet=plan) for i in range(6)],
        ),
        reinforcement=[longitudinal, ties],
        sheets=[plan, section],
    )
    if conflict:
        el.conflicts.append("instances: 6 (schedule) vs 8 (plan symbol count)")
    return el


def show(title: str, element: Element, rulebook: Rulebook) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")
    graph = ProjectKnowledgeGraph("atlantic")
    graph.add(element)
    chain = EvidenceChain("demo-run")
    chain.append(claim_type=ClaimType.EXTRACTION, subject="atlantic|pile|P1", value="12-30M")
    chain.append(claim_type=ClaimType.INTERPRETATION, subject="atlantic|pile|P1", value="longitudinal")
    chain.append(claim_type=ClaimType.RULE_APPLICATION, subject="atlantic|pile|P1",
                 value=rulebook.version)

    calc = RebarCalculator(rulebook)
    schedule = calc.calculate("atlantic", [element])

    if schedule.items:
        chain.append(claim_type=ClaimType.CALCULATION, subject="atlantic|pile|P1",
                     value=schedule.total_mass_kg)
        print(f"{'qty':>6} {'size':>5} {'length':>8} {'mark':>7} {'mass kg':>10}  decision")
        for it in schedule.items:
            decision = it.gates.decide().value if it.gates else "-"
            print(f"{it.quantity:>6} {it.size.value:>5} {it.cutting_length_mm:>8} "
                  f"{(it.mark or '-'):>7} {it.total_mass_kg:>10.1f}  {decision}")
        print(f"{'':>30}{'TOTAL':>7} {schedule.total_mass_kg:>10.1f} kg")
        for it in schedule.items:
            if it.gates and it.gates.decide().value != "auto_proceed":
                print(f"  gate: {it.gates.explain()}")
    if schedule.exceptions:
        print("EXCEPTIONS — nothing released for these:")
        for e in schedule.exceptions:
            print(f"  - {e}")

    releasable, missing = chain.is_releasable("atlantic|pile|P1")
    ok, msg = chain.verify()
    print(f"evidence: chain {'valid' if ok else 'BROKEN'} ({msg}); "
          f"releasable={releasable}" + (f", missing {missing}" if missing else ""))

    scene = build_scene(graph)
    problems = self_check(scene, schedule)
    bars = sum(len(n.bars) for n in scene.nodes)
    print(f"3D: {len(scene.nodes)} node(s), {bars} bar path(s), "
          f"self-check {'PASS' if not problems else 'FAIL: ' + problems[0]}")
    for f in scene.findings:
        print(f"  finding: {f}")


def main() -> None:
    print("TrustSight demonstrator — Atlantic Cages, six P1 piles")
    print("Reference bar list states: 72 x 30M @ 12,465mm and 216 x 15M @ 3,125mm")

    approved = Rulebook(
        version="atlantic-1.0",
        project_id="atlantic",
        cover_mm={"pile": 75},
        stock_length_mm=9000,
        rounding_mm=5,
        spacing_convention="floor_plus_one",
        approved_by="estimator@client",
    )

    show("STRUCTURED PATH — schedule states the quantity; cover is explicit",
         pile(cover_known=True, tier=SourceTier.NATIVE_TEXT), approved)

    show("UNSTRUCTURED PATH — cover is not stated anywhere on the drawings",
         pile(cover_known=False, tier=SourceTier.VISION), approved)

    show("CONFLICT — the schedule says 6 piles, the plan shows 8",
         pile(cover_known=True, tier=SourceTier.NATIVE_TEXT, conflict=True), approved)

    show("UNAPPROVED RULEBOOK — no engineer has signed off the rules",
         pile(cover_known=True, tier=SourceTier.NATIVE_TEXT), DEMO_RULEBOOK)

    print(f"\n{RULE}\nThe point: the same facts produce a released number only when the "
          f"evidence\nsupports it. Otherwise the system asks, or stops.\n{RULE}")


if __name__ == "__main__":
    main()
