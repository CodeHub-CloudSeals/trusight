#!/usr/bin/env python3
"""Record a verified run so the demo has something to fall back to.

Spec section 15, "Fallback": pre-create one known-good run and export pack so
the session can continue read-only if live extraction is unavailable.

This drives a running server rather than importing the pipeline, for one
reason: what gets frozen must be exactly what the client would have seen.
A fixture built by calling internals can pass while the HTTP surface is
broken, which is the failure this file exists to survive.

    uvicorn trustsight.api.main:app --port 8000 &
    python scripts/freeze_run.py --project "Project 5 - Atlanic Cages"

The run is taken through its clarifications, verified against the project's
reference bar list, and only then written. A run that does not reconcile is
refused: a fallback that is wrong is worse than no fallback, because it will
be trusted.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STORE = ROOT / "data" / "fallback"

#: The answers an estimator gives in the walkthrough. Recorded here with the
#: same rationale text the client will hear, so the frozen evidence chain
#: reads the way the live one would.
#: The same set the pile demonstrator uses, so the recorded run reconciles
#: with the reference exactly as the live one does. Bend types are included
#: even though the run does not always ask for them: a cutting length is a
#: function of the shape, and leaving the shape to a default is the habit
#: this product exists to break.
ANSWERS = [
    {"field_name": "legs", "value": {"A": 510, "B": 11955},
     "element_type": "pile", "role": "longitudinal",
     "rationale": "Cutting-length basis for the longitudinal bars: a 510 mm "
                  "projection into the cap above an 11,955 mm body."},
    {"field_name": "bend_type", "value": "2",
     "element_type": "pile", "role": "longitudinal",
     "rationale": "Single end hook, RebarCAD shape type 2."},
    {"field_name": "run_length_mm", "value": 12250,
     "element_type": "pile", "role": "spiral",
     "rationale": "Reinforced run length for the spiral, measured from the "
                  "section: 12,250 mm, pitch taken first turn to last."},
    {"field_name": "legs", "value": {"A": 140, "B": 140, "C": 2545,
                                     "G": 300, "O": 810},
     "element_type": "pile", "role": "spiral",
     "rationale": "Standard spiral turn geometry, shape 15A01. O is a "
                  "geometry parameter and is excluded from the leg sum."},
    {"field_name": "bend_type", "value": "T3",
     "element_type": "pile", "role": "spiral",
     "rationale": "Spiral shape family T3."},
]


def call(base: str, path: str, body: dict | None = None, raw: bool = False):
    url = f"{base}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=120) as r:
        payload = r.read()
    return payload if raw else json.loads(payload)


def settle(base: str, run_id: str, tries: int = 60) -> dict:
    """Wait for the background run to stop moving."""
    for _ in range(tries):
        run = call(base, f"/runs/{run_id}")
        if run["state"] not in ("running", "pending"):
            return run
        time.sleep(0.4)
    raise SystemExit(f"run {run_id} never settled")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://127.0.0.1:8000")
    p.add_argument("--project", default="atlantic-demo")
    p.add_argument("--scenario", default="clarification")
    p.add_argument("--baseline-minutes", type=int, default=45,
                   help="the estimator's stated manual time for this scope")
    p.add_argument("--name", default=None,
                   help="folder name under data/fallback (default: slugged project)")
    p.add_argument("--store", default=str(DEFAULT_STORE))
    p.add_argument("--approver", default="Demo estimator (recorded session)")
    p.add_argument("--allow-unverified", action="store_true",
                   help="write the run even if it does not match the reference")
    args = p.parse_args()

    try:
        call(args.base, "/health")
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit(
            f"no server at {args.base} — start uvicorn first ({exc})") from exc

    print(f"starting {args.project} ({args.scenario})")
    started = call(args.base, "/runs", {
        "project_id": args.project, "scenario": args.scenario,
        "manual_baseline_minutes": args.baseline_minutes,
        "requested_outputs": ["bbs_pdf", "bbs_xlsx", "evidence_pdf",
                              "spatial_view"],
    })
    run_id = started["run_id"]
    settle(args.base, run_id)

    data = call(args.base, f"/runs/{run_id}/workspace")
    mark = next((e["mark"] for e in data.get("elements") or []), None)
    for a in ANSWERS:
        print(f"  answering {a['field_name']} ({a['role']})")
        call(args.base, f"/runs/{run_id}/clarifications",
             {**a, "mark": mark, "approver": args.approver})

    data = call(args.base, f"/runs/{run_id}/workspace")
    if data.get("questions"):
        print("  still open: " +
              ", ".join(q["field"] for q in data["questions"]))
    if data["run"].get("pending_step") == 19:
        print("  signing off the rulebook")
        call(args.base, f"/runs/{run_id}/approve", {
            "token": data["run"].get("pending_token"),
            "approver": args.approver,
            "answer": {"subject": "rulebook_approval", "decision": "approved"},
            "rationale": "Cover, lap and stock-length rules reviewed against "
                         "the client assumption sheet for this recorded run.",
        })
        data = call(args.base, f"/runs/{run_id}/workspace")

    bench = data.get("benchmark")
    matched = sum(1 for r in bench["rows"] if r["match"]) if bench else 0
    total = len(bench["rows"]) if bench else 0
    verified = bool(bench) and matched == total and total > 0
    print(f"  released {data['released_mass_kg']} kg · "
          f"{matched}/{total} reference lines match")
    if not verified and not args.allow_unverified:
        raise SystemExit(
            "refusing to record a run that does not reconcile with its "
            "reference bar list. A fallback nobody can trust is worse than "
            "none, because it will be trusted. Re-run with "
            "--allow-unverified only if you intend to demonstrate a variance.")

    name = args.name or "".join(
        c if c.isalnum() or c == "-" else "-"
        for c in args.project.lower()).strip("-")[:48]
    out = Path(args.store) / name
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("page-*.png"):
        stale.unlink()

    (out / "workspace.json").write_text(json.dumps(data, indent=2))
    (out / "scene.json").write_text(
        json.dumps(call(args.base, f"/runs/{run_id}/scene"), indent=2))

    pages = call(args.base, f"/runs/{run_id}/document").get("pages", 0)
    if not data.get("seeded"):
        for n in range(1, int(pages) + 1):
            (out / f"page-{n}.png").write_bytes(
                call(args.base, f"/runs/{run_id}/drawing?page={n}", raw=True))

    # Export the client pack alongside it, so the download buttons on a
    # recorded run hand over files that were generated from this same run.
    packs = out / "exports"
    packs.mkdir(exist_ok=True)
    for f in ("bbs.csv", "bbs.pdf", "exceptions.csv", "exceptions.pdf",
              "bbs.xlsx"):
        try:
            (packs / f).write_bytes(
                call(args.base, f"/runs/{run_id}/exports/{f}", raw=True))
        except urllib.error.HTTPError as exc:
            print(f"  skipped {f}: {exc.code} {exc.reason}")

    manifest = {
        "label": f"{args.project} — recorded walkthrough",
        "project_id": args.project,
        "scenario": args.scenario,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_run_id": run_id,
        "verified_against_reference": verified,
        "reference_lines_matched": f"{matched}/{total}",
        "released_mass_kg": data["released_mass_kg"],
        "review_mass_kg": data["review_mass_kg"],
        "pages": int(pages) if not data.get("seeded") else 0,
        "note": "Recorded run. Read-only: clarifications cannot be answered "
                "against it. Serve it only when the live path is unavailable, "
                "and say so on the call.",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\nrecorded to {out}")
    print(f"serve as run id: recorded-{name}")


if __name__ == "__main__":
    sys.exit(main())
