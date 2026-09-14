"""FastAPI surface for the demonstrator.

POST /runs executes a real run rather than returning an empty shell. In
cloud mode it starts the Step Functions execution and the approval endpoint
calls SendTaskSuccess with the persisted task token; in local mode it drives
the WorkflowRunner directly. The same service functions back both, so local
and cloud behaviour cannot drift.
"""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..engine.rulebook import Rulebook
from ..engine.spatial import build_scene, self_check
from ..eval import harness
from ..evidence.fabric import EvidenceChain
from ..extraction.barlist import parse_bar_list
from ..extraction import playbooks
from ..extraction.pdf import extract
from ..graph.knowledge_graph import ProjectKnowledgeGraph
from ..knowledge.project import ProjectKnowledge, Scope
from ..services.pipeline import (
    SEED_PROJECT_ID,
    PipelineContext,
    answer,
    build_runner,
    build_seeded_runner,
    rerun,
)
from ..workflow.runner import Run, RunState
from ..workflow.steps import STEPS, summary
from .dashboard import DASHBOARD_HTML
from .viewer import VIEWER_HTML

app = FastAPI(title="TrustSight", version="0.2.0")
ASSETS = Path(__file__).parent / "static"
app.mount("/assets", StaticFiles(directory=ASSETS), name="assets")


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    """The five-screen demo product UI (spec section 16)."""
    return DASHBOARD_HTML

CORPUS = Path(os.getenv("TRUSTSIGHT_CORPUS", str(Path(__file__).parents[3] / "data" / "corpus")))
CLOUD_MODE = bool(os.getenv("STATE_MACHINE_ARN"))

_runs: dict[str, Run] = {}
_ctx: dict[str, PipelineContext] = {}


def _rulebook(project_id: str, scenario: str = "clarification") -> Rulebook:
    """Demo rulebook. Replace with the client's approved assumption sheet.

    Every seeded scenario ships pre-approved except "approval", which
    deliberately leaves it unsigned so the approval_gate step (19) has
    something real to suspend on. A real (non-seeded) project always starts
    unapproved — a demo default is not an engineer's sign-off.
    """
    seeded_approved = project_id == SEED_PROJECT_ID and scenario != "approval"
    slug = "atlantic" if project_id == SEED_PROJECT_ID else (
        project_id.lower().split(" - ")[-1].split()[0] or "project")
    return Rulebook(
        version=f"{slug}-1.0", project_id=project_id,
        # One entry per element family the playbooks can read. There is no
        # generic fallback: an unlisted condition raises rather than borrowing
        # another element's cover (invariant I2).
        cover_mm={"pile": 75, "footing": 40},
        stock_length_mm=9000, rounding_mm=5,
        spacing_convention="floor_plus_one",
        approved_by="demo-estimator (simulated)" if seeded_approved else None,
    )


class StartRun(BaseModel):
    project_id: str
    path_mode: str = "semi_structured"  # structured | semi_structured | unstructured
    scenario: Literal["clarification", "conflict", "structured", "approval"] = "clarification"


class ClarificationIn(BaseModel):
    field_name: str
    value: Any
    element_type: str = "pile"
    mark: str | None = None
    role: str | None = None
    approver: str
    rationale: str


class ApprovalIn(BaseModel):
    token: str | None = None
    approver: str
    answer: dict[str, Any]
    rationale: str


def _require(run_id: str) -> tuple[Run, PipelineContext]:
    if run_id not in _runs or run_id not in _ctx:
        raise HTTPException(404, "unknown run")
    return _runs[run_id], _ctx[run_id]


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "mode": "cloud" if CLOUD_MODE else "local",
            "steps": summary()}


@app.get("/pipeline")
def pipeline() -> list[dict[str, Any]]:
    """Screen 2: make the orchestration visible."""
    return [{"no": s.no, "name": s.name, "kind": s.kind.value,
             "suspends": s.suspends, "note": s.note} for s in STEPS]


