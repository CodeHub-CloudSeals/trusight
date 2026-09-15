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
from ..evidence.fabric import ClaimType, EvidenceChain, claim_subject
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
from ..workflow import routes
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
    #: requested input quality: structured | semi_structured | unstructured
    path_mode: str = "semi_structured"
    #: the route actually taken, set when the runner is built
    route: str | None = None
    #: The estimator's current time for this scope, in minutes, supplied at
    #: run start. None means no baseline was given — and then no time saving
    #: is reported at all rather than one computed against a guess.
    manual_baseline_minutes: int | None = None
    #: Measured review time: wall clock between a question being raised and
    #: its answer arriving. Accumulated by the API, never estimated.
    human_seconds: float = 0.0
    #: monotonic timestamp of the moment the run last stopped on a human
    awaiting_since: float | None = None
    #: how many iterations this run has been through (spec s14.2). Approvals
    #: keep the same run id and increment this, so a client can see that the
    #: second pass is the same job continuing, not a fresh run.
    iteration: int = 1
    #: NOTIFY-101 bookkeeping: when each alert was first raised and which have
    #: been read. The alerts themselves are derived from run state, so this
    #: holds only what state cannot tell you.
    alerts: Any = None

    def start_waiting(self) -> None:
        """Mark the moment the run handed work back to a person."""
        import time
        if self.questions and self.awaiting_since is None:
            self.awaiting_since = time.monotonic()

    def stop_waiting(self) -> None:
        """Bank the review time that just elapsed."""
        import time
        if self.awaiting_since is not None:
            self.human_seconds += time.monotonic() - self.awaiting_since
            self.awaiting_since = None

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
            if r.legs and r.bend_type is None:
                # Legs alone do not give a cutting length: the shape decides
                # which columns are summed and which are geometry only (a T3
                # spiral carries a value in O that is excluded). Without this
                # question an answered "legs" looked complete while the
                # calculation had no shape to apply, and the run released a
                # number built from the wrong columns.
                out.append({
                    "claim_id": r.claim_id,
                    "element": element.identity.key(),
                    "role": r.role.value,
                    "field": "bend_type",
                    "question": (
                        f"Bar shape for {element.mark} {r.role.value} "
                        f"{r.bar_size.value}: leg dimensions "
                        f"{'+'.join(sorted(r.legs))} are approved but the bend "
                        f"type is not stated on any sheet. The shape decides "
                        f"which legs are summed, so please confirm it (for "
                        f"example 2 for a single end hook, 17 for a U-bar, "
                        f"T3 for a spiral)."
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


def build_runner(ctx: PipelineContext, *, strict: bool = True,
                 route: routes.Route | None = None) -> WorkflowRunner:
    """Register handlers for the steps this demo implements.

    The route decides which steps are required. Strict is the default: a
    step this route depends on that has no handler fails the run rather than
    letting it report success with interpretation skipped.
    """
    route = route or routes.get(ctx.path_mode)
    ctx.route = route.name
    runner = WorkflowRunner(strict=strict, required=route.requires())

    @runner.handler("receive_drawings")
    def _receive(run: Run) -> dict[str, Any]:
        ctx.chain.append(claim_type=ClaimType.EXTRACTION,
                         subject="document", value=ctx.document.name)
        return {"document": ctx.document.name, "_metrics": {"pages_or_items_processed": 1}}

    @runner.handler("preflight")
    def _preflight(run: Run) -> dict[str, Any]:
        """What kind of document is this, before anything tries to read it.

        The source tier is a governance input, not a diagnostic: a value
        read from a native text layer and one inferred from a scan do not
        deserve the same confidence, and the gate vector carries that
        difference all the way to the release decision.
        """
        from ..extraction.pdf import extract as probe
        paths = ([ctx.document] if not ctx.document.is_dir()
                 else sorted(ctx.document.glob("Input*.pdf"))
                 or sorted(ctx.document.glob("*.pdf")))
        tiers, needs_vision, pages = [], False, 0
        for path in paths[:8]:
            try:
                doc = probe(path)
            except Exception:      # a file we cannot open is a finding
                ctx.unresolved.append(f"{path.name}: could not be opened")
                continue
            tiers.append(doc.tier.value)
            pages += len(doc.sheets)
            needs_vision |= any(sheet.needs_vision for sheet in doc.sheets)
        run.context["source_tiers"] = sorted(set(tiers))
        run.context["needs_vision"] = needs_vision
        return {"documents": len(paths), "pages": pages,
                "tiers": sorted(set(tiers)), "needs_vision": needs_vision,
                "_metrics": {"pages_or_items_processed": pages}}

    @runner.handler("playbook_retrieval")
    def _playbook(run: Run) -> dict[str, Any]:
        """Keyed lookup, never generation (spec s5).

        Selection is its own step because choosing the wrong reader is a
        different failure from reading badly, and only one of the two is
        visible in the output.
        """
        if ctx.project_id == SEED_PROJECT_ID:
            ctx.playbook = "pile_v1"
            return {"playbook": ctx.playbook, "basis": "seeded walkthrough"}
        try:
            chosen = playbooks.select(ctx.document)
        except playbooks.NoPlaybook as exc:
            ctx.unresolved.append(str(exc))
            return {"playbook": None, "basis": "no playbook recognises this set"}
        ctx.playbook = chosen.name
        ctx.chain.append(claim_type=ClaimType.RULE_APPLICATION,
                         subject=f"playbook/{chosen.name}",
                         value={"element_family": chosen.element_family,
                                "description": chosen.description})
        return {"playbook": chosen.name, "element_family": chosen.element_family,
                "basis": "anchors present in the drawing set"}

    @runner.handler("cross_sheet_resolution")
    def _cross_sheet(run: Run) -> dict[str, Any]:
        """Which sheets contributed to which element.

        The client's question is "why do you believe these facts describe one
        element". The answer is the sheet set behind it, recorded rather than
        asserted.
        """
        by_element: dict[str, list[str]] = {}
        for element in ctx.graph.elements.values():
            sheets = sorted({s.sheet_no or s.document_id
                             for s in element.sheets if s})
            by_element[element.identity.key()] = sheets
            if len(sheets) > 1:
                ctx.chain.append(
                    claim_type=ClaimType.INTERPRETATION,
                    subject=element.identity.key(),
                    value={"resolved_across_sheets": sheets},
                    produced_by="deterministic cross-sheet merge")
        run.context["sheets_per_element"] = by_element
        multi = sum(1 for v in by_element.values() if len(v) > 1)
        return {"elements": len(by_element), "multi_sheet_elements": multi}

    @runner.handler("clarification")
    def _clarify(run: Run) -> dict[str, Any]:
        """Turn each blocker into one precise question.

        This step writes the question and never the answer. A question that
        proposes its own answer is an assumption with a question mark on it.
        """
        ctx.questions = _questions_for(ctx)
        for q in ctx.questions:
            ctx.chain.append(claim_type=ClaimType.INTERPRETATION,
                             subject=q["element"],
                             value={"question": q["field"], "blocking": True})
        return {"questions": len(ctx.questions),
                "_metrics": {"exceptions_raised": len(ctx.questions)}}

    @runner.handler("approved_knowledge")
    def _approved(run: Run) -> dict[str, Any]:
        """Approved facts reused on this pass, with their scope.

        Reuse is only legitimate while the scope predicate matches. One
        project's assumption becoming a global rule is a governance action,
        not a side effect of a second run.
        """
        facts = ctx.knowledge.all() if hasattr(ctx.knowledge, "all") else []
        applied = [{"field": f.field, "scope": f.scope.describe(),
                    "approver": f.approver, "version": f.version}
                   for f in facts]
        run.context["approved_knowledge"] = applied
        return {"approved_facts": len(applied),
                "_metrics": {"reused_knowledge_count": len(applied)}}

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
            # and once per claim, so each reinforcement item owns a complete
            # chain rather than borrowing the element's
            for r in element.reinforcement:
                for kind in (ClaimType.EXTRACTION, ClaimType.INTERPRETATION):
                    ctx.chain.append(
                        claim_type=kind,
                        subject=claim_subject(element.identity.key(), r.claim_id),
                        value={"role": r.role.value, "bar_size": r.bar_size.value,
                               "count": r.count, "spacing_mm": r.spacing_mm},
                        source=(r.source[0] if r.source
                                else element.sheets[0] if element.sheets else None),
                        produced_by=f"extraction.{ctx.playbook}",
                    )
        ctx.graph.find_spatial_duplicates()
        return {**ctx.graph.stats(), "_metrics": {"reused_knowledge_count": reused}}

    @runner.handler("missing_conflict_check")
    def _missing(run: Run) -> dict[str, Any]:
        """Pure function over the graph: what is missing, what conflicts.

        Kept separate from clarification: finding a gap and wording a
        question about it are different jobs, and only the first must be
        deterministic.
        """
        ctx.questions = _questions_for(ctx)
        conflicts = [c for e in ctx.graph.elements.values() for c in e.conflicts]
        return {"open_questions": len(ctx.questions), "conflicts": len(conflicts),
                "_metrics": {"exceptions_raised": len(ctx.questions) + len(conflicts)}}

    @runner.handler("rulebook")
    def _rulebook(run: Run) -> dict[str, Any]:
        for key, element in ctx.graph.elements.items():
            for r in element.reinforcement:
                ctx.chain.append(
                    claim_type=ClaimType.RULE_APPLICATION,
                    subject=claim_subject(key, r.claim_id),
                    value=ctx.rulebook.version,
                    rulebook_ver=ctx.rulebook.version,
                    rulebook_approved=ctx.rulebook.is_approved(),
                )
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
                subject=claim_subject(item.element_key or "", item.claim_id),
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
    """Same registration as :func:`build_runner`, extraction swapped for the
    synthetic Atlantic drawing so the demo needs no PDF corpus."""
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
    """Record a human answer as approved project knowledge.

    The approval is written against the scope *and* against every claim that
    scope currently matches. Without the second write the release decision
    for a claim never sees the approval that unblocked it, because the two
    are keyed differently.
    """
    fact = ctx.knowledge.approve(field_name, value, scope, approver, rationale)
    for element in ctx.graph.elements.values():
        for r in element.reinforcement:
            if not scope.matches(project_id=ctx.project_id,
                                 element_type=element.element_type.value,
                                 mark=element.mark, role=r.role.value):
                continue
            ctx.chain.append(
                claim_type=ClaimType.APPROVAL,
                subject=claim_subject(element.identity.key(), r.claim_id),
                value={"field": field_name, "value": value,
                       "version": fact.version},
                approver=approver,
                rationale=rationale,
            )
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
    run.route = ctx.route
    ctx.iteration += 1
    run.iteration = ctx.iteration
    executed = runner.execute(run)
    # A rerun that still has questions is waiting on a person again; the
    # clock for the next answer starts here rather than at the API call.
    ctx.start_waiting()
    return executed
