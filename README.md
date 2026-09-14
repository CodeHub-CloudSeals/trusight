# TrustSight

Governed AI for rebar estimation from engineering drawings.

AI interprets the drawing. Engineering rules define how reinforcement is
treated. Deterministic software performs the arithmetic. A person approves
the exceptions. Every released number traces back to its source.

This repository implements the reference architecture in the TrustSight
Engineering Specification. It runs today against the five supplied reference
projects.

---

## What runs today

```
$ python scripts/demo_pipeline.py "/path/to/AGENT 1ST SET"

PASS 1 — what the drawing alone supports
  exceptions: longitudinal has no leg dimensions; spiral run length unstated
  clarification queue: 3 precise questions
  nothing released

ESTIMATOR ANSWERS THE CLARIFICATIONS
  approved legs, bend types and spiral run length, each scoped and attributed

PASS 2 — after approved project knowledge
    72 x 30M @ 12465mm   4931.7 kg   released
   216 x 15M @  3125mm   1059.8 kg   released

AGAINST GROUND TRUTH
  bars      generated    288   reference    288
  mass kg   generated  5,991.4   reference  5,991.4
```

The pipeline reads the actual Atlantic Cages PDF — six piles from the
location table, 1000mm diameter, 11,150mm length, `12-30M`, `15M@350` — then
stops where the drawing is silent, asks, and releases only what the evidence
supports. The reference bar list is never shown to the pipeline; it is used
only for comparison afterwards.

## Verified against the reference corpus

```
$ python scripts/run_eval.py "/path/to/AGENT 1ST SET"

project                            items   bars    mass kg    shape
Project 1 - Kingston Pipe Found        1     72      325.6      1/1
Project 2 - Bertas Phase 2 wall        7    550      942.5      7/7
Project 3 - Pier 23 & 25 gate sl      12   1500   11,982.1    12/12
Project 4 - Ireland Park               7    248    1,006.9      7/7
Project 5 - Atlantic Cages             2    288    5,991.4      2/2
TOTAL                                 29   2658   20,248.4    29/29
```

All 29 reference bar-list items reconcile against the bend-type catalogue.
The harness exits non-zero on any failure, so it gates CI.

---

## Why the architecture is shaped this way

Six invariants hold at all times. A change to any of them is a change to the
product, not an implementation detail.

| # | Invariant | Enforced by |
|---|---|---|
| I1 | A model never emits a quantity, length or mass | `engine/calculator.py` contains no LLM call |
| I2 | An unresolved fact never receives a default | `Reinforcement.unknown_fields`, `RuleNotFound` |
| I3 | Every released figure carries an evidence chain | `EvidenceChain.is_releasable` |
| I4 | 3D is generated from validated data, never an input | `engine/spatial.py` reads only the graph |
| I5 | Playbook, rulebook, knowledge and graph stay separate | four distinct modules and stores |
| I6 | Every step is idempotent and replayable | `WorkflowRunner`, keyed by input hash |
| I7 | Calculated is not the same as releasable | `EvidenceChain.release_status` |
| I8 | A run never reports success having skipped a step | `WorkflowRunner(strict=True)` |

Of the 23 pipeline steps, **14 are ordinary deterministic software**, four
invoke a language model generatively, three are mixed, and two suspend for a
human. That ratio is the strongest available argument that the output is
reproducible, and it should be stated to the client rather than hidden.

```
$ python -c "from trustsight.workflow.steps import summary; print(summary())"
{'deterministic': 14, 'llm': 3, 'vision': 1, 'mixed': 3, 'human': 2}
```

---

## The three things most likely to be built wrong

### 1. The shape catalogue