@app.get("/projects")
def projects() -> list[dict[str, Any]]:
    """Screen 1: project command centre."""
    out: list[dict[str, Any]] = [{
        "project_id": SEED_PROJECT_ID,
        "inputs": ["seeded — no PDF corpus required"],
        "has_ground_truth": True,
        "tiers": ["seeded"],
        "needs_vision": False,
        "route": "semi_structured",
        "seeded": True,
        "playbook": "pile_v1",
        "readable": True,
    }]
    for project_dir in sorted(p for p in CORPUS.glob("*") if p.is_dir()):
        inputs = sorted(project_dir.glob("Input*.pdf"))
        tiers, needs_vision = [], False
        for f in inputs:
            doc = extract(f)
            tiers.append(doc.tier.value)
            needs_vision |= any(s.needs_vision for s in doc.sheets)
        # Coverage is stated per project rather than implied. A project with
        # no playbook is reported as such instead of appearing runnable and
        # then returning an empty schedule.
        try:
            playbook = playbooks.select(project_dir).name
        except playbooks.NoPlaybook:
            playbook = None
        out.append({
            "project_id": project_dir.name,
            "inputs": [f.name for f in inputs],
            "has_ground_truth": bool(list(project_dir.glob("Output*.pdf"))),
            "tiers": sorted(set(tiers)),
            "needs_vision": needs_vision,
            "route": "unstructured" if needs_vision else "semi_structured",
            "playbook": playbook,
            "readable": playbook is not None,
        })
    return out


@app.get("/projects/{project_id}/reference")
def reference(project_id: str) -> dict[str, Any]:
    refs = sorted((CORPUS / project_id).glob("Output*.pdf"))
    if not refs:
        raise HTTPException(404, f"no reference bar list for {project_id}")
    schedule = parse_bar_list(refs[0], project_id=project_id)
    return {"project_id": project_id,
            "items": [i.model_dump(mode="json") for i in schedule.items],
            "total_mass_kg": round(schedule.total_mass_kg, 1),
            "by_size": {k: round(v, 1) for k, v in schedule.by_size().items()}}


@app.post("/runs")
def start_run(body: StartRun, background: BackgroundTasks) -> dict[str, Any]:
    """Start a real run. Returns immediately; poll /runs/{id} for progress.

    ``project_id == SEED_PROJECT_ID`` runs the seeded Atlantic Cages walkthrough
    (spec s16/s20) so the five screens are explorable without a PDF corpus —
    same 23-step registration, same gates and evidence chain; only the
    extraction step reads a synthetic drawing instead of a real PDF.
    """
    seeded = body.project_id == SEED_PROJECT_ID
    if seeded:
        document = Path("Atlantic-Cages-DR-612-E-1319.pdf")
    else:
        project_dir = (CORPUS / body.project_id).resolve()
        if project_dir.parent != CORPUS.resolve():
            raise HTTPException(400, "invalid project")
        drawings = sorted(project_dir.glob("Input*.pdf"))
        if not drawings:
            raise HTTPException(404, f"no input drawings for {body.project_id}")
        # Hand the playbook the whole set: an element's instance count can
        # depend on how many documents in the set describe it, which reading
        # only the first sheet would silently undercount.
        document = project_dir

    ctx = PipelineContext(
        project_id=body.project_id,
        document=document,
        rulebook=_rulebook(body.project_id, body.scenario),
        graph=ProjectKnowledgeGraph(body.project_id),
        chain=EvidenceChain(run_id="pending"),
        knowledge=ProjectKnowledge(body.project_id),
        demo_scenario=body.scenario,
    )
    run = Run(project_id=body.project_id)
    run.versions = {"rulebook": ctx.rulebook.version,
                    "path_mode": body.path_mode,
                    "model": os.getenv("TRUSTSIGHT_MODEL", "unset")}
    ctx.chain.run_id = run.run_id
    _runs[run.run_id] = run
    _ctx[run.run_id] = ctx

    if CLOUD_MODE:
        import boto3
        sfn = boto3.client("stepfunctions")
        execution = sfn.start_execution(
            stateMachineArn=os.environ["STATE_MACHINE_ARN"],
            input=run.model_dump_json() if hasattr(run, "model_dump_json")
            else f'{{"run_id":"{run.run_id}","project_id":"{body.project_id}"}}',
        )
        run.context["execution_arn"] = execution["executionArn"]
    else:
        runner = build_seeded_runner(ctx) if seeded else build_runner(ctx)
        background.add_task(lambda: runner.execute(run))

    return {"run_id": run.run_id, "state": run.state.value,
            "versions": run.versions, "mode": "cloud" if CLOUD_MODE else "local"}


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    run, ctx = _require(run_id)
    return {"run_id": run.run_id, "project_id": run.project_id,
            "state": run.state.value, "completed_steps": run.completed_steps(),
            "pending_step": run.pending_step, "pending_token": run.pending_token,
            "versions": run.versions,
            "open_questions": len(ctx.questions), "roi": run.roi()}


