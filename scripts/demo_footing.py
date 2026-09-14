#!/usr/bin/env python3
"""End-to-end demonstrator for the Kingston pad footings (footing_v1).

Same governed loop as the pile demo, second element family:
read the RFI sketches, stop where they are silent, ask, then release.

    python scripts/demo_footing.py "/path/to/AGENT 1ST SET"
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trustsight.engine.rulebook import Rulebook  # noqa: E402
from trustsight.evidence.fabric import EvidenceChain  # noqa: E402
from trustsight.extraction.barlist import parse_bar_list  # noqa: E402
from trustsight.graph.knowledge_graph import ProjectKnowledgeGraph  # noqa: E402
from trustsight.knowledge.project import ProjectKnowledge, Scope  # noqa: E402
from trustsight.services.pipeline import (  # noqa: E402
    PipelineContext, answer, build_runner, rerun,
)
from trustsight.workflow.runner import Run  # noqa: E402

RULE = "=" * 78


def show(ctx: PipelineContext, run: Run, title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")
    print(f"run state: {run.state.value}   playbook: {ctx.playbook}   "
          f"steps: {run.completed_steps()}")

    if ctx.schedule and ctx.schedule.items:
        print(f"\n{'qty':>6} {'size':>5} {'length':>8} {'mark':>7} {'mass kg':>10}  release")
        for it in ctx.schedule.items:
            rel = ctx.chain.release_status(
                it.element_key or "", rulebook_approved=ctx.rulebook.is_approved())
            print(f"{it.quantity:>6} {it.size.value:>5} {it.cutting_length_mm:>8} "
                  f"{(it.mark or '-'):>7} {it.total_mass_kg:>10.1f}  {rel.state.value}")
        print(f"{'':>30}{'TOTAL':>7} {ctx.schedule.total_mass_kg:>10.1f} kg")

    if ctx.schedule and ctx.schedule.exceptions:
        print("\nexceptions (nothing released for these):")
        for e in ctx.schedule.exceptions:
            print(f"  - {e}")

    if ctx.questions:
        print("\nclarification queue:")
        for q in ctx.questions:
            print(f"  - [{q['field']}] {q['question']}")

    if ctx.control_results:
        print("\ncontrols:")
        for c in ctx.control_results:
            print(f"  {c.control_id} {c.status.value:<14} {c.rationale[:58]}")

    roi = run.roi()
    print(f"\nROI (measured): machine {roi['machine_ms']}ms, "
          f"{roi['values_extracted']} values extracted, "
          f"{roi['exceptions']} exceptions, {roi['reused_knowledge']} knowledge reuses")


def main() -> None:
    corpus = Path(sys.argv[1] if len(sys.argv) > 1 else "./corpus")
    project_dir = next(corpus.glob("*Kingston*"), None)
    if project_dir is None:
        print(f"Kingston project not found under {corpus}")
        raise SystemExit(2)
    reference = parse_bar_list(next(project_dir.glob("Output*.pdf")))

    print("TrustSight — RFI sketches to governed BBS (footing_v1)")
    print(f"input:     {project_dir.name} "
          f"({len(list(project_dir.glob('Input*.pdf')))} RFI documents)")
    print(f"reference: {sum(i.quantity for i in reference.items)} bars, "
          f"{reference.total_mass_kg:,.1f} kg (ground truth, not shown to the pipeline)")

    rulebook = Rulebook(
        version="kingston-1.0", project_id="kingston",
        # Two covers are annotated on the sketch and the governing face is
        # not marked, so the approved assumption sheet decides.
        cover_mm={"footing": 40}, stock_length_mm=9000, rounding_mm=5,
        spacing_convention="floor_plus_one", approved_by="estimator@client",
    )
    ctx = PipelineContext(
        project_id="kingston", document=project_dir, rulebook=rulebook,
        graph=ProjectKnowledgeGraph("kingston"), chain=EvidenceChain("demo-kingston"),
        knowledge=ProjectKnowledge("kingston"),
    )

    run = build_runner(ctx).execute(Run(project_id="kingston"))
    show(ctx, run, "PASS 1 — what the sketches alone support")

    print(f"\n{RULE}\nESTIMATOR ANSWERS THE CLARIFICATIONS\n{RULE}")
    answers = [
        ("legs", {"B": 400, "C": 1120, "D": 400}, "each_way",
         "U-bar across the 1,200mm foundation with 400mm legs turned up"),
        ("bend_type", "17", "each_way", "U-shape / hairpin, RebarCAD type 17"),
        ("shape_code", "20A01", "each_way", "project bar mark for the 20M mat bar"),
    ]
    for field_name, value, role, why in answers:
        answer(ctx, field_name=field_name, value=value,
               scope=Scope(project_id="kingston", element_type="footing",
                           mark="CS-01", role=role),
               approver="estimator@client", rationale=why)
        print(f"  approved {field_name} for {role}: {why}")

    run2 = rerun(ctx, "kingston")
    show(ctx, run2, "PASS 2 — after approved project knowledge")

    if ctx.schedule:
        got = sum(i.quantity for i in ctx.schedule.items)
        ref = sum(i.quantity for i in reference.items)
        print(f"\n{RULE}\nAGAINST GROUND TRUTH\n{RULE}")
        print(f"  bars      generated {got:>6}   reference {ref:>6}")
        print(f"  mass kg   generated {ctx.schedule.total_mass_kg:>9,.1f}   "
              f"reference {reference.total_mass_kg:>9,.1f}")
        for it in ctx.schedule.items:
            match = [r for r in reference.items
                     if r.size == it.size and r.cutting_length_mm == it.cutting_length_mm]
            verdict = (f"matches reference ({match[0].quantity} bars)" if match
                       else "no reference item at this size and length")
            delta = ("" if match and match[0].quantity == it.quantity
                     else "  <-- QUANTITY DIFFERS")
            print(f"  {it.quantity:>4} x {it.size.value} @ {it.cutting_length_mm}mm: "
                  f"{verdict}{delta}")

    print(f"\n{RULE}")


if __name__ == "__main__":
    main()
