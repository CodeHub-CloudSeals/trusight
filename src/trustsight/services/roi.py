"""Measured value. Spec section 12.

The rule this module exists to enforce: every number here is something the
run actually recorded. There is no "70% faster" constant, no assumed hourly
rate and no baseline invented on the client's behalf. If the estimator did
not supply a manual baseline, the saving is reported as unavailable with the
reason stated, because a time saving computed against a number nobody
measured is a sales figure wearing an engineering label — and a buyer who
later discovers that is entitled to distrust the rest of the page.

Three families of metric:

* effort     — what the machine and the human each spent (measured)
* claims     — how much work reached a releasable number, counted per claim
               rather than per document, because one document can carry a
               released bar and a blocked one at the same time
* accuracy   — generated against the client's own reference bar list, which
               the pipeline never reads. Recall is listed before variance
               deliberately: a missed element is a safety problem, a
               quantity that differs by two bars is a commercial one.
"""
from __future__ import annotations

from typing import Any

from ..evidence.fabric import claim_subject


def claim_summary(ctx: Any) -> dict[str, int]:
    """Release outcome per claim, plus the claims that never got that far.

    An exception is a claim too: it is reinforcement the drawing described
    and the engine refused to quantify. Counting only the schedule lines
    would report 100% of a job that dropped half its steel.
    """
    released = review = blocked = 0
    if ctx.schedule:
        approved = ctx.rulebook.is_approved()
        for item in ctx.schedule.items:
            state = ctx.chain.release_status(
                claim_subject(item.element_key or "", item.claim_id),
                rulebook_approved=approved).state.value
            if state == "released":
                released += 1
            elif state == "review":
                review += 1
            else:
                blocked += 1
    unresolved = len(ctx.schedule.exceptions) if ctx.schedule else 0
    total = released + review + blocked + unresolved
    return {"released": released, "review": review, "blocked": blocked,
            "unresolved": unresolved, "total": total}


def _accuracy(benchmark: dict[str, Any] | None,
              generated_mass_kg: float) -> dict[str, Any] | None:
    """Generated against the reference bar list the pipeline never reads."""
    if not benchmark or not benchmark.get("rows"):
        return None
    rows = benchmark["rows"]
    produced = [r for r in rows if r.get("generated_quantity") is not None]
    exact = [r for r in rows if r.get("match")]
    reference_mass = float(benchmark.get("mass_kg") or 0.0)

    lines = []
    for r in rows:
        got, want = r.get("generated_quantity"), r.get("reference_quantity")
        lines.append({
            "mark": r.get("mark"),
            "size": r.get("size"),
            "reference_quantity": want,
            "generated_quantity": got,
            "quantity_variance": None if got is None else got - want,
            "reference_length_mm": r.get("reference_length_mm"),
            "generated_length_mm": r.get("generated_length_mm"),
            "length_variance_mm": (
                None if r.get("generated_length_mm") is None
                else r["generated_length_mm"] - r["reference_length_mm"]),
            "outcome": ("exact" if r.get("match")
                        else "not_generated" if got is None else "variance"),
        })

    mass_variance = round(generated_mass_kg - reference_mass, 1)
    return {
        "label": benchmark.get("label"),
        # recall first: a line the engine never produced is the failure that
        # matters, and it is invisible in a variance average
        "reference_lines": len(rows),
        "lines_generated": len(produced),
        "element_recall": round(len(produced) / len(rows), 4) if rows else None,
        "lines_exact": len(exact),
        "exact_rate": round(len(exact) / len(rows), 4) if rows else None,
        "reference_mass_kg": round(reference_mass, 1),
        "generated_mass_kg": round(generated_mass_kg, 1),
        "mass_variance_kg": mass_variance,
        "mass_variance_pct": (round(mass_variance / reference_mass * 100, 2)
                              if reference_mass else None),
        "lines": lines,
    }