Cutting length is the sum of the leg columns, out to out, with no bend
deduction — the bar lists state this directly ("ALL DIMENSIONS ARE OUT TO
OUT", "FOR TYPICAL BEND TYPES REFER TO ACI").

But **populated columns are not always legs**, and **the bar mark does not
determine which are**:

| Bend type | Legs | Excluded | Observed marks |
|---|---|---|---|
| *(none)* | `B` | — | straight bars |
| `2` | `A+B` | — | 10A01, 15A02, 15A31, 30A01 |
| `2` | `A+B+G` | — | 15A03, 15A04, 15A05 |
| `17` | `B+C+D` | — | 15A01, 15A17–19, 20A01 |
| `17` | `B+C` | — | 15A30 |
| `T3` | `A+B+C+G` | `O` | 15A01 |
| `B16A` | `A+B+C+D+E` | `H`, `K` | 15A20 |

A "sum everything populated" implementation reconciles 27 of 29 items and is
confidently wrong on the other two — off by 810 mm on the `T3` spiral and
645 mm on the `B16A`. Mark `15A01` appears as type `17` in one project and
type `T3` in another, so the mark cannot be the key.

The sum rule is a **verification** test, not a derivation rule: you cannot
use "whichever columns sum to the length" to generate a length you do not yet
have. Seed `shapes/catalogue.py` from the licensed ACI reference and assert
agreement with the observed table.

### 2. Confidence is a gate vector, not a score

Blending OCR character confidence, a vision model's token probability and a
boolean "rule found" produces a number that cannot be calibrated and cannot
be explained. Six independent gates instead:

```python
g1_fields_complete   g2_no_conflict   g3_rule_resolved
g4_source_quality    g5_pattern_known g6_within_bounds
```

Auto-proceed requires all gates plus a native-text or vector source. A
conflict blocks and is never auto-resolved. A missing field becomes the
question to ask. The policy reads as a sentence to an estimator: *"we did not
auto-approve this because the cover was not stated on any sheet."*

### 3. Partial completion is the normal outcome

Element-wide blockers stop an element; an item blocker stops only that
reinforcement item. One unresolved spiral must not suppress 72 perfectly
good longitudinal bars — that is the difference between an 80/20 result and
an all-or-nothing one.

`Element.element_blockers()` covers missing instance basis, identity
conflict and unresolved element count. `Element.reinforcement_blockers()`
covers missing cover, spacing, run length or shape on one item.

### 4. Calculated is not releasable

A calculation record is necessary but not sufficient. `release_status()`
also requires a valid chain, no unresolved gate, no correction superseding
the calculation, and either an approved rulebook or a human approval record
dated *after* the calculation. Conflating "we computed it" with "it may be
released" is how an unapproved number reaches a tender.

### 5. Explicit count must agree with spacing

Where a drawing gives both an explicit count and a spacing, the engine
checks them against each other and raises `QuantityConflict` on
disagreement. On the pile case, 11,955mm at 350mm with `floor_plus_one`
gives 35 per pile while the reference implies 36 — a real discrepancy that
must be closed with the estimator rather than hidden by preferring whichever
number is to hand.

### 6. Approval gates make this a long-running workflow

Steps 12 and 19 suspend pending human action, potentially for days. That
rules out a request-response agent loop. On AWS this binds to Step Functions'
`waitForTaskToken`: the machine parks, holds no compute, survives
deployments, and resumes on `SendTaskSuccess`.

There is no timeout-to-approve path. The 14-day timeout fails the branch; it
never approves by default.

---

## Layout

```
src/trustsight/
  models/core.py            canonical types; BarItem mirrors the RebarCAD schema
  shapes/catalogue.py       bend type -> leg columns (the table above)
  engine/rulebook.py        versioned reference data; raises rather than defaulting
  engine/calculator.py      deterministic engine — no LLM in this module
  engine/spatial.py         3D by instantiation, not reconstruction
  extraction/pdf.py         tiered extraction: native -> vector -> vision -> OCR
  extraction/barlist.py     RebarCAD parser (uses ruled table lines, not text x-positions)
  graph/knowledge_graph.py  element identity, merge, spatial de-duplication
  agents/base.py            Bedrock client; schema-validated output with repair retries
  agents/specialists.py     the five generative agents
  workflow/steps.py         the 23 steps, classified by execution type
  workflow/runner.py        durable run state with suspend/resume
  evidence/fabric.py        append-only hash-chained claims
  eval/harness.py           corpus regression — build this first
  extraction/drawings.py    real PDF -> Element facts (pile_v1 playbook)
  services/pipeline.py      registers handlers; the run that actually runs
  knowledge/project.py      approved, scoped, versioned human answers
  engine/controls.py        C-001..C-006 control pack with evidence
  api/viewer.py             three.js completeness view (not a BIM model)
  api/main.py               FastAPI surface for the five demo screens
handlers/                   Lambda entry points
infra/app.py                AWS CDK stack
scripts/run_eval.py         harness CLI (CI gate)
scripts/demo_pile.py        end-to-end pile demonstrator
```

---

## Running locally

```bash
pip install -r requirements-dev.txt
export PYTHONPATH=src

# real drawing -> clarification -> governed BBS
python scripts/demo_pipeline.py "/path/to/AGENT 1ST SET"

# the gate/release behaviours in isolation
python scripts/demo_pile.py

# corpus regression
python scripts/run_eval.py "/path/to/AGENT 1ST SET"

# tests (57)
TRUSTSIGHT_CORPUS="/path/to/AGENT 1ST SET" pytest -q

# API
TRUSTSIGHT_CORPUS="/path/to/AGENT 1ST SET" \
  uvicorn trustsight.api.main:app --reload
```

`scripts/demo_pile.py` runs the Atlantic Cages case four ways and is the
clearest expression of the product:

| Condition | Behaviour |
|---|---|
| Structured, cover stated | 288 bars, 5,991.4 kg, **auto-proceed** — matches the client's bar list exactly |
| Cover not stated anywhere | **Nothing released.** The spiral's cutting length is underivable; the 3D view renders one bar path instead of two rather than drawing plausible steel |
| Schedule says 6 piles, plan shows 8 | **Blocked.** The conflict is preserved, not resolved by precedence |
| Rulebook not signed off | Released but flagged for review on `g3_rule_resolved` |

---

## AWS deployment

```bash
cd infra
npm install -g aws-cdk
pip install -r ../requirements-dev.txt
cdk bootstrap
cdk deploy --context region=eu-west-2
```

| Service | Role |
|---|---|
| S3 | Drawings (versioned, immutable originals), rendered pages, exports |
| Step Functions | The 23-step workflow; task tokens for approval gates |
| Lambda | Deterministic and LLM steps — short, idempotent |
| Fargate | PDF-heavy extraction beyond Lambda's memory and time limits |
| Bedrock | Claude models for the five generative agents |
| DynamoDB | Knowledge graph, evidence chain, run state |
| API Gateway | FastAPI behind a Mangum adapter |

The evidence table is granted `PutItem`, `GetItem` and `Query` — never
`UpdateItem` or `DeleteItem`. IAM alone is not sufficient, because `PutItem`
can still overwrite an item with the same key, so `put_evidence` adds a
condition expression that rejects a second write to the same
`(run_id, record_id)`. Append-only is enforced twice.

Approval in cloud mode calls `SendTaskSuccess` with the persisted task
token. Rejection uses `SendTaskFailure` or returns the run to clarification;
a timeout leaves the item unresolved. There is no path that approves by
default.

### Before production

- Set `TRUSTSIGHT_MODEL` to a Bedrock model ID enabled in your account and
  region; model availability varies and should be confirmed rather than
  assumed.
- Replace `DEMO_RULEBOOK` with the client's approved assumption sheet. Its
  version string is deliberately `demo-0.1-UNAPPROVED` and `approved_by` is
  `None`, so every item fails `g3_rule_resolved` until an engineer signs off.
- Add VPC endpoints for S3, DynamoDB and Bedrock if drawings must not
  traverse the public internet.
- The `HttpLambdaIntegration` import in `infra/app.py` depends on your CDK
  version; check it against the installed `aws-cdk-lib` before first deploy.

---

## Open decisions

These block implementation. Items marked *client* need the estimator or the
responsible engineer.

| # | Decision | Owner |
|---|---|---|
| D1 | Stock length — is 9,000 mm the standing value? | client |
| D2 | Default cover, lap, grade where drawings are silent | client |
| D3 | Start/end convention for spacing-driven counts | client |
| D4 | Definition of "match" for rebar specification accuracy | joint |
| D5 | Waste and rounding rules | client |
| D6 | ACI / RebarCAD shape catalogue licensing and edition | CloudSeals |
| D7 | Spatial duplicate tolerance (currently 50 mm) | engineering |
| D8 | Element families in demo scope — pile and footing proposed | joint |
| D9 | Client support hours — blueprint says 6–10, call deck says ~4 | CloudSeals |
| D10 | Abutment support (Project 4 is a bridge abutment) | joint |

D3 is the one that will show up first in the variance report: a run of length
*L* at spacing *s* yielding `floor(L/s)`, `floor(L/s)+1` or a cover-adjusted
variant shifts every tie count by one bar per element.

---

## Out of scope for the demonstrator

- Training or fine-tuning a model. This is retrieval, constrained prompting,
  geometry and deterministic rules.
- Production BIM authoring, IFC export, clash detection.
- Automatic compliance certification — standards mapping returns status and
  evidence gaps only.
- Handwriting recognition. No supplied file requires it.
- All twenty-plus element types (see D8).
- Autonomous learning. Only human-approved corrections enter project
  knowledge.

---

*CloudSeals UK Limited — engineering draft, September 2026*
