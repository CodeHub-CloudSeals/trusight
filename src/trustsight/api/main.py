"""FastAPI surface for the demonstrator.

POST /runs executes a real run rather than returning an empty shell. In
cloud mode it starts the Step Functions execution and the approval endpoint
calls SendTaskSuccess with the persisted task token; in local mode it drives
the WorkflowRunner directly. The same service functions back both, so local
and cloud behaviour cannot drift.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import json
import os
import uuid
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
from ..evidence.fabric import EvidenceChain, claim_subject
from ..extraction.barlist import parse_bar_list
from .. import exports
from ..extraction import playbooks
from ..extraction.pdf import extract
from ..graph.knowledge_graph import ProjectKnowledgeGraph
from ..shapes import catalogue as shapes
from ..knowledge.project import ProjectKnowledge, Scope
from ..services.pipeline import (
    SEED_PROJECT_ID,
    PipelineContext,
    answer,
    build_runner,
    build_seeded_runner,
    rerun,
)
from ..services import roi as roi_service
from ..workflow import routes
from ..workflow.runner import Run, RunState
from ..workflow.steps import STEPS, summary
from .dashboard import DASHBOARD_HTML
from . import fallback
from . import projects as prj
from . import ask as ask_service
from .viewer import VIEWER_HTML

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Pay the corpus scan at boot, not when the presenter opens the picker.

    Tier detection opens every input PDF, which took five seconds across the
    reference corpus — five seconds of empty panel every time someone clicked
    "New run". Best effort in a background thread: a corpus that cannot be
    read must not stop the service from starting, because the seeded
    walkthrough does not need one and /ready reports the state honestly.
    """
    import threading

    def warm() -> None:
        try:
            projects()
        except Exception:  # noqa: BLE001 - /ready is the place that reports
            pass

    threading.Thread(target=warm, daemon=True).start()
    yield


app = FastAPI(title="TrustSight", version="0.2.0", lifespan=lifespan)
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

prj.STORE.seed_from_corpus(CORPUS, SEED_PROJECT_ID)


def _actor(request: Request) -> prj.DemoUser:
    """Who is acting. Spec v2 s2: every action records an actor.

    The session is the demo user the browser last selected, sent on each
    request. It is not proof of identity and this build never claims it is —
    see projects.py. What it does give is a real role check: the capability
    tests below refuse the action server-side, so hiding a button is not the
    only thing standing between a Viewer and a release.
    """
    email = request.headers.get("x-trustsight-user", "")
    user = prj.USERS.get(email)
    if user is None:
        raise HTTPException(401, "Select a demo user to continue")
    return user


def _needs(user: prj.DemoUser, capability: str) -> None:
    role = prj.ROLES[user.role]
    if not getattr(role, f"can_{capability}"):
        raise HTTPException(403, (
            f"{role.label} cannot {capability.replace('_', ' ')} on this "
            f"project. Sign in as a role that can."))


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
    #: The estimator's current time for this scope. Optional, and left
    #: optional on purpose: with no baseline the ROI view reports machine and
    #: review time and states that no saving is claimed, rather than
    #: measuring against a number nobody supplied (spec s12).
    manual_baseline_minutes: int | None = None
    #: Advisory (spec s14.1). Every output is generated on demand from the
    #: same run data, so this records what the client asked for rather than
    #: gating anything — an "available" list that silently differs from what
    #: /runs/{id}/exports serves would be worse than no list.
    requested_outputs: list[str] = []


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
    if fallback.is_recorded(run_id):
        # A recorded run has no live context to mutate. Saying so is better
        # than returning an empty one and letting a write appear to succeed.
        raise HTTPException(
            409, "this is a recorded run and is read-only; start a live run "
                 "to answer clarifications")
    if run_id not in _runs or run_id not in _ctx:
        raise HTTPException(404, "unknown run")
    return _runs[run_id], _ctx[run_id]


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "mode": "cloud" if CLOUD_MODE else "local",
            "steps": summary()}


