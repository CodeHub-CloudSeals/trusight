"""Read-only presentation payloads, the client output pack and bounded local PDF intake."""
from __future__ import annotations

from dataclasses import asdict
import csv
import io
import json
import uuid
from typing import Any

import pymupdf
from fastapi import HTTPException, Request
from fastapi.responses import Response

from ..models.core import BarItem, BarSchedule, BarSize
from ..services.pipeline import SEED_PROJECT_ID
from ..services import roi as roi_service
from . import fallback
from ..shapes.catalogue import resolve
from ..shapes import catalogue as shapes
from .. import exports as exports_mod
from ..workflow import routes
from ..workflow.steps import STEPS


def register(app, api):
    @app.get("/runs/{run_id}/workspace")
    def workspace(run_id: str) -> dict[str, Any]:
        # A recorded run is served straight from disk. Every screen, export
        # and trace below reads this one payload, so the fallback needs no
        # parallel implementation that could drift from the live one.
        if fallback.is_recorded(run_id):
            try:
                return fallback.workspace(run_id)
            except KeyError as exc:
                raise HTTPException(404, "unknown recorded run") from exc
        run, ctx = api._require(run_id)
        schedule = api.schedule(run_id)
        items = schedule.get("items", [])
        for item in items:
            original = next(i for i in ctx.schedule.items if i.item_no == item["item_no"])
            element = ctx.graph.elements[original.element_key]
            reinforcement = next(r for r in element.reinforcement if r.claim_id == original.claim_id)
            shape = resolve(original.bend_type, original.legs)
            item["explanation"] = {
                "instances": element.instances,
                "per_instance": original.quantity // element.instances if element.instances else 0,
                "count_basis": "explicit count" if reinforcement.count is not None else ctx.rulebook.spacing_convention,
                "spacing_mm": reinforcement.spacing_mm,
                "run_length_mm": reinforcement.run_length_mm,
                "legs": original.legs,
                "length_basis": " + ".join(f"{k} ({original.legs[k]})" for k in shape.leg_columns),
                "unit_mass": original.unit_mass,
                "rulebook": ctx.rulebook.version,
                "role": reinforcement.role.value,
            }
        elements = [e.model_dump(mode="json") for e in ctx.graph.elements.values()]
        seeded = ctx.project_id == SEED_PROJECT_ID
        benchmark = None
        if seeded:
            reference = BarSchedule(project_id=SEED_PROJECT_ID, items=[
                BarItem(item_no=1, quantity=72, size=BarSize.M30, cutting_length_mm=12465, mark="30A01"),
                BarItem(item_no=2, quantity=216, size=BarSize.M15, cutting_length_mm=3125, mark="15A01"),
            ])
            label = "Target from the repository walkthrough"
        else:
            # ``document`` may be the project folder or one sheet inside it
            root = ctx.document if ctx.document.is_dir() else ctx.document.parent
            refs = sorted(root.glob("Output*.pdf"))
            reference = api.parse_bar_list(refs[0], project_id=ctx.project_id) if refs else None
            label = "Project reference bar list"
        if reference:
            rows = []
            for ref in reference.items:
                # Match on physical identity — size and cutting length — and
                # only tighten to the bar mark when the pipeline actually
                # produced one.
                #
                # Matching on the mark alone was wrong: a mark is a
                # fabricator's label from the client's own schedule, and a
                # drawing that never states it leaves every generated line
                # with mark=None. The comparison then reported 0 of 2 lines
                # matching over quantities and lengths that were correct to
                # the millimetre, which is the most damaging kind of wrong
                # number in this product — one that understates itself in
                # front of a client.
                got = next(
                    (i for i in items
                     if i["size"] == ref.size.value
                     and i["cutting_length_mm"] == ref.cutting_length_mm
                     and (i["mark"] is None or ref.mark is None
                          or i["mark"] == ref.mark)),
                    None)
                rows.append({"mark": ref.mark, "size": ref.size.value, "reference_quantity": ref.quantity,
                             "reference_length_mm": ref.cutting_length_mm,
                             "generated_mark": got["mark"] if got else None,
                             "generated_quantity": got["quantity"] if got else None,
                             "generated_length_mm": got["cutting_length_mm"] if got else None,
                             "match": bool(got and got["quantity"] == ref.quantity and got["cutting_length_mm"] == ref.cutting_length_mm)})
            benchmark = {"label": label, "rows": rows, "mass_kg": round(reference.total_mass_kg, 1)}
        facts = [{"field": r.subject, "value": r.value, "source": r.source.model_dump(mode="json") if r.source else None}
                 for r in ctx.chain.records if r.claim_type.value == "extraction" and r.source and not isinstance(r.value, dict)]
        # One row per human decision, normalised so the screen never has to
        # guess at the shape of ``value``.
        #
        # Two kinds of approval reach the chain and they do not look alike: a
        # clarification answer carries {"field", "value"}, while a rulebook
        # sign-off carries {"rulebook", "decision"} and is written again
        # against every element it unblocked so the release check can see it.
        # Rendering ``value.field`` over the second kind threw, and it threw
        # on the one screen the demo lands on straight after signing the
        # rulebook. Normalising here means the timeline cannot break on a
        # record shape the UI did not anticipate.
        history = []
        for r in ctx.chain.records:
            if r.claim_type.value != "approval" or "#" in r.subject:
                continue
            v = r.value if isinstance(r.value, dict) else {}
            if "field" in v:
                entry = {"kind": "clarification",
                         "label": str(v["field"]).replace("_", " "),
                         "detail": json.dumps(v.get("value"))}
            elif "rulebook" in v:
                # the per-element copies exist for release_status, not for
                # display: one sign-off is one decision
                if r.subject != "rulebook_approval":
                    continue
                entry = {"kind": "rulebook", "label": "rulebook sign-off",
                         "detail": str(v.get("rulebook", ""))}
            else:
                entry = {"kind": "decision",
                         "label": str(r.subject).replace("_", " "),
                         "detail": json.dumps(v) if v else ""}
            history.append({**r.model_dump(mode="json"), **entry})
        executed = {r.name for r in run.results.values()}
        released_mass = round(sum(i["quantity"] * i["cutting_length_mm"] / 1000 * i["explanation"]["unit_mass"] for i in items if i["release"] == "released"), 1)
        review_mass = round(sum(i["quantity"] * i["cutting_length_mm"] / 1000 * i["explanation"]["unit_mass"] for i in items if i["release"] != "released"), 1)
        return {
            "run": api.get_run(run_id), "seeded": seeded, "scenario": ctx.demo_scenario,
            "playbook": ctx.playbook,
            "document": ctx.document.name, "elements": elements, "facts": facts,
            "schedule": schedule, "questions": ctx.questions,
            "unresolved": ctx.unresolved, "controls": api.controls_view(run_id),
            "evidence": api.evidence(run_id), "history": history, "benchmark": benchmark,
            "released_mass_kg": released_mass,
            "review_mass_kg": review_mass,
            # Measured value, computed from this run's own telemetry and this
            # project's own reference list (spec s12). Sharing one payload
            # with the schedule screen is what stops the ROI page quoting a
            # mass the schedule does not show.
            "roi": roi_service.report(run, ctx, benchmark=benchmark,
                                      generated_mass_kg=released_mass + review_mass),
            "recorded": False,
            "rulebook": asdict(ctx.rulebook),
            "route": ctx.route,
            # The shapes this build can actually apply. The clarification
            # form offers these rather than a free text box: a bend type
            # the catalogue does not hold is not an answer, and finding
            # that out after submitting wastes the moment on stage.
            "shape_catalogue": sorted(k for k in shapes.CATALOGUE if k),
            "route_detail": routes.describe(routes.get(ctx.path_mode)),
            # Four distinct facts, not one "not run" bucket: ran, does not
            # apply to this route, not built yet, or should have run and did
            # not. Only the last is a defect, and a client cannot tell them
            # apart if they share a label.
            "steps": [{"no": s.no, "name": s.name, "kind": s.kind.value,
                       "status": ("executed" if s.name in executed
                                  else run.skipped.get(s.no).value
                                  if run.skipped.get(s.no) else "not run"),
                       "excluded_because": routes.get(ctx.path_mode).excluded.get(s.name),
                       "ok": next((r.ok for r in run.results.values() if r.name == s.name), None)} for s in STEPS],
            "live_model": False,
        }

    # The ROI endpoint in main needs the same payload the screens read.
    api.workspace_payload = workspace

    @app.get("/runs/{run_id}/drawing")
    def drawing(run_id: str, page: int = 1):
        if fallback.is_recorded(run_id):
            try:
                return Response(fallback.page_png(run_id, page),
                                media_type="image/png")
            except KeyError as exc:
                raise HTTPException(404, "Page not recorded") from exc
        _, ctx = api._require(run_id)
        if ctx.project_id == SEED_PROJECT_ID:
            raise HTTPException(404, "Sample uses an illustrative schematic, not a source PDF")
        with pymupdf.open(ctx.primary_document) as doc:
            if not 1 <= page <= len(doc):
                raise HTTPException(404, "Page not found")
            p = doc[page - 1]
            scale = min(2, 1800 / max(p.rect.width, p.rect.height))
            return Response(p.get_pixmap(matrix=pymupdf.Matrix(scale, scale)).tobytes("png"), media_type="image/png")

    @app.get("/runs/{run_id}/document")
    def document(run_id: str):
        if fallback.is_recorded(run_id):
            try:
                return fallback.document(run_id)
            except KeyError as exc:
                raise HTTPException(404, "unknown recorded run") from exc
        _, ctx = api._require(run_id)
        if ctx.project_id == SEED_PROJECT_ID:
            return {"pages": 1, "sample": True}
        with pymupdf.open(ctx.primary_document) as doc:
            return {"pages": len(doc), "sample": False}

    @app.post("/projects/upload")
    async def upload(request: Request):
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
                    raise ValueError("Use an unencrypted PDF with 1–100 pages")
        except Exception as exc:
            raise HTTPException(422, "Use a readable, unencrypted PDF with 1–100 pages") from exc
        project_id = "drawing-" + uuid.uuid4().hex[:10]
        target = api.CORPUS / project_id
        target.mkdir(parents=True, exist_ok=False)
        (target / "Input-drawing.pdf").write_bytes(payload)
        return {"project_id": project_id}

    @app.get("/runs/{run_id}/exports/{name}")
    def exports(run_id: str, name: str):
        """Client output pack (spec s9). One source for every number: these
        writers format what the engine produced and recalculate nothing."""
        data = workspace(run_id)
        stem = (data["run"]["project_id"] or "trustsight")[:40].replace(" ", "-")
        table = {
            "bbs.csv": ("text/csv", exports_mod.schedule_csv, "csv"),
            "bbs.xlsx": ("application/vnd.openxmlformats-officedocument."
                         "spreadsheetml.sheet", exports_mod.schedule_xlsx, "xlsx"),
            "bbs.pdf": ("application/pdf", exports_mod.schedule_pdf, "pdf"),
            "exceptions.csv": ("text/csv", exports_mod.exceptions_csv, "csv"),
            "exceptions.pdf": ("application/pdf", exports_mod.exceptions_pdf, "pdf"),
        }
        if name == "evidence.json":
            body = json.dumps(data, indent=2)
            return Response(body, media_type="application/json", headers={
                "Content-Disposition":
                    f'attachment; filename="{stem}-evidence.json"'})
        if name not in table:
            raise HTTPException(404, f"Unknown export {name!r}")
        media, writer, ext = table[name]
        try:
            body = writer(data)
        except exports_mod.ExportUnavailable as exc:
            # 503, not 500: the run is fine, this one format is not available
            raise HTTPException(503, str(exc)) from exc
        kind = name.split(".")[0]
        return Response(body, media_type=media, headers={
            "Content-Disposition": f'attachment; filename="{stem}-{kind}.{ext}"'})

    @app.get("/runs/{run_id}/export/{kind}")
    def export(run_id: str, kind: str):
        data = workspace(run_id)
        if kind == "evidence":
            return Response(json.dumps(data, indent=2), media_type="application/json",
                            headers={"Content-Disposition": 'attachment; filename="trustsight-evidence.json"'})
        if kind != "bbs":
            raise HTTPException(404, "Unknown export")
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["Project", "Data basis", "Mark", "Size", "Quantity", "Length mm", "Mass kg", "Release", "Rulebook"])
        for item in data["schedule"].get("items", []):
            # Prevent spreadsheet formula interpretation of source-supplied strings.
            row = [data["run"]["project_id"], "SAMPLE - not for construction" if data["seeded"] else "PDF - estimator review required",
                   item["mark"], item["size"], item["quantity"], item["cutting_length_mm"], item["total_mass_kg"], item["release"], data["rulebook"]["version"]]
            writer.writerow(["'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v for v in row])
        return Response(out.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="trustsight-bbs.csv"'})
