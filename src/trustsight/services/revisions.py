"""Rev-to-rev comparison (spec v2 REV-101).

A drawing revision arriving late is the normal way an estimate goes wrong.
The question an estimator actually asks is not "what bytes changed" but
"does the steel change, and where do I have to look".

So this compares the two revisions at the level the engine reads them — the
facts each drawing *states* — and reports the quantity impact only where both
revisions state enough to compute one. Where they do not, it says which facts
are still open rather than producing a kilogram figure from a half-read
drawing. That is the same rule the rest of the product follows: a number is
either earned or absent.

The comparison never mutates either revision. It reads the bytes registered
for each and nothing else.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..extraction import playbooks


@dataclass(frozen=True)
class Side:
    """One revision, read."""

    revision: int
    document_id: str
    sha256: str
    bytes_: int
    uploaded_by: str
    uploaded_at: str
    readable: bool
    playbook: str | None
    facts: dict[str, Any]
    elements: int
    unresolved: list[str]
    error: str | None = None


def _key(fact: Any) -> str:
    """A stable name for a fact, scoped to the element it describes."""
    sheet = getattr(fact.sheet, "sheet_no", None) or ""
    return f"{fact.field}@{sheet}" if sheet else str(fact.field)


def read_side(doc: Any) -> Side:
    """Extract one revision. Never raises — an unreadable revision is a fact."""
    base = dict(revision=doc.revision, document_id=doc.document_id,
                sha256=doc.sha256, bytes_=doc.bytes_,
                uploaded_by=doc.uploaded_by, uploaded_at=doc.uploaded_at)
    path = Path(doc.path) if doc.path else None
    if path is None or not path.exists():
        return Side(**base, readable=False, playbook=None, facts={}, elements=0,
                    unresolved=[], error="the registered file is not on this host")
    try:
        book = playbooks.select(path)
        result = book.reader(path, "revision-compare")
    except playbooks.NoPlaybook:
        return Side(**base, readable=False, playbook=None, facts={}, elements=0,
                    unresolved=[], error="no implemented playbook reads this drawing")
    except Exception as exc:                       # a corrupt PDF, say
        return Side(**base, readable=False, playbook=None, facts={}, elements=0,
                    unresolved=[], error=f"could not be read: {exc}")
    return Side(**base, readable=True, playbook=book.name,
                facts={_key(f): f.value for f in result.facts},
                elements=len(result.elements),
                unresolved=list(result.unresolved))


#: Facts that move a quantity. A change to one of these is a commercial
#: event; a change to anything else is a drafting one.
QUANTITY_BEARING = (
    "instances", "count", "quantity", "diameter", "length", "run_length_mm",
    "spacing", "spacing_mm", "size", "bar_size", "legs", "bend_type",
    "cover", "cover_mm",
)


def _bears_on_quantity(field: str) -> bool:
    name = field.split("@", 1)[0]
    return any(token in name for token in QUANTITY_BEARING)


def compare(family: str, older: Any, newer: Any) -> dict[str, Any]:
    """Compare two registered revisions of the same drawing."""
    a, b = read_side(older), read_side(newer)

    if a.sha256 and a.sha256 == b.sha256:
        return {
            "family": family,
            "from": a.revision, "to": b.revision,
            "identical": True,
            "headline": "These two revisions are the same file.",
            "detail": ("Both revisions hash to the same value, so nothing was "
                       "re-read and no quantity can have moved."),
            "changes": [], "quantity_impact": None,
            "sides": [_side_dict(a), _side_dict(b)],
        }

    changes: list[dict[str, Any]] = []
    for key in sorted(set(a.facts) | set(b.facts)):
        before, after = a.facts.get(key), b.facts.get(key)
        if before == after:
            continue
        changes.append({
            "field": key,
            "before": before,
            "after": after,
            "kind": "added" if before is None
                    else "removed" if after is None else "changed",
            "affects_quantity": _bears_on_quantity(key),
        })

    blocked = [s for s in (a, b) if not s.readable]
    open_facts = sorted(set(a.unresolved) | set(b.unresolved))

    if blocked:
        impact: dict[str, Any] | None = {
            "available": False,
            "reason": ("; ".join(f"revision {s.revision} {s.error}" for s in blocked)
                       + " — no quantity impact is claimed for a revision this "
                         "build cannot read."),
        }
    elif open_facts:
        impact = {
            "available": False,
            "reason": ("the drawing still does not state "
                       + ", ".join(open_facts[:4])
                       + ". A quantity impact computed over an unanswered fact "
                         "would be a guess, so none is shown until these are "
                         "resolved on both revisions."),
            "open_facts": open_facts,
        }
    else:
        moving = [c for c in changes if c["affects_quantity"]]
        impact = {
            "available": True,
            "quantity_bearing_changes": len(moving),
            "element_delta": b.elements - a.elements,
            "summary": (
                f"{len(moving)} of {len(changes)} changed facts bear on a "
                f"quantity." if changes else
                "The drawings differ but no fact the engine reads has changed."),
            "note": ("Re-run the project on the current revision to produce the "
                     "released mass. This screen reports what moved, not a "
                     "figure computed from a drawing nobody has approved."),
        }

    moving_count = sum(1 for c in changes if c["affects_quantity"])
    if not changes:
        headline = "The file changed, but no fact the engine reads did."
    elif moving_count:
        headline = (f"{moving_count} quantity-bearing "
                    f"{'change' if moving_count == 1 else 'changes'} "
                    f"between rev {a.revision} and rev {b.revision}.")
    else:
        headline = (f"{len(changes)} facts changed, none of which moves a "
                    "quantity.")

    return {
        "family": family,
        "from": a.revision, "to": b.revision,
        "identical": False,
        "headline": headline,
        "detail": ("Every fact below was read from the drawing itself. Facts "
                   "the engine does not read cannot appear here, which is why "
                   "a revision with no listed change is not the same claim as "
                   "a revision with no change."),
        "changes": changes,
        "quantity_impact": impact,
        "sides": [_side_dict(a), _side_dict(b)],
    }


def _side_dict(s: Side) -> dict[str, Any]:
    return {"revision": s.revision, "document_id": s.document_id,
            "sha256": s.sha256, "bytes": s.bytes_,
            "uploaded_by": s.uploaded_by, "uploaded_at": s.uploaded_at,
            "readable": s.readable, "playbook": s.playbook,
            "facts_read": len(s.facts), "elements": s.elements,
            "unresolved": s.unresolved, "error": s.error}