@app.get("/ready")
def ready() -> dict[str, Any]:
    """Go/no-go check, meant to be read in the ten minutes before a demo.

    ``/health`` says the process is up. This says whether it can actually
    do the thing: which corpus it can see, which playbook reads each
    project, and whether any incomplete cloud handler is reachable. It
    reports what is true rather than a green light — a readiness endpoint
    that always says ready is worth nothing.
    """
    projects: list[dict[str, Any]] = [{
        "project_id": SEED_PROJECT_ID, "seeded": True,
        "playbook": "pile_v1", "readable": True,
        "has_reference": True,
    }]
    corpus_present = CORPUS.exists()
    if corpus_present:
        for d in sorted(p for p in CORPUS.glob("*") if p.is_dir()):
            try:
                name = playbooks.select(d).name
            except playbooks.NoPlaybook:
                name = None
            projects.append({
                "project_id": d.name, "seeded": False, "playbook": name,
                "readable": name is not None,
                "has_reference": bool(list(d.glob("Output*.pdf"))),
            })
    readable = [p for p in projects if p["readable"]]
    assets = Path(__file__).parent / "static"
    checks = {
        "execution_mode": "cloud" if CLOUD_MODE else "local_in_process",
        # Cloud mode routes runs to Step Functions, and several Lambda
        # handlers still raise NotImplementedError. Local mode is the only
        # complete path today, so a hosted demo must report it.
        "incomplete_cloud_handlers_reachable": bool(CLOUD_MODE),
        "corpus_path": str(CORPUS),
        "corpus_present": corpus_present,
        "projects_visible": len(projects),
        "projects_readable": len(readable),
        "playbooks": [p.name for p in playbooks.PLAYBOOKS],
        "routes": {r.name: {"available": r.available,
                            "steps": len(r.steps),
                            "reason": r.unavailable_reason or None}
                   for r in routes.ROUTES.values()},
        "viewer_three_js_vendored": (assets / "vendor" / "three.module.js").exists(),
        "static_assets_present": (assets / "workspace.js").exists(),
        "exports": ["bbs.csv", "bbs.xlsx", "bbs.pdf",
                    "exceptions.csv", "exceptions.pdf", "evidence.json"],
        "xlsx_writer_available": exports.xlsx_available(),
        "active_runs": len(_runs),
        # If the live path fails mid-session there has to be something to
        # switch to. Zero here is not a blocker — it is a stated risk.
        "recorded_runs": [r["run_id"] for r in fallback.available()],
        "fallback_path": str(fallback.STORE),
    }
    blocking = []
    if CLOUD_MODE:
        blocking.append(
            "cloud mode is on: runs would be routed to Step Functions and "
            "several handlers raise NotImplementedError")
    if not checks["static_assets_present"]:
        blocking.append("workspace assets missing from the image")
    if not checks["viewer_three_js_vendored"]:
        blocking.append(
            "three.js is not vendored: the 3D viewer would need a public CDN")
    if not checks["xlsx_writer_available"]:
        blocking.append(
            "openpyxl is missing: the XLSX export would be unavailable "
            "(run pip install -r requirements.txt)")
    if not corpus_present:
        blocking.append(
            f"no corpus at {CORPUS}: only the seeded walkthrough can run")

    warnings = []
    if not checks["recorded_runs"]:
        warnings.append(
            "no recorded fallback run: if live extraction fails during the "
            "session there is nothing to switch to "
            "(python scripts/freeze_run.py)")
    return {"ready": not blocking, "blocking": blocking, "warnings": warnings,
            "checks": checks, "projects": projects}


@app.get("/session/users")
def session_users() -> dict[str, Any]:
    """The demo users offered on the sign-in screen.

    There is no password here on purpose (projects.py explains why). The
    screen is a user picker that says so, not a credential form that lies.
    """
    return {
        "tenant_id": prj.DEMO_TENANT,
        "authentication": "demonstration",
        "note": ("Demonstration sign-in. No password is requested or checked "
                 "and no account is created. Role permissions below are "
                 "enforced by the API."),
        "roadmap": "AWS Cognito user pool, then enterprise SSO via OIDC/SAML",
        "users": [u.as_dict() for u in prj.USERS.values()],
        "roles": [{"key": r.key, "label": r.label, "summary": r.summary}
                  for r in prj.ROLES.values()],
    }


