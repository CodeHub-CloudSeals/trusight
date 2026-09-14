# TrustSight — client demo runbook

Fifteen minutes, six screens, one business goal: *estimate the rebar for
these structural elements*. Rehearse it twice, start to finish, without
touching the code. If you need the code during a rehearsal, that is the
finding — fix it before the session, not during it.

> **Freeze the build one business day before the session.** The client
> meeting is not an integration test.

---

## 1. Ninety minutes before — go / no-go

Run these in order. Every one is a hard gate except where marked.

| # | Check | Command | Pass condition |
|---|-------|---------|----------------|
| 1 | Service up | `curl -s $URL/health` | `{"status":"ok","mode":"local"}` — **`mode` must be `local`** |
| 2 | Readiness | `curl -s $URL/ready` | `"ready": true`, `"blocking": []` |
| 3 | Corpus visible | same payload | `corpus_present: true`, `projects_readable` ≥ 2 |
| 4 | Routes | same payload | `structured` and `semi_structured` available; `unstructured` **false** — that is correct, see §5 |
| 5 | Exports | same payload | `xlsx_writer_available: true` |
| 6 | Viewer assets | same payload | `viewer_three_js_vendored: true` |
| 7 | Recorded fallback | same payload | `recorded_runs` is not empty |
| 8 | Tests | `pytest -q` | all pass |
| 9 | Reference match | `python scripts/demo_pipeline.py "$CORPUS"` | Atlantic **5,991.4 kg**, 2/2 lines |
| 10 | Second family | `python scripts/demo_footing.py "$CORPUS"` | Kingston **325.6 kg**, exact |
| 11 | Browser | open `$URL` | workspace loads, no console errors |

`mode: cloud` is a stop. It routes runs to Step Functions, where several
Lambda handlers still raise `NotImplementedError`. Unset `STATE_MACHINE_ARN`
and restart.

Also do, by hand:

- Open the **Value & accuracy** screen once and confirm it is not claiming a
  saving you cannot defend. With no baseline entered it should say so.
- Open **New run** once. The project list should appear immediately; if it
  hangs, the corpus scan has not warmed and the first open will be slow.
- Load the recorded run once (New run → Recorded runs) and confirm the amber
  *Recorded run* banner appears. Then start a live run again.
- Close every other tab. Turn off notifications. Set the display to light
  theme unless the room is dark.

---

## 2. The fifteen minutes

Times are cumulative. The numbers in brackets are what should be on screen.

### 0:00 — Frame it (1 min)
No slides. Open the workspace on **Overview**.

> "The goal is the same as your estimator's: take these drawings and produce
> a bar bending schedule you can issue. The difference is that every number
> here can be traced back to the line on the drawing it came from, the rule
> that was applied, and the person who approved anything the drawing didn't
> say."

Point at **Open questions** and **Released mass**. Say that the second number
is deliberately held back until the first is zero.

### 1:00 — The happy path (2 min)
New run → **03 / Everything known: instant release**.

Everything the drawing states is enough; nothing is asked; the schedule
releases. This is the structured route.

> "When the drawing is complete, nothing is asked and nothing is assumed."

### 3:00 — The real case (3 min)
New run → the corpus project **Project 5 - Atlanic Cages**. Enter **45**
as the estimator's manual baseline when prompted.

Go to **Drawing intelligence**. The source PDF is on the left. Click two
facts on the right and read the *why this matters* text aloud.

Open **Review & approve**. The run asks five questions, one at a time, each
naming the exact missing fact:

| # | Field | Role | Answer to give |
|---|-------|------|----------------|
| 1 | `legs` | longitudinal | `{"A": 510, "B": 11955}` |
| 2 | `bend_type` | longitudinal | `2` |
| 3 | `run_length_mm` | spiral | `12250` |
| 4 | `legs` | spiral | `{"A":140,"B":140,"C":2545,"G":300,"O":810}` |
| 5 | `bend_type` | spiral | `T3` |

> "The drawing gives a spacing but never a reinforced run length. A tool
> that guesses gets a plausible number. This one asks."

Questions 2 and 5 are worth a sentence, because they look pedantic and are
not:

> "It has the leg dimensions and still won't compute a length, because the
> shape decides which legs are summed. On a spiral, column O is geometry and
> never enters the cutting length. Guess the shape and you get a number
> that's wrong by a hook."

### 6:00 — Answer, and watch it release (3 min)
Answer each question with a name and a rationale. The schedule recalculates
in front of them after every answer — partial release is normal and worth
pointing out: the longitudinal bars release while the spiral is still
unanswered.

