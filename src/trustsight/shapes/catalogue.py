"""Shape catalogue: which bar-list columns are legs for a given bend type.

Spec section 4.2. This is the single most error-prone piece of the engine.

Two facts that a naive implementation gets wrong:

1. **Populated columns are not always legs.** A ``T3`` spiral carries a value
   in column ``O`` that is excluded from the cutting length; a ``B16A``
   multi-bend carries values in ``H`` and ``K`` that are excluded. Summing
   every populated column reconciles 27 of the 29 sampled items and is
   confidently wrong on the other two.

2. **The bar mark does not determine the legs.** Mark ``15A01`` appears as
   bend type ``17`` (legs B+C+D) in one project and as bend type ``T3``
   (legs A+B+C+G) in another. The bend type is the shape family; the mark is
   a size prefix plus a project-local shape reference.

The entries below were observed across all five supplied bar lists and are
the *validation set*, not the source of truth. Production must seed this
catalogue from the licensed ACI / RebarCAD bend-type reference (spec D6) and
assert agreement with these observations before use.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..models.core import LEG_COLUMNS


@dataclass(frozen=True)
class ShapeDef:
    """One entry in the bend-type catalogue."""

    bend_type: str
    leg_columns: tuple[str, ...]
    #: columns that may hold values but are never part of the cutting length
    excluded_columns: tuple[str, ...] = ()
    description: str = ""
    observed_marks: tuple[str, ...] = field(default=())

    def cutting_length(self, legs: dict[str, int]) -> int:
        return sum(int(legs[c]) for c in self.leg_columns if c in legs)

    def missing_legs(self, legs: dict[str, int]) -> list[str]:
        return [c for c in self.leg_columns if c not in legs]


#: A straight bar: no mark, no bend type, full length in column B.
STRAIGHT = ShapeDef(
    bend_type="",
    leg_columns=("B",),
    description="Straight bar; cutting length carried in column B.",
)


#: Observed catalogue. Key is (bend_type, leg_signature) because one bend type
#: can carry different leg counts for different shapes.
CATALOGUE: dict[str, list[ShapeDef]] = {
    "": [STRAIGHT],
    "2": [
        ShapeDef(
            "2", ("A", "B"),
            description="Single end hook (A) plus straight body (B).",
            observed_marks=("10A01", "15A02", "15A31", "30A01"),
        ),
        ShapeDef(
            "2", ("A", "B", "G"),
            description="Two end hooks (A, G) plus straight body (B).",
            observed_marks=("15A03", "15A04", "15A05"),
        ),
    ],
    "17": [
        ShapeDef(
            "17", ("B", "C", "D"),
            description="U-shape / hairpin: two legs (B, D) and a return (C).",
            observed_marks=("15A01", "15A17", "15A18", "15A19", "20A01"),
        ),
        ShapeDef(
            "17", ("B", "C"),
            description="Two-leg bend.",
            observed_marks=("15A30",),
        ),
    ],
    "T3": [
        ShapeDef(
            "T3", ("A", "B", "C", "G"), excluded_columns=("O",),
            description="Spiral / helical family. Column O holds pitch or "
                        "overall geometry and is excluded from cutting length.",
            observed_marks=("15A01",),
        )
    ],
    "B16A": [
        ShapeDef(
            "B16A", ("A", "B", "C", "D", "E"), excluded_columns=("H", "K"),
            description="Multi-bend. H and K carry secondary geometry and are "
                        "excluded from cutting length.",
            observed_marks=("15A20",),
        )
    ],
}


class ShapeResolutionError(RuntimeError):
    """Raised when no catalogue entry can account for a bar's geometry."""


def resolve(bend_type: str | None, legs: dict[str, int]) -> ShapeDef:
    """Return the catalogue entry whose legs are present in ``legs``.

    Selection is by which entry's leg columns are all populated, preferring
    the entry with the most legs. This never inspects the stated cutting
    length, so it remains usable when generating a length rather than
    checking one.
    """
    if bend_type is not None and not isinstance(bend_type, str):
        # A bend type arriving as a number means something upstream accepted
        # an answer it should have rejected. Raising the module's own error
        # keeps that a handled exception the run can report, rather than an
        # AttributeError that fails the whole run with a 500.
        raise ShapeResolutionError(
            f"bend type must be a shape reference such as '2' or 'T3'; "
            f"got {bend_type!r}"
        )
    bt = (bend_type or "").strip()
    candidates = CATALOGUE.get(bt)
    if candidates is None:
        raise ShapeResolutionError(
            f"bend type {bt!r} is not in the catalogue; seed it from the ACI "
            f"reference before use (spec D6)"
        )
    viable = [c for c in candidates if not c.missing_legs(legs)]
    if not viable:
        want = " or ".join("+".join(c.leg_columns) for c in candidates)
        raise ShapeResolutionError(
            f"bend type {bt!r} expects legs {want}; got {sorted(legs)}"
        )
    return max(viable, key=lambda c: len(c.leg_columns))


def verify(bend_type: str | None, legs: dict[str, int], stated_length: int) -> tuple[bool, str]:
    """Check a bar against the catalogue.

    Used by the evaluation harness to validate a reference bar list, and by
    the QA agent (step 18) to cross-check generated output.
    """
    try:
        shape = resolve(bend_type, legs)
    except ShapeResolutionError as exc:
        return False, str(exc)
    computed = shape.cutting_length(legs)
    if computed != stated_length:
        return False, (
            f"legs {'+'.join(shape.leg_columns)} sum to {computed}, "
            f"stated cutting length is {stated_length}"
        )
    return True, f"{'+'.join(shape.leg_columns)} = {computed}"


def infer_from_sum(legs: dict[str, int], stated_length: int) -> tuple[str, ...] | None:
    """Find a contiguous run of columns summing to ``stated_length``.

    Diagnostic only. Useful when ingesting a new project's bar lists to
    propose catalogue entries for review — never used in the forward
    calculation path, because it requires a length you do not yet have.
    """
    order = [c for c in LEG_COLUMNS if c in legs]
    for n in range(len(order), 0, -1):
        for start in range(0, len(order) - n + 1):
            window = order[start:start + n]
            if sum(legs[c] for c in window) == stated_length:
                return tuple(window)
    return None
