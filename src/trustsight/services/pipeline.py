"""The demo pipeline: PDF in, governed BBS out.

Registers concrete handlers against the 23-step workflow so a run actually
executes. The same service functions are what the Lambda handlers wrap, so
local and cloud behaviour cannot drift.

Partial completion is the expected outcome. The pile case releases its
longitudinal bars once the cutting-length basis is approved, while the
spiral stays in clarification until the estimator supplies the reinforced
run length. Nothing is defaulted to make the demo look complete.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..engine import controls
from ..engine.calculator import RebarCalculator
from ..engine.rulebook import Rulebook
from ..engine.spatial import build_scene
from ..evidence.fabric import ClaimType, EvidenceChain
from ..extraction import playbooks
from ..extraction.drawings import read_pile_project
from ..graph.knowledge_graph import ProjectKnowledgeGraph
from ..knowledge.project import ProjectKnowledge, Scope
from ..models.core import (
    BarSchedule,
    BarSize,
    CountingPattern,
    Element,
    ElementIdentity,
    ElementType,
    Geometry,
    Placement,
    Provenance,
    RebarRole,
    Reinforcement,
    SheetRef,
    SourceTier,
)
from ..workflow.runner import Run, WorkflowRunner

#: Fixed id for the seeded walkthrough (spec s16/s20 "resettable seeded demo
#: data"). Lets the five-screen UI be driven end to end without a PDF
#: corpus: same handlers, same gates, same evidence chain, only the
#: extraction step is swapped for a synthetic drawing read.
SEED_PROJECT_ID = "atlantic-demo"


@dataclass
class PipelineContext:
    """Everything one run owns. Held per run_id by the API."""

    project_id: str
    document: Path
    rulebook: Rulebook
    graph: ProjectKnowledgeGraph
    chain: EvidenceChain
    knowledge: ProjectKnowledge
    schedule: BarSchedule | None = None
    control_results: list[controls.ControlResult] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    demo_scenario: str = "clarification"
    #: which playbook read this document set; None until extraction runs
    playbook: str | None = None

    @property
    def primary_document(self) -> Path:
        """The sheet to render and cite.

        ``document`` may be the whole drawing set, because an element's
        instance count can depend on how many documents describe it. Anything
        that needs a single renderable PDF — the viewer, the page count —
        asks for this instead.
        """
        if self.document.is_dir():
            found = (sorted(self.document.glob("Input*.pdf"))
                     or sorted(self.document.glob("*.pdf")))
            if found:
                return found[0]
        return self.document


def _apply_known(ctx: PipelineContext, element: Element) -> int:
    """Fill reinforcement gaps from approved project knowledge only."""
    reused = 0
    for r in element.reinforcement:
        if r.cover_mm is None:
            fact = ctx.knowledge.lookup(
                "cover_mm", element_type=element.element_type.value,
                mark=element.mark, role=r.role.value)
            if fact is not None:
                r.cover_mm = int(fact.value)  # type: ignore[arg-type]
                if "cover_mm" in r.unknown_fields:
                    r.unknown_fields.remove("cover_mm")
                reused += 1
        if not r.legs:
            fact = ctx.knowledge.lookup(
                "legs", element_type=element.element_type.value,
                mark=element.mark, role=r.role.value)
            if fact is not None:
                r.legs = dict(fact.value)  # type: ignore[arg-type]
                for name, attr in (("bend_type", "bend_type"),
                                   ("shape_code", "shape_code")):
                    known = ctx.knowledge.lookup(
                        name, element_type=element.element_type.value,
                        mark=element.mark, role=r.role.value)
                    if known is not None and getattr(r, attr) is None:
                        setattr(r, attr, known.value)
                reused += 1
        if r.role in (RebarRole.SPIRAL, RebarRole.TIE) and r.run_length_mm is None:
            fact = ctx.knowledge.lookup(
                "run_length_mm", element_type=element.element_type.value,
                mark=element.mark, role=r.role.value)
            if fact is not None:
                r.run_length_mm = int(fact.value)  # type: ignore[arg-type]
                if "run_length_mm" in r.unknown_fields:
                    r.unknown_fields.remove("run_length_mm")
                reused += 1
    return reused


def _questions_for(ctx: PipelineContext) -> list[dict[str, Any]]:
    """One precise question per unresolved fact."""
    out: list[dict[str, Any]] = []
    for element in ctx.graph.elements.values():
        for r in element.reinforcement:
            for missing in element.reinforcement_blockers(r):
                out.append({
                    "claim_id": r.claim_id,
                    "element": element.identity.key(),
                    "role": r.role.value,
                    "field": missing,
                    "question": (
                        f"{missing} is not stated on any sheet for "
                        f"{element.mark} ({r.role.value}, {r.bar_size.value}). "
                        f"Please confirm the value and the scope it applies to."
                    ),
                    "blocking": True,
                })
            if not r.legs:
                out.append({
                    "claim_id": r.claim_id,
                    "element": element.identity.key(),
                    "role": r.role.value,
                    "field": "legs",
                    "question": (
                        f"Cutting-length basis for {element.mark} "
                        f"{r.role.value} {r.bar_size.value}: the drawing gives "
                        f"the bar count but not the bar shape or leg "
                        f"dimensions. Please confirm the shape code and legs."
                    ),
                    "blocking": True,
                })
    return out


def build_runner(ctx: PipelineContext, *, strict: bool = False) -> WorkflowRunner:
    """Register handlers for the steps this demo implements."""
    runner = WorkflowRunner(strict=strict)

    @runner.handler("receive_drawings")
    def _receive(run: Run) -> dict[str, Any]:
        ctx.chain.append(claim_type=ClaimType.EXTRACTION,
                         subject="document", value=ctx.document.name)
        return {"document": ctx.document.name, "_metrics": {"pages_or_items_processed": 1}}

    @runner.handler("extraction")
    def _extract(run: Run) -> dict[str, Any]:
        # The playbook is chosen from what the set actually contains. An
        # unrecognised set is reported as unreadable rather than run through
        # the nearest reader and returned as an empty drawing.
        try:
            result, playbook = playbooks.read(ctx.document, ctx.project_id)
        except playbooks.NoPlaybook as exc:
            ctx.unresolved.append(str(exc))
            run.context["elements"] = []
            return {"facts": 0, "elements": 0, "playbook": None,
                    "_metrics": {"pages_or_items_processed": 0,
                                 "values_extracted": 0,
                                 "exceptions_raised": 1}}
        ctx.playbook = playbook.name
        ctx.unresolved.extend(result.unresolved)
        for fact in result.facts:
            ctx.chain.append(
                claim_type=ClaimType.EXTRACTION,
                subject=fact.field,
                value=fact.value,
                source=fact.sheet,
                produced_by=f"native_text/{fact.tier.value}",
            )
        run.context["elements"] = result.elements
        return {
            "facts": len(result.facts),
            "elements": len(result.elements),
            "playbook": playbook.name,
            "_metrics": {
                "pages_or_items_processed": result.pages_processed,
                "values_extracted": result.values_extracted,
                "exceptions_raised": len(result.unresolved),
            },
        }

    @runner.handler("knowledge_graph")
    def _graph(run: Run) -> dict[str, Any]:
        reused = 0
        for element in run.context.get("elements", []):
            reused += _apply_known(ctx, element)
            ctx.graph.add(element)
            # the element inherits the extraction evidence of its facts, so a
            # release decision on the element can see an unbroken chain
            ctx.chain.append(
                claim_type=ClaimType.EXTRACTION,
                subject=element.identity.key(),
                value={"source": element.sheets[0].cite() if element.sheets else None},
                source=element.sheets[0] if element.sheets else None,
                produced_by=f"extraction.{ctx.playbook}",
            )
            ctx.chain.append(
                claim_type=ClaimType.INTERPRETATION,
                subject=element.identity.key(),
                value={"instances": element.instances,
                       "reinforcement": len(element.reinforcement)},
                source=element.sheets[0] if element.sheets else None,
            )
        ctx.graph.find_spatial_duplicates()
        return {**ctx.graph.stats(), "_metrics": {"reused_knowledge_count": reused}}

    @runner.handler("missing_conflict_check")
    def _missing(run: Run) -> dict[str, Any]:
        ctx.questions = _questions_for(ctx)
        return {"open_questions": len(ctx.questions),
                "_metrics": {"exceptions_raised": len(ctx.questions)}}

    @runner.handler("rulebook")
    def _rulebook(run: Run) -> dict[str, Any]:
        for key in ctx.graph.elements:
            ctx.chain.append(
                claim_type=ClaimType.RULE_APPLICATION,
                subject=key,
                value=ctx.rulebook.version,
                rulebook_ver=ctx.rulebook.version,
                rulebook_approved=ctx.rulebook.is_approved(),
            )
        return {"rulebook": ctx.rulebook.version,
                "approved": ctx.rulebook.is_approved()}

    @runner.handler("deterministic_calculation")
    def _calculate(run: Run) -> dict[str, Any]:
        calc = RebarCalculator(ctx.rulebook)
        ctx.schedule = calc.calculate(ctx.project_id, list(ctx.graph.elements.values()))
        for item in ctx.schedule.items:
            ctx.chain.append(
                claim_type=ClaimType.CALCULATION,
                subject=item.element_key or "",
                value={"quantity": item.quantity,
                       "cutting_length_mm": item.cutting_length_mm},
                gates=item.gates,
                rulebook_ver=ctx.rulebook.version,
                rulebook_approved=ctx.rulebook.is_approved(),
            )
        run.context["exceptions"] = ctx.schedule.exceptions
        return {
            "items": len(ctx.schedule.items),
            "exceptions": len(ctx.schedule.exceptions),
            "total_mass_kg": round(ctx.schedule.total_mass_kg, 1),
            "_metrics": {"exceptions_raised": len(ctx.schedule.exceptions)},
        }

    @runner.handler("qa")
    def _controls(run: Run) -> dict[str, Any]:
        ctx.control_results = controls.evaluate(
            list(ctx.graph.elements.values()),
            ctx.schedule or BarSchedule(project_id=ctx.project_id),
            ctx.rulebook,
            ctx.chain,
        )
        return {c.control_id: c.status.value for c in ctx.control_results}

    @runner.handler("approval_gate")
    def _approval_gate(run: Run) -> dict[str, Any]:
        """Human gate (spec step 19): required only by policy, here an
        unapproved rulebook. Facts can be fully known and still calculate
        to nothing releasable — I3, calculated does not equal releasable."""
        if ctx.rulebook.is_approved():
            return {"required": False}
        token = uuid.uuid4().hex
        raise WorkflowRunner.Suspend(token, question={
            "kind": "rulebook_approval",
            "rulebook_version": ctx.rulebook.version,
            "message": (
                f"Rulebook {ctx.rulebook.version} has not been signed off by an "
                f"engineer. Quantities are calculated but nothing can release "
                f"until this approval exists."
            ),
        })

    @runner.handler("generate_bbs")
    def _emit(run: Run) -> dict[str, Any]:
        scene = build_scene(ctx.graph)
        return {
            "bbs_items": len(ctx.schedule.items) if ctx.schedule else 0,
            "scene_nodes": len(scene.nodes),
            "findings": len(scene.findings),
        }

    return runner


def _seeded_pile_elements(project_id: str, scenario: str = "clarification") -> list[Element]:
    """The Atlantic Cages P1 pile, written by hand instead of read from a PDF.

    Same shape a real extraction would produce for this project (spec s12).
    For "clarification"/"conflict", longitudinal has no leg dimensions and
    the spiral's reinforced run length is unstated — genuine blockers
    resolved through the same clarification path a real run uses, not a
    canned answer. For "structured"/"approval" every fact is already known,
    so the only thing standing between calculation and release is whether
    the rulebook itself has been signed off (spec s15 structured path).
    """
    resolved = scenario in ("structured", "approval")
    plan = SheetRef(document_id="DR-612-E-1319.pdf", sheet_no="S101")
    section = SheetRef(document_id="DR-612-E-1319.pdf", sheet_no="S103")
    identity = ElementIdentity(project_id=project_id, element_type=ElementType.PILE, mark="P1")
    longitudinal = Reinforcement(
        bar_size=BarSize.M30, role=RebarRole.LONGITUDINAL, count=12,
        bend_type="2", shape_code="30A01",
        legs={"A": 510, "B": 11955} if resolved else {},
        cover_condition="pile", source=[section], source_tier=SourceTier.NATIVE_TEXT,
    )
    spiral = Reinforcement(
        bar_size=BarSize.M15, role=RebarRole.SPIRAL, spacing_mm=350,
        run_length_mm=12250 if resolved else None,
        bend_type="T3", shape_code="15A01",
        legs={"A": 140, "B": 140, "C": 2545, "G": 300, "O": 810},
        cover_condition="pile", source=[section], source_tier=SourceTier.NATIVE_TEXT,
        unknown_fields=[] if resolved else ["run_length_mm"],
    )
    element = Element(
        identity=identity, element_type=ElementType.PILE, mark="P1", instances=6,
        instance_basis=Provenance(
            pattern=CountingPattern.SCHEDULE_QTY, evidence=[plan],
            detail="pile schedule: 6 off, type P1",
        ),
        geometry=Geometry(
            primitive="cylinder", params={"diameter": 1000, "length": 11150},
            placements=[Placement(x=i * 2500, y=0, sheet=plan) for i in range(6)],
        ),
        reinforcement=[longitudinal, spiral],
        sheets=[plan, section],
    )
    return [element]


def build_seeded_runner(ctx: PipelineContext) -> WorkflowRunner:
    """Same 23-step registration as :func:`build_runner`, extraction swapped
    for the synthetic Atlantic drawing so the demo needs no PDF corpus."""
    runner = build_runner(ctx)

    @runner.handler("extraction")
    def _seed_extract(run: Run) -> dict[str, Any]:
        elements = _seeded_pile_elements(ctx.project_id, ctx.demo_scenario)
        if ctx.demo_scenario == "conflict":
            elements[0].conflicts.append(
                "Sample conflict: schedule lists 6 piles; plan lists 8. "
                "Confirm the authoritative count before any release."
            )
        ctx.playbook = "pile_v1"
        ctx.chain.append(claim_type=ClaimType.EXTRACTION, subject="document",
                         value=ctx.document.name, produced_by="seeded_demo_data")
        run.context["elements"] = elements
        return {
            "facts": 2, "elements": len(elements), "playbook": ctx.playbook,
            "_metrics": {"pages_or_items_processed": 2, "values_extracted": 2,
                        "exceptions_raised": 0},
        }

    return runner


def answer(ctx: PipelineContext, *, field_name: str, value: Any, scope: Scope,
           approver: str, rationale: str) -> None:
    """Record a human answer as approved project knowledge."""
    fact = ctx.knowledge.approve(field_name, value, scope, approver, rationale)
    ctx.chain.append(
        claim_type=ClaimType.APPROVAL,
        subject=scope.describe(),
        value={"field": field_name, "value": value, "version": fact.version},
        approver=approver,
        rationale=rationale,
    )


def rerun(ctx: PipelineContext, project_id: str) -> Run:
    """Re-execute after new approved knowledge has been supplied."""
    run = Run(project_id=project_id, run_id=ctx.chain.run_id)
    run.versions = {"rulebook": ctx.rulebook.version}
    ctx.graph.elements.clear()
    ctx.graph.edges.clear()
    ctx.questions.clear()
    ctx.unresolved.clear()
    runner = build_seeded_runner(ctx) if project_id == SEED_PROJECT_ID else build_runner(ctx)
    return runner.execute(run)
