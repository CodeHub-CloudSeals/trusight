#!/usr/bin/env python3
"""Post-deploy smoke test for the TrustSight demo.

Runs the exact path a client walks in the demo and asserts the numbers, so a
broken deploy is caught here rather than on the call.

    python3 smoke.py https://xxxx.eu-west-2.awsapprunner.com
"""
import json
import sys
import urllib.request
import urllib.error

BASE = (sys.argv[1] if len(sys.argv) > 1 else "").rstrip("/")
if not BASE:
    sys.exit("usage: python3 smoke.py https://<your-app-runner-url>")

FAILURES = []
def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(label)

def req(path, body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(r, timeout=60) as resp:
        payload = resp.read()
        return payload if raw else json.loads(payload)

print(f"\nTrustSight smoke test -> {BASE}\n")

# 1 ── the service is up and in the right mode ───────────────────────────────
print("service")
try:
    h = req("/health")
    check("/health responds", h.get("status") == "ok", json.dumps(h)[:80])
    # 'cloud' means STATE_MACHINE_ARN leaked into the env, which routes runs to
    # Step Functions and silently breaks the seeded demo.
    check("mode is local (seeded demo path)", h.get("mode") == "local",
          f"mode={h.get('mode')}")
except Exception as e:
    check("/health responds", False, str(e))
    sys.exit("\nservice is not reachable — stopping\n")

# 2 ── the redesigned front end is actually the one deployed ─────────────────
print("\nfront end")
html = req("/", raw=True).decode("utf-8", "replace")
css = req("/assets/workspace.css", raw=True).decode("utf-8", "replace")
js = req("/assets/workspace.js", raw=True).decode("utf-8", "replace")
check("page serves", "TrustSight" in html)
check("theme toggle present", 'id="theme-toggle"' in html)
check("dark theme tokens in css", '[data-theme="dark"]' in css)
check("evidence drawer in js", "function openTrace" in js)
check("hash-chain diagram in js", "evidenceChainDiagram" in js)
check("pipeline diagram in js", "pipelineDiagram" in js)
check("no stock photos requested", ".png" not in html and "-context.png" not in js)

# 3 ── the demo flow, end to end, with the numbers asserted ──────────────────
print("\ndemo flow")
run = req("/runs", {"project_id": "atlantic-demo", "scenario": "clarification"})
rid = run["run_id"]
w = req(f"/runs/{rid}/workspace")
check("run starts", bool(rid), rid[:8])
check("starts with 2 open questions", len(w["questions"]) == 2,
      f"{len(w['questions'])} questions")
check("nothing released before approval", w["released_mass_kg"] == 0,
      f"{w['released_mass_kg']} kg")

answers = {"legs": {"A": 510, "B": 11955}, "run_length_mm": 12250}
for q in list(w["questions"]):
    req(f"/runs/{rid}/clarifications", {
        "field_name": q["field"], "value": answers[q["field"]],
        "element_type": "pile", "mark": "P1", "role": q["role"],
        "approver": "smoke test", "rationale": "post-deploy verification"})
    w = req(f"/runs/{rid}/workspace")

items = w["schedule"]["items"]
check("all questions resolved", len(w["questions"]) == 0)
check("2 schedule lines", len(items) == 2, f"{len(items)} lines")
check("released mass is 5991.4 kg", abs(w["released_mass_kg"] - 5991.4) < 0.05,
      f"{w['released_mass_kg']} kg")
check("both lines released",
      all(i["release"] == "released" for i in items),
      ", ".join(f"{i['mark']}={i['release']}" for i in items))

b = w.get("benchmark")
if b:
    matched = sum(1 for r in b["rows"] if r["match"])
    check("matches the reference bar list", matched == len(b["rows"]),
          f"{matched}/{len(b['rows'])} lines")

# 4 ── the thing the demo is sold on ─────────────────────────────────────────
print("\nevidence")
check("chain verified", w["evidence"]["chain_valid"] is True)
check("evidence records written", len(w["evidence"]["records"]) > 10,
      f"{len(w['evidence']['records'])} records")
x = items[0]["explanation"]
check("derivation data present for the drawer",
      all(k in x for k in ("count_basis", "length_basis", "unit_mass", "rulebook")))

# 5 ── exports the client may ask for on the call ────────────────────────────
print("\nexports")
for name, path in (("BBS export", f"/runs/{rid}/export/bbs"),
                   ("evidence export", f"/runs/{rid}/export/evidence")):
    try:
        body = req(path, raw=True)
        check(name, len(body) > 50, f"{len(body)} bytes")
    except Exception as e:
        check(name, False, str(e))

# ── verdict ─────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"{len(FAILURES)} CHECK(S) FAILED: " + "; ".join(FAILURES))
    sys.exit(1)
print("ALL CHECKS PASSED — demo is safe to run against this URL")
