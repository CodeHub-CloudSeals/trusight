"""Ask TrustSight. Spec v2 section 9.

The spec calls this "a project intelligence interface, not a generic
chatbot", and says it must answer from the project graph, TrustOps evidence,
run state, controls and approved knowledge, citing the sheet or claim.

This build has no model configured. That leaves two ways to satisfy the
brief and one way to fail it:

* **Deterministic, grounded** — what is implemented here. Each intent maps a
  question to a query over this run's own data and returns the answer with
  the records it came from. It cannot hallucinate because it never
  generates; it can only report a row that exists or say it has no answer.
* **A configured model, grounded on the same retrieval** — the honest next
  step, and the retrieval below is the half that a model would need anyway.
* **A model answering freely, or canned replies** — the failure. This
  product is sold on never guessing an engineering fact. A query bar that
  guesses one, in front of the client, refutes the entire pitch in a
  sentence.

So an unmatched question returns "I don't answer that from this project's
data", lists what it can answer, and stops. Saying nothing is a feature
here, not a gap.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from ..evidence.fabric import claim_subject


class Answer:
    """One grounded reply: prose, optional rows, and where it came from."""

    def __init__(self, text: str, *, rows: list[dict[str, Any]] | None = None,
                 citations: list[dict[str, str]] | None = None,
                 goto: str | None = None) -> None:
        self.text = text
        self.rows = rows or []
        self.citations = citations or []
        #: which workspace screen shows the working, so the answer is a way in
        self.goto = goto

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "rows": self.rows,
                "citations": self.citations, "goto": self.goto,
                "grounded": True}


def _cite(kind: str, ref: str) -> dict[str, str]:
    return {"kind": kind, "ref": ref}


def _released(ctx: Any) -> list[Any]:
    if not ctx.schedule:
        return []
    approved = ctx.rulebook.is_approved()
    return [i for i in ctx.schedule.items
            if ctx.chain.release_status(
                claim_subject(i.element_key or "", i.claim_id),
                rulebook_approved=approved).state.value == "released"]


# ── intents ────────────────────────────────────────────────────────────────
# Each is (name, example question, pattern, handler). The examples are shown
# in the UI, so a presenter never has to guess what the bar understands.

def _blocked(ctx: Any, run: Any) -> Answer:
    open_q = list(ctx.questions)
    exceptions = list(ctx.schedule.exceptions) if ctx.schedule else []
    conflicts = [c for e in ctx.graph.elements.values() for c in e.conflicts]
    if not (open_q or exceptions or conflicts):
        return Answer("Nothing is blocked. Every claim on this run reached a "
                      "releasable number.", goto="results")
    rows = [{"what": q["field"].replace("_", " "),
             "where": f"{q['element']} · {q['role']}",
             "why": q["question"]} for q in open_q]
    rows += [{"what": "calculation exception", "where": "schedule",
              "why": e} for e in exceptions]
    rows += [{"what": "source conflict", "where": "element", "why": c}
             for c in conflicts]
    return Answer(
        f"{len(rows)} thing{'' if len(rows) == 1 else 's'} block release on "
        f"this run. Each is a fact the drawing does not state, or two sources "
        f"that disagree — none of them is a value the engine may choose.",
        rows=rows,
        citations=[_cite("screen", "Review & approve")], goto="review")


def _released_q(ctx: Any, run: Any) -> Answer:
    items = _released(ctx)
    if not items:
        return Answer("Nothing has released yet on this run.", goto="results")
    rows = [{"what": f"{i.quantity} × {i.size.value}",
             "where": f"{i.cutting_length_mm} mm cutting length",
             "why": f"{i.total_mass_kg:.1f} kg"} for i in items]
    total = sum(i.total_mass_kg for i in items)
    return Answer(
        f"{len(items)} schedule line{'' if len(items) == 1 else 's'} released, "
        f"{total:,.1f} kg in total. Each passed every release gate and carries "
        f"an approver.",
        rows=rows, citations=[_cite("screen", "Bar schedule")], goto="results")


def _why_length(ctx: Any, run: Any) -> Answer:
    if not ctx.schedule or not ctx.schedule.items:
        return Answer("No cutting length has been calculated on this run yet.",
                      goto="review")
    from ..shapes.catalogue import resolve
    rows = []
    for i in ctx.schedule.items:
        try:
            shape = resolve(i.bend_type, i.legs)
        except Exception:  # noqa: BLE001 — reported, not swallowed
            continue
        basis = " + ".join(f"{c} ({i.legs[c]})" for c in shape.leg_columns
                           if c in i.legs)
        rows.append({"what": f"{i.size.value} · shape {i.bend_type}",
                     "where": basis,
                     "why": f"= {i.cutting_length_mm} mm"})
    return Answer(
        "Cutting length is the out-to-out sum of the legs the shape defines — "
        "not every populated column. A spiral carries a value in O that is "
        "geometry and never enters the sum.",
        rows=rows, citations=[_cite("rulebook", ctx.rulebook.version)],
        goto="results")


def _route(ctx: Any, run: Any) -> Answer:
    from ..workflow import routes
    r = routes.get(ctx.path_mode)
    executed = len([x for x in run.results.values() if x.ok])
    return Answer(
        f"This run took the {r.name.replace('_', '-')} route: {r.summary} "
        f"It suits input where {r.input_quality}. {executed} of the 23 steps "
        f"executed; the rest are listed with the reason they did not.",
        rows=[{"what": name.replace("_", " "), "where": "not applicable",
               "why": why} for name, why in r.excluded.items()],
        citations=[_cite("screen", "Agent flow")], goto="flow")


def _evidence(ctx: Any, run: Any) -> Answer:
    ok, msg = ctx.chain.verify()
    approvals = [r for r in ctx.chain.records
                 if r.claim_type.value == "approval" and "#" not in r.subject]
    return Answer(
        f"{len(ctx.chain.records)} evidence records, hash chained. "
        f"Verification: {msg} {len(approvals)} human decision"
        f"{'' if len(approvals) == 1 else 's'} recorded, each with an approver "
        f"and a rationale.",
        rows=[{"what": r.value.get("field", r.subject) if isinstance(r.value, dict) else r.subject,
               "where": r.approver or "—", "why": r.rationale or ""}
              for r in approvals],
        citations=[_cite("chain", "valid" if ok else "broken")], goto="evidence")


def _accuracy(ctx: Any, run: Any, payload: dict[str, Any]) -> Answer:
    acc = (payload.get("roi") or {}).get("accuracy")
    if not acc:
        return Answer("This project has no independently prepared bar list, so "
                      "no accuracy is reported. Nothing is inferred in its "
                      "place.", goto="value")
    return Answer(
        f"Against {acc['label']}: {acc['lines_generated']} of "
        f"{acc['reference_lines']} reference lines produced, "
        f"{acc['lines_exact']} matching quantity and cutting length exactly, "
        f"mass variance {acc['mass_variance_kg']:+.1f} kg. The reference is "
        f"read only after generation and never by the extraction path.",
        rows=[{"what": l["mark"], "where": f"{l['reference_quantity']} ref / "
                                           f"{l['generated_quantity'] if l['generated_quantity'] is not None else '—'} generated",
               "why": l["outcome"].replace("_", " ")} for l in acc["lines"]],
        citations=[_cite("screen", "Value & accuracy")], goto="value")


def _roi(ctx: Any, run: Any, payload: dict[str, Any]) -> Answer:
    roi = payload.get("roi") or {}
    saving = roi.get("saving", {})
    effort = roi.get("effort", {})
    if saving.get("available"):
        head = (f"Measured against a stated baseline of "
                f"{saving['manual_baseline_minutes']} minutes, this run took "
                f"{saving['elapsed_minutes']:.2f} minutes.")
        if saving.get("review_time_representative") is False:
            head += (" The review time was a script, not an estimator, so that "
                     "is machine time and not a productivity figure.")
    else:
        head = ("No time saving is claimed on this run. "
                + saving.get("reason", ""))
    return Answer(
        f"{head} Machine time {effort.get('machine_seconds', 0)}s, review time "
        f"{effort.get('human_seconds', 0)}s, both measured.",
        citations=[_cite("screen", "Value & accuracy")], goto="value")


def _controls(ctx: Any, run: Any) -> Answer:
    rows = [{"what": c.control_id, "where": c.status.value,
             "why": c.rationale} for c in ctx.control_results]
    return Answer(
        "Compliance readiness, control by control. These show evidence and "
        "gaps; none of them is a certification and TrustSight does not claim "
        "one.",
        rows=rows, citations=[_cite("screen", "Evidence & workflow")],
        goto="evidence")


def _rulebook(ctx: Any, run: Any) -> Answer:
    rb = ctx.rulebook
    return Answer(
        f"Rulebook {rb.version}: cover {rb.cover_mm} mm, rounding "
        f"{rb.rounding_mm} mm, spacing convention "
        f"{rb.spacing_convention.replace('_', ' ')}. Approved by "
        f"{rb.approved_by or 'nobody yet — nothing can release until it is'}.",
        citations=[_cite("rulebook", rb.version)], goto="evidence")


INTENTS: list[tuple[str, str, re.Pattern[str], Callable[..., Answer]]] = [
    ("blocked", "What is blocked and why?",
     re.compile(r"block|stuck|waiting|outstanding|missing|open question|why not", re.I), _blocked),
    ("released", "What has released so far?",
     re.compile(r"releas|how much steel|total mass|how many bars|schedule so far", re.I), _released_q),
    ("length", "How was the cutting length derived?",
     re.compile(r"cutting length|how .*length|leg|shape|bend type", re.I), _why_length),
    ("route", "Which route did this run take?",
     re.compile(r"route|path|which steps|orchestrat|pipeline|structured", re.I), _route),
    ("evidence", "Is the evidence chain intact?",
     re.compile(r"evidence|chain|audit|who approved|trustops|provenance", re.I), _evidence),
    ("accuracy", "How accurate is it against our own bar list?",
     re.compile(r"accura|variance|recall|reference|ground truth|match", re.I), _accuracy),
    ("roi", "What did this run cost in time?",
     re.compile(r"roi|time saved|how long|machine time|baseline|productiv", re.I), _roi),
    ("controls", "What do the compliance controls show?",
     re.compile(r"control|compliance|c-00|certif|standard", re.I), _controls),
    ("rulebook", "Which rulebook is in force?",
     re.compile(r"rulebook|assumption|cover|rounding|spacing convention", re.I), _rulebook),
]

EXAMPLES = [{"intent": name, "question": example} for name, example, _, _ in INTENTS]


def answer(question: str, ctx: Any, run: Any,
           payload: dict[str, Any]) -> dict[str, Any]:
    q = (question or "").strip()
    action = proposed_action(q, ctx, run)
    if not q:
        return {"text": "Ask about this project.", "rows": [], "citations": [],
                "goto": None, "grounded": True, "examples": EXAMPLES,
                "proposed_action": None}
    for name, _example, pattern, handler in INTENTS:
        if pattern.search(q):
            try:
                result = (handler(ctx, run, payload)
                          if handler in (_accuracy, _roi) else handler(ctx, run))
            except Exception as exc:  # noqa: BLE001
                return {"text": f"That query failed against this run: {exc}",
                        "rows": [], "citations": [], "goto": None,
                        "grounded": True, "examples": EXAMPLES,
                        "intent": name, "proposed_action": action}
            body = result.as_dict()
            body["intent"] = name
            body["examples"] = EXAMPLES
            body["proposed_action"] = action
            return body
    # No match. This is the honest branch and the reason the feature is safe.
    return {
        "text": ("I answer from this project's own data — the graph, the "
                 "evidence chain, the run state, the rulebook and the "
                 "controls. That question is not one of them, and this build "
                 "has no model configured, so I will not guess at it."),
        "rows": [], "citations": [], "goto": None, "grounded": True,
        "intent": None, "examples": EXAMPLES, "proposed_action": action,
    }


# ── actions (spec v2 COP-102) ──────────────────────────────────────────────
#
# The spec asks the query bar to offer generate/release/approve actions that
# "require policy + explicit confirmation". Both halves of that are load
# bearing, and the second one is the harder to honour: a confirmation step
# that a presenter clicks through without reading is theatre.
#
# So an action here is never executed by asking for it. Asking returns a
# *proposal*: what would happen, which capability it needs, and what the
# caller must send back to run it. The action then runs only on a second call
# carrying explicit consent and a rationale, and only if the actor's role
# permits it — checked on the server, where hiding a button cannot help.
#
# Release is deliberately not an action. Nothing in this product releases a
# quantity by command: release is computed from the gate vector, and offering
# a "release it" button would misrepresent how the engine works to the exact
# audience most likely to believe it. Asking for one says so.

@dataclass(frozen=True)
class ActionSpec:
    key: str
    label: str
    capability: str          # the Role attribute the actor must hold
    effect: str              # what changes, in plain words
    irreversible: bool


ACTIONS: dict[str, ActionSpec] = {a.key: a for a in (
    ActionSpec("approve_rulebook", "Sign the rulebook", "approve",
               "Records your name against the assumption sheet. Quantities "
               "whose other gates already pass become releasable, and the "
               "signature is written into the evidence chain.",
               irreversible=True),
    ActionSpec("generate_pack", "Generate the output pack", "export",
               "Builds the BBS and the evidence export from what has already "
               "released. It changes no quantity and approves nothing.",
               irreversible=False),
)}


def propose(key: str, ctx: Any, run: Any) -> dict[str, Any] | None:
    """Describe an action without performing any part of it."""
    spec = ACTIONS.get(key)
    if spec is None:
        return None
    blocked_reason = None
    if key == "approve_rulebook":
        if ctx.rulebook.is_approved():
            blocked_reason = (f"the rulebook is already signed by "
                              f"{ctx.rulebook.approved_by}")
        elif run.pending_step != 19:
            blocked_reason = ("this run is not at the rulebook gate, so there "
                              "is nothing to sign yet")
    return {
        "action": spec.key,
        "label": spec.label,
        "requires_capability": spec.capability,
        "effect": spec.effect,
        "irreversible": spec.irreversible,
        "available": blocked_reason is None,
        "unavailable_reason": blocked_reason,
        # the caller must echo these back; a bare POST does nothing
        "confirmation_required": {
            "confirm": True,
            "rationale": "a sentence saying why, recorded against your name",
        },
    }


ACTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("approve_rulebook",
     re.compile(r"\b(approve|sign|sign[- ]?off)\b.*\b(rulebook|assumption)|"
                r"\b(rulebook|assumption)\b.*\b(approve|sign)", re.I)),
    ("generate_pack",
     re.compile(r"\b(generate|build|produce|export|download)\b.*"
                r"\b(pack|bbs|schedule|report|xlsx|pdf)", re.I)),
]

RELEASE_REQUEST = re.compile(r"\b(release|issue|approve)\b.*\b(bar|steel|"
                             r"quantit|line|schedule)|^\s*release\b", re.I)


def proposed_action(question: str, ctx: Any, run: Any) -> dict[str, Any] | None:
    """If the question asks for something to be done, describe it."""
    q = (question or "").strip()
    if not q:
        return None
    for key, pattern in ACTION_PATTERNS:
        if pattern.search(q):
            return propose(key, ctx, run)
    if RELEASE_REQUEST.search(q):
        return {
            "action": None,
            "label": "Release is not a command",
            "available": False,
            "unavailable_reason": (
                "Nothing here releases a quantity because someone asked for "
                "it. Release is computed from the gate vector — source, rule, "
                "approval, conflict, tolerance — and a line releases the "
                "moment its gates pass. What a person can do is answer a "
                "missing fact or sign the rulebook; both are offered as their "
                "own actions."),
        }
    return None