@app.get("/api/projects")
def list_projects(request: Request) -> list[dict[str, Any]]:
    user = _actor(request)
    out = []
    for p in prj.STORE.list(user.tenant_id):
        d = p.as_dict()
        if p.seeded:
            d["playbook"], d["readable"] = "pile_v1", True
        elif p.source_dir:
            try:
                d["playbook"] = playbooks.select(Path(p.source_dir)).name
            except playbooks.NoPlaybook:
                d["playbook"] = None
            d["readable"] = d["playbook"] is not None
            d["has_reference"] = bool(list(Path(p.source_dir).glob("Output*.pdf")))
        else:
            # A project someone created: readable once it holds a drawing a
            # playbook can read. Reported per project rather than assumed.
            d["playbook"] = None
            d["readable"] = bool(p.documents)
        out.append(d)
    return sorted(out, key=lambda d: (not d["seeded"], d["name"]))


class NewProject(BaseModel):
    name: str
    client: str = ""
    site: str = ""
    standard: str = "ACI / RebarCAD bend types"
    estimator: str = ""
    manual_baseline_minutes: int | None = None


@app.post("/api/projects")
def create_project(body: NewProject, request: Request) -> dict[str, Any]:
    user = _actor(request)
    _needs(user, "create_project")
    if not body.name.strip():
        raise HTTPException(422, "A project needs a name")
    if (body.manual_baseline_minutes is not None
            and not 0 < body.manual_baseline_minutes <= 10000):
        raise HTTPException(422, "Baseline must be whole minutes, 1-10000")
    project = prj.STORE.create(
        name=body.name.strip(), tenant_id=user.tenant_id,
        client=body.client.strip(), site=body.site.strip(),
        standard=body.standard.strip(), estimator=body.estimator.strip(),
        manual_baseline_minutes=body.manual_baseline_minutes,
        created_by=user.email)
    return project.as_dict()


@app.get("/api/projects/{project_id}")
def get_project(project_id: str, request: Request) -> dict[str, Any]:
    user = _actor(request)
    project = prj.STORE.get(project_id, user.tenant_id)
    if project is None:
        raise HTTPException(404, "No such project in this tenant")
    return project.as_dict()


@app.post("/api/projects/{project_id}/documents")
async def add_document(project_id: str, request: Request) -> dict[str, Any]:
    """Web upload channel (spec v2 s6). Originals are hashed and immutable."""
    import hashlib

    user = _actor(request)
    _needs(user, "upload")
    project = prj.STORE.get(project_id, user.tenant_id)
    if project is None:
        raise HTTPException(404, "No such project in this tenant")
    if project.seeded or project.source_dir:
        raise HTTPException(409, (
            "This project's drawings come from the mounted corpus and are "
            "read-only. Create a project to upload your own."))
    filename = request.headers.get("x-filename", "drawing.pdf")
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > 25 * 1024 * 1024:
            raise HTTPException(413, "Use a PDF under 25 MB")
    if not payload.startswith(b"%PDF"):
        raise HTTPException(422, "Please select a PDF drawing")
    try:
        with pymupdf.open(stream=bytes(payload), filetype="pdf") as doc:
            if doc.needs_pass or not 0 < len(doc) <= 100:
                raise ValueError
    except Exception as exc:
        raise HTTPException(422, "Use a readable, unencrypted PDF with 1-100 pages") from exc

    target = CORPUS / project.project_id
    target.mkdir(parents=True, exist_ok=True)
    safe = Path(filename).name.replace("/", "_")[:80] or "drawing.pdf"
    if not safe.lower().endswith(".pdf"):
        safe += ".pdf"
    stem = f"Input-{len(project.documents) + 1:02d}-{safe}"
    (target / stem).write_bytes(payload)
    project.source_dir = str(target)
    doc = prj.Document(
        document_id="doc-" + uuid.uuid4().hex[:10], filename=safe,
        channel="web_upload", bytes_=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        uploaded_by=user.email, uploaded_at=prj.now(),
        path=str(target / stem))
    prj.STORE.add_document(project, doc)
    return {"project_id": project.project_id, "document": doc.as_dict(),
            "document_count": len(project.documents)}