@app.get("/runs/{run_id}/graph")
def graph(run_id: str) -> dict[str, Any]:
    _, ctx = _require(run_id)
    return {"stats": ctx.graph.stats(),
            "elements": {k: {"type": e.element_type.value, "instances": e.instances,
                             "sheets": [s.cite() for s in e.sheets],
                             "conflicts": e.conflicts}
                         for k, e in ctx.graph.elements.items()},
            "completeness": ctx.graph.completeness_report()}


@app.get("/runs/{run_id}/exceptions")
def exceptions(run_id: str) -> dict[str, Any]:
    """Screen 4: clarification and approval."""
    run, ctx = _require(run_id)
    return {"run_id": run_id, "suspended": run.state is RunState.SUSPENDED,
            "token": run.pending_token, "questions": ctx.questions,
            "unresolved": ctx.unresolved,
            "schedule_exceptions": ctx.schedule.exceptions if ctx.schedule else []}


@app.post("/runs/{run_id}/clarifications")
def clarify(run_id: str, body: ClarificationIn) -> dict[str, Any]:
    """Record a human answer as approved project knowledge, then re-run."""
    _, ctx = _require(run_id)
    if not body.approver.strip() or not body.rationale.strip():
        raise HTTPException(422, "Approver and rationale are required")
    if body.field_name not in {"legs", "run_length_mm", "cover_mm", "bend_type", "shape_code"}:
        raise HTTPException(422, "Unsupported clarification field")
    if body.field_name == "legs":
        if (not isinstance(body.value, dict) or not body.value or
                any(k not in "ABCDEFGHJKOR" or len(k) != 1 or type(v) is not int or v <= 0
                    for k, v in body.value.items())):
            raise HTTPException(422, "Legs require positive integer dimensions in mm")
    elif body.field_name.endswith("_mm"):
        if type(body.value) is not int or not 0 < body.value <= 100000:
            raise HTTPException(422, "Enter a positive integer dimension up to 100,000 mm")
    answer(ctx, field_name=body.field_name, value=body.value,
           scope=Scope(project_id=ctx.project_id, element_type=body.element_type,
                       mark=body.mark, role=body.role),
           approver=body.approver, rationale=body.rationale)
    run = rerun(ctx, ctx.project_id)
    _runs[run_id] = run
    return {"run_id": run_id, "state": run.state.value,
            "open_questions": len(ctx.questions),
            "knowledge_facts": len(ctx.knowledge.facts)}


@app.post("/runs/{run_id}/approve")
def approve(run_id: str, body: ApprovalIn) -> dict[str, Any]:
    """Deliver a human decision. There is no default approval path.

    A suspend on the approval_gate step (19) means the rulebook itself was
    unsigned; the decision delivered here *is* that sign-off, so it also
    replaces ctx.rulebook with an approved copy before resuming. Any other
    suspend (e.g. a future policy-triggered review) is recorded as evidence
    but does not touch the rulebook.
    """
    run, ctx = _require(run_id)
    ctx.chain.append(claim_type="approval",
                     subject=body.answer.get("subject", "unspecified"),
                     value=body.answer, approver=body.approver,
                     rationale=body.rationale)
    if CLOUD_MODE:
        if not body.token:
            raise HTTPException(400, "task token required in cloud mode")
        import boto3
        boto3.client("stepfunctions").send_task_success(
            taskToken=body.token,
            output=f'{{"approved":true,"approver":"{body.approver}"}}',
        )
        return {"run_id": run_id, "state": "resumed"}

    was_rulebook_gate = run.pending_step == 19 and not ctx.rulebook.is_approved()
    try:
        runner = build_seeded_runner(ctx) if ctx.project_id == SEED_PROJECT_ID else build_runner(ctx)
        run = runner.resume(run, body.token or "", body.answer)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    if was_rulebook_gate:
        ctx.rulebook = replace(ctx.rulebook, approved_by=body.approver)
        # Release is decided per calculation *subject* (element/claim key),
        # so the sign-off has to land on the same subjects the calculation
        # used, not a generic "rulebook" label, or release_status never sees it.
        keys = {i.element_key for i in (ctx.schedule.items if ctx.schedule else []) if i.element_key}
        for key in keys:
            ctx.chain.append(claim_type="approval", subject=key,
                             value={"rulebook": ctx.rulebook.version, "decision": "approved"},
                             approver=body.approver, rationale=body.rationale)
    _runs[run_id] = run
    return {"run_id": run_id, "state": run.state.value}