def report(run: Any, ctx: Any, benchmark: dict[str, Any] | None = None,
           generated_mass_kg: float = 0.0) -> dict[str, Any]:
    """The full ROI payload for one run."""
    measured = run.roi()
    machine_ms = measured["machine_ms"]
    human_seconds = round(float(getattr(ctx, "human_seconds", 0.0)), 1)
    baseline = getattr(ctx, "manual_baseline_minutes", None)
    human_decisions = len([r for r in ctx.chain.records
                           if r.claim_type.value == "approval"
                           and "#" not in r.subject])

    # A run whose clarifications were answered in under five seconds was
    # answered by a script — a freeze, a test, a rehearsal replay. Its review
    # time is not a person's, so the saving computed from it measures machine
    # time and calls it productivity. That is the single most quotable wrong
    # number this product could produce, so it is labelled at the source
    # rather than left for whoever reads the screen to notice.
    scripted = human_decisions > 0 and human_seconds < 5
    claims = claim_summary(ctx)
    total = claims["total"]

    if baseline and not claims["released"]:
        # The job is not done. Comparing an unfinished run's elapsed time
        # against an estimator's time for a finished one produces the most
        # flattering number the product can generate and the least true:
        # a screen reading "100% time saved" beside "0 of 2 lines produced".
        # No saving is reported until something has actually released.
        saving: dict[str, Any] = {
            "available": False,
            "reason": (
                "Nothing has released on this run yet, so there is no "
                "completed work to set against the baseline. The comparison "
                "appears once at least one claim passes its release gates."),
            "manual_baseline_minutes": baseline,
        }
    elif baseline:
        elapsed_minutes = machine_ms / 60000 + human_seconds / 60
        saved = baseline - elapsed_minutes
        partial = claims["released"] < total
        saving = {
            "available": True,
            "manual_baseline_minutes": baseline,
            "elapsed_minutes": round(elapsed_minutes, 2),
            "minutes_saved": round(saved, 2),
            "pct_saved": round(max(-100.0, min(100.0, saved / baseline * 100)), 1),
            "review_time_representative": not scripted,
            # What the comparison actually covers. A saving measured over
            # half a job is not a saving over the job.
            "covers": f"{claims['released']} of {total} claims released",
            "complete": not partial,
            "partial_note": (
                f"{claims['needing_human']} claim(s) on this run have not "
                "released. The work they still need is not in the elapsed "
                "time above, so this comparison covers part of the scope, "
                "not all of it." if partial else ""),
            "caveat": (
                f"Review time here is {human_seconds}s across "
                f"{human_decisions} decisions, which is a script answering, "
                "not an estimator. This figure therefore measures machine "
                "time only — do not quote it as a productivity number."
                if scripted else
                "One project, this run. A baseline measured across a full "
                "drawing set is the only figure worth quoting to a board."),
        }
    else:
        saving = {
            "available": False,
            "reason": (
                "No manual baseline was entered for this project, so no time "
                "saving is claimed. Enter the estimator's current time for "
                "this scope at run start and it is computed against measured "
                "machine and review time."),
        }

    return {
        "run_id": run.run_id,
        "iteration": getattr(run, "iteration", 1),
        "route": run.route,
        "playbook": getattr(ctx, "playbook", None),
        "effort": {
            "machine_ms": machine_ms,
            "machine_seconds": round(machine_ms / 1000, 2),
            "human_seconds": human_seconds,
            "human_source": (
                "wall-clock between a question appearing and its answer being "
                "submitted in this session"),
        },
        "saving": saving,
        "claims": {
            **claims,
            # "automation" here means: reached a number a person can issue.
            # Not "done without a human" — a released claim may well have
            # taken an estimator's answer, and pretending otherwise would
            # sell the wrong product.
            "release_rate": round(claims["released"] / total, 4) if total else None,
            "needing_human": claims["review"] + claims["blocked"] + claims["unresolved"],
        },
        "work": {
            "values_extracted": measured["values_extracted"],
            "exceptions": measured["exceptions"],
            "knowledge_reused": measured["reused_knowledge"],
            "human_decisions": human_decisions,
            "open_questions": len(ctx.questions),
        },
        "accuracy": _accuracy(benchmark, generated_mass_kg),
        # agent-by-agent: which step cost what, so a slow stage is visible
        # rather than averaged away
        "steps": measured["steps"],
    }