@app.get("/api/channels")
def channels() -> list[dict[str, Any]]:
    """Intake channels and their real status (spec v2 s6).

    Every card here states whether it works in this build. A connector drawn
    as available and then not demonstrated is the kind of detail a technical
    buyer remembers.
    """
    return [
        {"key": "web_upload", "name": "Web upload", "status": "live",
         "detail": "Drag and drop a PDF. Hashed on arrival and stored immutable."},
        {"key": "corpus", "name": "Mounted project folder", "status": "live",
         "detail": "Drawings mounted with the service, read-only."},
        {"key": "api", "name": "REST API", "status": "live",
         "detail": "POST /api/projects/{id}/documents with the PDF body."},
        {"key": "s3", "name": "S3 project prefix", "status": "roadmap",
         "detail": "Watch a tenant prefix, register on event. Not in this build."},
        {"key": "sftp", "name": "SFTP", "status": "roadmap",
         "detail": "AWS Transfer Family into the same prefix. Not in this build."},
        {"key": "sharepoint", "name": "SharePoint / OneDrive", "status": "roadmap",
         "detail": "Connector pulls into the governed originals store."},
        {"key": "teams", "name": "Teams / Slack", "status": "roadmap",
         "detail": "Status and approval links. Never the engineering repository."},
        {"key": "email", "name": "Email ingestion", "status": "roadmap",
         "detail": "Project address, attachment extraction."},
    ]


@app.get("/pipeline")
def pipeline() -> list[dict[str, Any]]:
    """Screen 2: make the orchestration visible."""
    return [{"no": s.no, "name": s.name, "kind": s.kind.value,
             "suspends": s.suspends, "note": s.note} for s in STEPS]


#: Tier detection opens and parses every input PDF, which took five seconds
#: across the reference corpus. The project picker calls this every time it
#: opens, so on stage the presenter clicked "New run" and watched an empty
#: panel. The drawings do not change while the server is up; keyed on path
#: and mtime, so a file replaced underneath us is still re-read.
_project_cache: dict[tuple[str, int], dict[str, Any]] = {}


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
        key = (str(project_dir),
               max((int(f.stat().st_mtime) for f in inputs), default=0))
        cached = _project_cache.get(key)
        if cached is not None:
            out.append(cached)
            continue
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
        entry = {
            "project_id": project_dir.name,
            "inputs": [f.name for f in inputs],
            "has_ground_truth": bool(list(project_dir.glob("Output*.pdf"))),
            "tiers": sorted(set(tiers)),
            "needs_vision": needs_vision,
            "route": "unstructured" if needs_vision else "semi_structured",
            "playbook": playbook,
            "readable": playbook is not None,
        }
        _project_cache[key] = entry
        out.append(entry)
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

    route = routes.get(body.path_mode)
    if not route.available:
        # Refusing is the honest answer. Starting a run that fails halfway
        # through would look like a bug rather than a stated boundary.
        raise HTTPException(400, {
            "error": f"route {route.name!r} is not available in this build",
            "reason": route.unavailable_reason,
            "available_routes": [r.name for r in routes.ROUTES.values()
                                 if r.available],
        })

    ctx = PipelineContext(
        project_id=body.project_id,
        document=document,
        path_mode=route.name,
        rulebook=_rulebook(body.project_id, body.scenario),
        graph=ProjectKnowledgeGraph(body.project_id),
        chain=EvidenceChain(run_id="pending"),
        knowledge=ProjectKnowledge(body.project_id),
        demo_scenario=body.scenario,
        manual_baseline_minutes=body.manual_baseline_minutes,
    )
    run = Run(project_id=body.project_id)
    run.versions = {"rulebook": ctx.rulebook.version,
                    "path_mode": route.name,
                    "model": os.getenv("TRUSTSIGHT_MODEL", "unset")}
    run.route = route.name
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

        def _execute() -> None:
            runner.execute(run)
            # If the run ended holding a question, review time starts now —
            # not when the browser happens to poll.
            ctx.start_waiting()

        background.add_task(_execute)

    return {"run_id": run.run_id, "state": run.state.value, "iteration": run.iteration,
            "route": route.name, "versions": run.versions,
            "requested_outputs": body.requested_outputs,
            "manual_baseline_minutes": body.manual_baseline_minutes,
            "execution_mode": "cloud" if CLOUD_MODE else "local-demo",
            "mode": "cloud" if CLOUD_MODE else "local"}


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    if fallback.is_recorded(run_id):
        try:
            return fallback.summary(run_id)
        except KeyError as exc:
            raise HTTPException(404, "unknown recorded run") from exc
    run, ctx = _require(run_id)
    return {"run_id": run.run_id, "project_id": run.project_id,
            "state": run.state.value, "iteration": run.iteration,
            "route": run.route,
            "completed_steps": run.completed_steps(),
            "pending_step": run.pending_step, "pending_token": run.pending_token,
            "versions": run.versions,
            "open_questions": len(ctx.questions),
            "claim_summary": roi_service.claim_summary(ctx),
            "recorded": False,
            "roi": run.roi()}


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
def clarify(run_id: str, body: ClarificationIn, request: Request) -> dict[str, Any]:
    """Record a human answer as approved project knowledge, then re-run."""
    _needs(_actor(request), "clarify")
    _, ctx = _require(run_id)
    # The review clock stops the moment the answer arrives, before any
    # recalculation, so machine time is never billed as human time.
    ctx.stop_waiting()
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
    elif body.field_name == "bend_type":
        # Now that the shape is something a person types during the demo, it
        # has to be checked here. An unchecked value reached the catalogue,
        # which raised AttributeError on a number and failed the whole run —
        # a typo on stage ending the walkthrough. Rejecting with the list of
        # shapes the catalogue actually holds is both safer and more useful
        # than a run that dies.
        known = sorted(k for k in shapes.CATALOGUE if k)
        if not isinstance(body.value, str) or body.value.strip() not in known:
            raise HTTPException(422, (
                f"Unknown bend type. This build's catalogue holds "
                f"{', '.join(known)}. Seed the catalogue from the ACI "
                f"reference to add more (spec D6)."))
        body.value = body.value.strip()
    answer(ctx, field_name=body.field_name, value=body.value,
           scope=Scope(project_id=ctx.project_id, element_type=body.element_type,
                       mark=body.mark, role=body.role),
           approver=body.approver, rationale=body.rationale)
    run = rerun(ctx, ctx.project_id)
    _runs[run_id] = run
    return {"run_id": run_id, "state": run.state.value,
            "iteration": run.iteration,
            "open_questions": len(ctx.questions),
            "claim_summary": roi_service.claim_summary(ctx),
            "knowledge_facts": len(ctx.knowledge.facts)}


