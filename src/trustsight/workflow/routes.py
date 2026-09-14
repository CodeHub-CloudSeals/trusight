"""Execution routes. Spec section 3.1.

The same business goal — estimate the rebar for these elements — is reached
three different ways depending on how much the drawing set actually states.
The destination does not change; the sequence does.

This module answers one question: which of the 23 steps does *this* route
genuinely depend on. That matters twice over.

* Strict execution needs it. Requiring all 23 forces either a permanently
  failing run or a row of stub handlers that report success while doing
  nothing, and the second is worse than skipping openly.
* The timeline needs it. "Not run" is three different facts — does not
  apply here, not implemented yet, or should have run and did not — and a
  client reading one label for all three cannot tell a deliberate design
  from a gap.

Steps that are in a route but not yet implemented still show as
``not_implemented`` rather than being quietly dropped from the picture.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: Every route runs these: take the drawings in, read them, build the graph,
#: check what is missing, apply the rulebook, calculate, gate, release.
_SPINE = (
    "receive_drawings",
    "preflight",
    "playbook_retrieval",
    "extraction",
    "knowledge_graph",
    "missing_conflict_check",
    "rulebook",
    "deterministic_calculation",
    "qa",
    "approval_gate",
    "generate_bbs",
)


@dataclass(frozen=True)
class Route:
    name: str
    summary: str
    #: steps this route depends on, in step order
    steps: tuple[str, ...]
    #: what the route is claiming about the input
    input_quality: str
    expected_human: str
    #: steps deliberately not taken, with the reason shown in the timeline
    excluded: dict[str, str] = field(default_factory=dict)
    #: False when the route depends on a capability this build does not have.
    #: Refusing the route is honest; starting a run that fails halfway is not.
    available: bool = True
    unavailable_reason: str = ""

    def requires(self) -> set[str]:
        return set(self.steps)


STRUCTURED = Route(
    name="structured",
    summary="Extract, validate, calculate.",
    input_quality="native CAD or vector tables the drawing states outright",
    expected_human="confirm the rulebook; little or no clarification",
    steps=_SPINE,
    excluded={
        "sheet_classification": "the sheet type is stated in the title block",
        "element_detection": "elements come from the schedule, not from vision",
        "cross_sheet_resolution": "the facts are on one sheet",
        "reinforcement_interpretation":
            "native fields map deterministically; no model is called",
        "clarification": "nothing was left unstated",
        "human_input": "no clarification was raised",
    },
)

SEMI_STRUCTURED = Route(
    name="semi_structured",
    summary="Extract, correlate, interpret, calculate.",
    input_quality="facts spread across plan, section, detail, schedule and notes",
    expected_human="answer what the set genuinely does not state",
    steps=_SPINE + ("cross_sheet_resolution", "clarification",
                    "approved_knowledge"),
    excluded={
        "element_detection": "a text layer is present, so vision is not used",
        "sheet_classification": "sheet numbers resolve from the title block",
        "reinforcement_interpretation":
            "no model is configured in this build; the facts came from the "
            "deterministic playbook, and saying otherwise would credit a "
            "model that never ran",
        "human_input":
            "clarifications are surfaced without parking the whole run, so a "
            "resolved claim still releases while another waits",
    },
)

UNSTRUCTURED = Route(
    name="unstructured",
    summary="Interpret, resolve, clarify, calculate.",
    input_quality="scans, inconsistent notation, incomplete or mixed conventions",
    expected_human="more clarification, and more of it blocking",
    steps=_SPINE + ("sheet_classification", "element_detection",
                    "cross_sheet_resolution", "reinforcement_interpretation",
                    "clarification", "approved_knowledge"),
    excluded={
        "human_input":
            "clarifications are surfaced without parking the whole run",
    },
    available=False,
    unavailable_reason=(
        "this route depends on sheet classification, element detection and "
        "model-based reinforcement interpretation. None is implemented in "
        "this build, so the route is refused rather than started and failed "
        "half way through."
    ),
)

ROUTES: dict[str, Route] = {r.name: r for r in
                            (STRUCTURED, SEMI_STRUCTURED, UNSTRUCTURED)}

DEFAULT = SEMI_STRUCTURED


def get(name: str | None) -> Route:
    """Resolve a route by name, falling back to the dominant real-world case."""
    return ROUTES.get((name or "").strip(), DEFAULT)


def describe(route: Route) -> dict[str, object]:
    return {
        "route": route.name,
        "summary": route.summary,
        "input_quality": route.input_quality,
        "expected_human": route.expected_human,
        "steps": list(route.steps),
        "excluded": route.excluded,
    }