@app.get("/runs/{run_id}/schedule")
def schedule(run_id: str) -> dict[str, Any]:
    """Screen 5: results with per-item release status."""
    _, ctx = _require(run_id)
    if ctx.schedule is None:
        return {"items": [], "exceptions": ctx.unresolved, "total_mass_kg": 0.0}
    items = []
    for i in ctx.schedule.items:
        decision = ctx.chain.release_status(
            i.element_key or "", rulebook_approved=ctx.rulebook.is_approved())
        items.append({**i.model_dump(mode="json"),
                      "total_mass_kg": round(i.total_mass_kg, 1),
                      "release": decision.state.value,
                      "release_reason": decision.reason})
    return {"items": items, "exceptions": ctx.schedule.exceptions,
            "total_mass_kg": round(ctx.schedule.total_mass_kg, 1),
            "by_size": {k: round(v, 1) for k, v in ctx.schedule.by_size().items()},
            "review_required": sum(1 for i in items if i["release"] != "released")}


@app.get("/runs/{run_id}/controls")
def controls_view(run_id: str) -> list[dict[str, Any]]:
    _, ctx = _require(run_id)
    return [c.model_dump(mode="json") for c in ctx.control_results]


@app.get("/runs/{run_id}/roi")
def roi(run_id: str) -> dict[str, Any]:
    run, _ = _require(run_id)
    return run.roi()


@app.get("/runs/{run_id}/scene")
def scene(run_id: str) -> dict[str, Any]:
    _, ctx = _require(run_id)
    built = build_scene(ctx.graph)
    payload = built.to_dict()
    if ctx.schedule:
        payload["self_check"] = self_check(built, ctx.schedule)
    return payload


@app.get("/runs/{run_id}/viewer", response_class=HTMLResponse)
def viewer(run_id: str) -> str:
    """Spatial Interpretation / Completeness View. Not a BIM model."""
    _require(run_id)
    return VIEWER_HTML


@app.get("/runs/{run_id}/evidence")
def evidence(run_id: str, subject: str | None = None) -> dict[str, Any]:
    _, ctx = _require(run_id)
    ok, msg = ctx.chain.verify()
    records = ctx.chain.trace(subject) if subject else ctx.chain.records
    body: dict[str, Any] = {"chain_valid": ok, "verification": msg,
                            "records": [r.model_dump(mode="json") for r in records]}
    if subject:
        decision = ctx.chain.release_status(
            subject, rulebook_approved=ctx.rulebook.is_approved())
        body["release"] = decision.model_dump(mode="json")
    return body


@app.get("/eval")
def evaluate() -> dict[str, Any]:
    results, totals = harness.run(CORPUS)
    return {"totals": totals,
            "projects": [{"project_id": r.project_id,
                          "reference_items": r.reference_items,
                          "reference_bars": r.reference_bars,
                          "reference_mass_kg": round(r.reference_mass_kg, 1),
                          "shape_verified": r.shape_verified,
                          "shape_failed": r.shape_failed} for r in results]}

# Keep presentation-specific payloads separate from the estimation engine.
import sys as _sys
from .demo import register as _register_demo
_register_demo(app, _sys.modules[__name__])