@app.post("/runs/{run_id}/approve")
def approve(run_id: str, body: ApprovalIn, request: Request) -> dict[str, Any]:
    """Deliver a human decision. There is no default approval path.

    A suspend on the approval_gate step (19) means the rulebook itself was
    unsigned; the decision delivered here *is* that sign-off, so it also
    replaces ctx.rulebook with an approved copy before resuming. Any other
    suspend (e.g. a future policy-triggered review) is recorded as evidence
    but does not touch the rulebook.
    """
    # Releasing steel is the one action in this product that must be somebody's
    # named decision, so the role check is server-side and comes first.
    _needs(_actor(request), "approve")
    run, ctx = _require(run_id)
    ctx.stop_waiting()
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
        subjects: set[str] = set()
        for item in (ctx.schedule.items if ctx.schedule else []):
            if not item.element_key:
                continue
            subjects.add(item.element_key)
            subjects.add(claim_subject(item.element_key, item.claim_id))
        for key in subjects:
            ctx.chain.append(claim_type="approval", subject=key,
                             value={"rulebook": ctx.rulebook.version, "decision": "approved"},
                             approver=body.approver, rationale=body.rationale)
    _runs[run_id] = run
    ctx.start_waiting()
    return {"run_id": run_id, "state": run.state.value,
            "iteration": run.iteration,
            "claim_summary": roi_service.claim_summary(ctx)}


@app.get("/runs/{run_id}/schedule")
def schedule(run_id: str) -> dict[str, Any]:
    """Screen 5: results with per-item release status."""
    _, ctx = _require(run_id)
    if ctx.schedule is None:
        return {"items": [], "exceptions": ctx.unresolved, "total_mass_kg": 0.0}
    items = []
    for i in ctx.schedule.items:
        decision = ctx.chain.release_status(
            claim_subject(i.element_key or "", i.claim_id),
            rulebook_approved=ctx.rulebook.is_approved())
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
    """Measured value (spec s12).

    Built from the same workspace payload the screens read, so the ROI page
    and the schedule page can never quote different numbers for the same
    run.
    """
    if fallback.is_recorded(run_id):
        return fallback.workspace(run_id).get("roi", {})
    run, ctx = _require(run_id)
    # bound by demo.register at import; resolved here at call time
    payload = workspace_payload(run_id)  # noqa: F821
    return roi_service.report(
        run, ctx, benchmark=payload.get("benchmark"),
        generated_mass_kg=payload.get("released_mass_kg", 0.0)
        + payload.get("review_mass_kg", 0.0))


