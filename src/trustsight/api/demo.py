"""Read-only presentation payloads and bounded local PDF intake."""
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
from ..shapes.catalogue import resolve
from ..workflow.steps import STEPS


def register(app, api):
    @app.get("/runs/{run_id}/workspace")
    def workspace(run_id: str) -> dict[str, Any]:
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
                got = next((i for i in items if i["size"] == ref.size.value and i["mark"] == ref.mark), None)
                rows.append({"mark": ref.mark, "size": ref.size.value, "reference_quantity": ref.quantity,
                             "reference_length_mm": ref.cutting_length_mm,
                             "generated_quantity": got["quantity"] if got else None,
                             "generated_length_mm": got["cutting_length_mm"] if got else None,
                             "match": bool(got and got["quantity"] == ref.quantity and got["cutting_length_mm"] == ref.cutting_length_mm)})
            benchmark = {"label": label, "rows": rows, "mass_kg": round(reference.total_mass_kg, 1)}
        facts = [{"field": r.subject, "value": r.value, "source": r.source.model_dump(mode="json") if r.source else None}
                 for r in ctx.chain.records if r.claim_type.value == "extraction" and r.source and not isinstance(r.value, dict)]
        history = [r.model_dump(mode="json") for r in ctx.chain.records if r.claim_type.value == "approval"]
        executed = {r.name for r in run.results.values()}
        return {
            "run": api.get_run(run_id), "seeded": seeded, "scenario": ctx.demo_scenario,
            "document": ctx.document.name, "elements": elements, "facts": facts,
            "schedule": schedule, "questions": ctx.questions,
            "unresolved": ctx.unresolved, "controls": api.controls_view(run_id),
            "evidence": api.evidence(run_id), "history": history, "benchmark": benchmark,
            "released_mass_kg": round(sum(i["quantity"] * i["cutting_length_mm"] / 1000 * i["explanation"]["unit_mass"] for i in items if i["release"] == "released"), 1),
            "review_mass_kg": round(sum(i["quantity"] * i["cutting_length_mm"] / 1000 * i["explanation"]["unit_mass"] for i in items if i["release"] != "released"), 1),
            "rulebook": asdict(ctx.rulebook),
            "steps": [{"no": s.no, "name": s.name, "kind": s.kind.value,
                       "status": "executed" if s.name in executed else "not run",
                       "ok": next((r.ok for r in run.results.values() if r.name == s.name), None)} for s in STEPS],
            "live_model": False,
        }

    @app.get("/runs/{run_id}/drawing")
    def drawing(run_id: str, page: int = 1):
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
