#!/usr/bin/env python3
"""End-to-end check of the demo output pack, on a running instance."""
import json
import sys
import time
import urllib.request

#: Every write now records who acted (spec v2 s2). Operator scripts run as
#: the admin demo user; the role rules are tested in test_roles.py.
ACTOR = "priya.raman@demo-client.com"

B = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8821").rstrip("/")
FAIL = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        FAIL.append(label)


def req(path, body=None, raw=False):
    d = json.dumps(body).encode() if body is not None else None
    headers = {"x-trustsight-user": ACTOR}
    if d:
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(B + path, data=d, headers=headers)
    with urllib.request.urlopen(r, timeout=120) as resp:
        payload = resp.read()
        return (payload, resp) if raw else json.loads(payload)


print(f"\nTrustSight demo pack check -> {B}\n")

print("readiness")
rd = req("/ready")
check("/ready responds", "ready" in rd)
check("no blocking issues", rd["ready"] is True, "; ".join(rd["blocking"]))
check("execution mode is local", rd["checks"]["execution_mode"] == "local_in_process")
check("no incomplete cloud handler reachable",
      rd["checks"]["incomplete_cloud_handlers_reachable"] is False)
check("three.js vendored, no CDN", rd["checks"]["viewer_three_js_vendored"] is True)
check("both playbooks registered",
      set(rd["checks"]["playbooks"]) == {"pile_v1", "footing_v1"})

for label, pid, answers, target in (
    ("Atlantic (pile_v1)", "atlantic-demo",
     [("legs", {"A": 510, "B": 11955}, "pile", "P1"),
      ("run_length_mm", 12250, "pile", "P1")], 5991.4),
    ("Kingston (footing_v1)", "Project 1 - Kingston Pipe Foundations",
     [("legs", {"B": 400, "C": 1120, "D": 400}, "footing", "CS-01"),
      ("bend_type", "17", "footing", "CS-01"),
      ("shape_code", "20A01", "footing", "CS-01")], 325.6),
):
    print(f"\n{label}")
    run = req("/runs", {"project_id": pid})
    rid = run["run_id"]
    for _ in range(60):
        w = req(f"/runs/{rid}/workspace")
        if w["elements"]:
            break
        time.sleep(0.4)
    check("run reads the drawing set", bool(w["elements"]),
          f"{len(w['elements'])} element(s)")

    for field, value, etype, mark in answers:
        role = next((q["role"] for q in w["questions"]), None) or "each_way"
        req(f"/runs/{rid}/clarifications", {
            "field_name": field, "value": value, "element_type": etype,
            "mark": mark, "role": role, "approver": "pack check",
            "rationale": "verification"})
        w = req(f"/runs/{rid}/workspace")

    if w["run"]["state"] == "suspended" and w["run"].get("pending_token"):
        req(f"/runs/{rid}/approve", {
            "token": w["run"]["pending_token"], "approver": "engineer",
            "answer": {"subject": "rulebook_approval", "decision": "approved"},
            "rationale": "assumption sheet reviewed"})
        w = req(f"/runs/{rid}/workspace")

    check(f"released mass is {target} kg",
          abs(w["released_mass_kg"] - target) < 0.05, f"{w['released_mass_kg']} kg")

    # viewer works with no query string
    body, resp = req(f"/runs/{rid}/viewer", raw=True)
    html = body.decode("utf-8", "replace")
    check("viewer loads from the route alone", resp.status == 200)
    check("viewer has the run id injected", f'"{rid}"' in html)
    check("viewer does not reference a CDN", "cdnjs" not in html and "unpkg" not in html)

    print("  exports")
    for name, magic in (("bbs.csv", b""), ("bbs.xlsx", b"PK"), ("bbs.pdf", b"%PDF"),
                        ("exceptions.csv", b""), ("exceptions.pdf", b"%PDF"),
                        ("evidence.json", b"{")):
        body, resp = req(f"/runs/{rid}/exports/{name}", raw=True)
        ok = resp.status == 200 and len(body) > 100 and body.startswith(magic)
        check(f"    {name}", ok, f"{len(body):,} bytes")

    csv_text = req(f"/runs/{rid}/exports/bbs.csv", raw=True)[0].decode()
    for col in ("Item", "No.", "Size", "Length (mm)", "Mark", "Type", "Weight (kg)"):
        check(f"    csv column {col!r}", col in csv_text)
    check("    csv states the data basis", "Data basis" in csv_text)
    check("    csv carries the leg columns", ",A,B,C,D,E,F,G,H,J,K,O,R," in csv_text)

print()
if FAIL:
    print(f"{len(FAIL)} CHECK(S) FAILED: " + "; ".join(FAIL))
    sys.exit(1)
print("ALL CHECKS PASSED")