@app.post("/runs/{run_id}/baseline")
def set_baseline(run_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Record the estimator's manual baseline for this scope.

    Separate from run start because in practice the number arrives in
    conversation — "this one takes me about forty minutes" — after the run
    is already going.
    """
    _, ctx = _require(run_id)
    value = body.get("manual_baseline_minutes")
    if value is not None and (type(value) is not int or not 0 < value <= 10000):
        raise HTTPException(422, "Enter the baseline as whole minutes, 1-10000")
    ctx.manual_baseline_minutes = value
    return {"run_id": run_id, "manual_baseline_minutes": value}


class AskIn(BaseModel):
    question: str


@app.post("/runs/{run_id}/ask")
def ask(run_id: str, body: AskIn) -> dict[str, Any]:
    """Ask TrustSight (spec v2 s9). Grounded in this run, or silent.

    Read-only by construction: every intent queries, none of them writes, so
    the "unsafe actions require confirmation" rule in the acceptance criteria
    is satisfied by there being no unsafe action to reach from here.
    """
    if fallback.is_recorded(run_id):
        raise HTTPException(409, "Ask is not available on a recorded run")
    run, ctx = _require(run_id)
    return ask_service.answer(body.question, ctx, run, workspace_payload(run_id))  # noqa: F821


@app.get("/ask/examples")
def ask_examples() -> dict[str, Any]:
    """What the query bar actually understands, so nobody has to guess."""
    return {"examples": ask_service.EXAMPLES,
            "model_configured": False,
            "note": ("Answers are queries over this project's own data, not "
                     "generated text. An unmatched question is declined "
                     "rather than guessed.")}


@app.get("/fallback")
def recorded_runs() -> list[dict[str, Any]]:
    """Recorded runs this instance can serve if the live path is unavailable."""
    return fallback.available()


@app.get("/runs/{run_id}/scene")
def scene(run_id: str) -> dict[str, Any]:
    """Derived spatial view, carrying each element's release state.

    The geometry is built from validated graph data; the release state is
    attached here so the viewer cannot draw unresolved steel the same way it
    draws approved steel. A spatial view that looks finished while the
    schedule says otherwise is a picture that contradicts its own numbers.
    """
    if fallback.is_recorded(run_id):
        try:
            return fallback.scene(run_id)
        except KeyError as exc:
            raise HTTPException(404, "unknown recorded run") from exc
    _, ctx = _require(run_id)
    built = build_scene(ctx.graph)
    payload = built.to_dict()
    if ctx.schedule:
        payload["self_check"] = self_check(built, ctx.schedule)
        approved = ctx.rulebook.is_approved()
        by_element: dict[str, list[str]] = {}
        for item in ctx.schedule.items:
            decision = ctx.chain.release_status(
                claim_subject(item.element_key or "", item.claim_id),
                rulebook_approved=approved)
            by_element.setdefault(item.element_key or "", []).append(
                decision.state.value)
        blocked = {e.split(" [")[0].strip()
                   for e in (ctx.schedule.exceptions or [])}
        for node in payload.get("nodes", []):
            states = by_element.get(node["element_key"], [])
            node["claims"] = {
                "released": states.count("released"),
                "review": states.count("review"),
                "blocked": states.count("block") + (
                    1 if node["element_key"] in blocked else 0),
            }
            # the drawing style follows the weakest claim on the element
            node["release_state"] = (
                "blocked" if node["claims"]["blocked"] else
                "review" if node["claims"]["review"] else
                "released" if node["claims"]["released"] else "pending")
    return payload


@app.get("/runs/{run_id}/viewer", response_class=HTMLResponse)
def viewer(run_id: str) -> str:
    """Spatial Interpretation / Completeness View. Not a BIM model.

    The run id is injected from the route so the page works when opened
    directly, with no query string to remember or lose.
    """
    if not fallback.is_recorded(run_id):
        _require(run_id)
    return VIEWER_HTML.replace("__RUN_ID__", json.dumps(run_id))


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