Then the rulebook gate: the numbers are all calculated and **still** nothing
releases until an engineer signs the assumption sheet.

> "Calculated is not the same as releasable. That distinction is the
> product."

Sign it. Released mass goes to **5,991.4 kg**.

### 9:00 — Trace one number (2 min)
**Bar schedule** → click any quantity → the evidence drawer opens.

Quantity formula, cutting-length formula, mass formula, release gates,
rulebook version, and the hash-chained evidence records — for that one line.

> "Every number on this page opens like this."

### 11:00 — Value and accuracy (2 min)
**Value & accuracy**.

Machine time, review time (measured in this session, in front of them),
claims released per claim rather than per document, knowledge reused.

Then scroll to **Accuracy against the reference**: 100% line recall, 100%
exact lines, **0.0 kg mass variance** against the client's own bar list —
which the extraction path never reads.

> "That reference was produced by your team. The pipeline never sees it. It
> is read after generation, only to check us."

Be careful with the saving figure. If review time in the room was thirty
seconds, say so — the honest claim is *machine time plus measured review*,
on one project.

### 13:00 — Spatial and downloads (1 min)
**Drawing intelligence → 3D view**: released steel is solid, held steel is
dashed amber. The picture cannot say "done" while the schedule says
"waiting".

**Download pack**: BBS as XLSX/PDF/CSV, the exception report, the evidence
chain.

### 14:00 — Close (1 min)
> "Two element families read today, pile and pad footing, both reconciling
> exactly against your own bar lists. The third and fourth are playbooks, not
> rewrites. What would you want us to read next?"

Stop. Let them ask.

---

## 3. Failure injection — rehearse these

Do each one at least once before the client session so the recovery is
muscle memory, not improvisation.

| If this happens | Do this | Say this |
|---|---|---|
| A run hangs or errors | New run → **Recorded runs** | "I'll switch to a recorded run — same project, captured earlier." **Say it out loud. Never pass a recording off as live.** |
| XLSX download 503s | Use the PDF or CSV | "The spreadsheet writer isn't installed on this host; same data, different file." |
| 3D view is blank | Move on to the schedule | It is a completeness view, not the product. Do not debug it live. |
| Drawing page won't render | Use the seeded scenarios | The governance story does not depend on the PDF renderer. |
| Someone asks for a drawing family you can't read | Show the project list | "No playbook reads that family yet — the app says so rather than returning an empty schedule." |
| Network dies entirely | Run locally on the laptop | Keep `uvicorn` ready on `localhost:8000` with `TRUSTSIGHT_CORPUS` set. |

---

## 4. Questions you will be asked

**"Does it use AI?"**
Yes, for interpretation — reading a drawing and proposing what a callout
means. It never does the arithmetic and it never approves anything. In this
build the extraction is deterministic playbook code, so no model is called
at all, and the app says so rather than implying one ran.

**"What's the accuracy?"**
On the two projects with playbooks: exact. 5,991.4 kg and 325.6 kg against
the client's own bar lists, every line matching on quantity and cutting
length. On the other three: no playbook reads that element family yet, and
the app reports that instead of generating something.

**"How much time does it save?"**
We measure it rather than quote it. Give us your estimator's current time
for a scope and the run reports the difference using measured machine and
review time. We do not publish a percentage from one pile.

**"Can it handle scanned drawings?"**
Not in this build. The unstructured route is refused outright — see below.

**"What happens when it's wrong?"**
Everything that produced the number is on screen and in the exported
evidence chain: source citation, rule version, formula, approver, rationale,
and the hash of the record before it. You can find the wrong input rather
than argue about the output.

---

## 5. Say this about the unstructured route

It will come up, because `/ready` reports it as unavailable and the pipeline
screen shows it greyed.

> "That route needs sheet classification, element detection and model-based
> reinforcement interpretation. None of those are built yet, so the product
> refuses the route instead of starting a run that fails halfway. We'd rather
> show you a boundary than a broken run."

This is a strength on a governance product. Do not apologise for it and do
not promise a date you have not costed.

---

## 6. Do not

- Do not open the code. Not once.
- Do not run the unstructured route "to see what happens".
- Do not quote a time saving without saying what it was measured against.
- Do not show a recorded run without saying it is recorded.
- Do not promise an element family that has no playbook.
- Do not enable `STATE_MACHINE_ARN`.

---

## 7. After the session

1. Note every drawing family they asked for that has no playbook. That list
   is the roadmap.
2. Note the manual baseline they gave, if any. It is the only honest input
   to an ROI case.
3. If they offered a new drawing set, ask for the reference bar list with
   it — without one, nothing can be verified.
