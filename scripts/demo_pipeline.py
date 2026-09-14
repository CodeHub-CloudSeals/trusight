#!/usr/bin/env python3
"""End-to-end demonstrator: real drawing -> governed BBS.

Runs the Atlantic Cages pile drawing through the pipeline, shows what the
drawing alone supports, asks for what it does not state, and re-runs once
the estimator has answered.

    python scripts/demo_pipeline.py "/path/to/AGENT 1ST SET"
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trustsight.engine.rulebook import Rulebook  # noqa: E402
from trustsight.evidence.fabric import EvidenceChain, ReleaseState  # noqa: E402
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
    print(f"run state: {run.state.value}   steps completed: {run.completed_steps()}")

    if ctx.schedule and ctx.schedule.items:
        print(f"\n{'qty':>6} {'size':>5} {'length':>8} {'mark':>7} {'mass kg':>10}  release")
        for it in ctx.schedule.items:
            rel = ctx.chain.release_status(
                it.element_key or "", rulebook_approved=ctx.rulebook.is_approved()
            )
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
          f"{roi['exceptions']} exceptions, "
          f"{roi['reused_knowledge']} knowledge reuses")


def main() -> None:
    corpus = Path(sys.argv[1] if len(sys.argv) > 1 else "./corpus")
    project_dir = next(corpus.glob("*Atlanic*"), None)
    if project_dir is None:
        print(f"Atlantic Cages project not found under {corpus}")
        raise SystemExit(2)
    drawing = next(project_dir.glob("Input*.pdf"))
    reference = parse_bar_list(next(project_dir.glob("Output*.pdf")))

    print("TrustSight — real drawing to governed BBS")
    print(f"input:     {drawing.name}")
    print(f"reference: {sum(i.quantity for i in reference.items)} bars, "
          f"{reference.total_mass_kg:,.1f} kg (ground truth, not shown to the pipeline)")

    rulebook = Rulebook(
        version="atlantic-1.0", project_id="atlantic",
        cover_mm={"pile": 75}, stock_length_mm=9000, rounding_mm=5,
        spacing_convention="floor_plus_one", approved_by="estimator@client",
    )
    ctx = PipelineContext(
        project_id="atlantic", document=drawing, rulebook=rulebook,
        graph=ProjectKnowledgeGraph("atlantic"), chain=EvidenceChain("demo"),
        knowledge=ProjectKnowledge("atlantic"),
    )

    run = build_runner(ctx).execute(Run(project_id="atlantic"))
    show(ctx, run, "PASS 1 — what the drawing alone supports")

    print(f"\n{RULE}\nESTIMATOR ANSWERS THE CLARIFICATIONS\n{RULE}")
    answers = [
        ("legs", {"A": 510, "B": 11955}, "longitudinal",
         "bar projects 510mm into the cap above an 11,955mm body"),
        ("bend_type", "2", "longitudinal", "single end hook, RebarCAD type 2"),
        ("run_length_mm", 12250, "spiral",
         "spiral runs 12,250mm; pitch measured from first to last turn"),
        ("legs", {"A": 140, "B": 140, "C": 2545, "G": 300, "O": 810}, "spiral",
         "standard spiral turn geometry, shape 15A01 type T3"),
        ("bend_type", "T3", "spiral", "spiral shape family"),
    ]
    for field_name, value, role, why in answers:
        answer(ctx, field_name=field_name, value=value,
               scope=Scope(project_id="atlantic", element_type="pile",
                           mark="P-A", role=role),
               approver="estimator@client", rationale=why)
        print(f"  approved {field_name} for {role}: {why}")

    run2 = rerun(ctx, "atlantic")
    show(ctx, run2, "PASS 2 — after approved project knowledge")

    if ctx.schedule:
        got_bars = sum(i.quantity for i in ctx.schedule.items)
        ref_bars = sum(i.quantity for i in reference.items)
        print(f"\n{RULE}\nAGAINST GROUND TRUTH\n{RULE}")
        print(f"  bars      generated {got_bars:>6}   reference {ref_bars:>6}")
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
    print("The pipeline read the drawing, stopped where the drawing was silent,")
    print("asked, and released only what the evidence supported.")
    print(RULE)


if __name__ == "__main__":
    main()
