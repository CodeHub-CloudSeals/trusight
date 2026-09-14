"""Construction control mapping.

TrustSight maps requirements, controls, evidence and gaps. It does not
declare that a design or construction is legally or completely compliant;
accountable engineers and authorities retain that judgement. Every result
carries evidence and a rationale, so a PASS is checkable rather than
asserted.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from ..evidence.fabric import EvidenceChain, ReleaseState
from ..models.core import BarSchedule, Element, SheetRef
from .rulebook import RuleNotFound, Rulebook


class ControlStatus(str, Enum):
    PASS = "pass"
    WARNING = "warning"
    MISSING = "missing"
    REVIEW = "review"
    NOT_APPLICABLE = "not_applicable"


class ControlResult(BaseModel):
    control_id: str
    requirement: str
    status: ControlStatus
    evidence: list[SheetRef] = Field(default_factory=list)
    rulebook_ver: str | None = None
    rationale: str


def evaluate(
    elements: list[Element],
    schedule: BarSchedule,
    rulebook: Rulebook,
    chain: EvidenceChain | None = None,
) -> list[ControlResult]:
    """Run the demo control pack. Each result must justify itself."""
    results: list[ControlResult] = []
    sheets = [s for e in elements for s in e.sheets]

    # C-001 cover established
    needs_cover = [
        (e, r) for e in elements for r in e.reinforcement if r.requires_cover()
    ]
    if not needs_cover:
        results.append(ControlResult(
            control_id="C-001", requirement="Concrete cover established",
            status=ControlStatus.NOT_APPLICABLE, rulebook_ver=rulebook.version,
            rationale="no reinforcement in scope is dimensioned from the concrete face",
        ))
    else:
        unresolved = []
        for e, r in needs_cover:
            if r.cover_mm is not None:
                continue
            try:
                rulebook.resolve_cover(e.element_type.value, r.role.value, r.cover_condition)
            except RuleNotFound:
                unresolved.append(f"{e.identity.key()} [{r.role.value}]")
        results.append(ControlResult(
            control_id="C-001", requirement="Concrete cover established",
            status=ControlStatus.PASS if not unresolved else ControlStatus.MISSING,
            evidence=sheets[:3], rulebook_ver=rulebook.version,
            rationale=("cover supplied by drawing or approved rule for every item"
                       if not unresolved
                       else f"no cover on drawing and no approved rule: {', '.join(unresolved)}"),
        ))

    # C-002 spacing / count basis
    no_basis = [e.identity.key() for e in elements if e.instance_basis is None]
    conflicts = [x for x in schedule.exceptions if "disagrees with spacing-derived" in x]
    if conflicts:
        status, why = ControlStatus.REVIEW, f"count basis disputed: {conflicts[0]}"
    elif no_basis:
        status, why = ControlStatus.MISSING, f"no recorded counting basis: {', '.join(no_basis)}"
    else:
        status, why = ControlStatus.PASS, "every quantity has a recorded and internally consistent basis"
    results.append(ControlResult(
        control_id="C-002", requirement="Reinforcement spacing and count basis established",
        status=status, evidence=sheets[:3], rulebook_ver=rulebook.version, rationale=why,
    ))

    # C-003 lap / anchorage rule available
    try:
        rulebook.stock_length()
        lap_ok = bool(rulebook.lap_multiplier)
    except RuleNotFound:
        lap_ok = False
    results.append(ControlResult(
        control_id="C-003", requirement="Lap, splice and anchorage rules available",
        status=ControlStatus.PASS if lap_ok and rulebook.is_approved() else ControlStatus.REVIEW,
        rulebook_ver=rulebook.version,
        rationale=("lap rules and stock length present in an approved rulebook"
                   if lap_ok and rulebook.is_approved()
                   else "lap rules absent or rulebook not approved by an engineer"),
    ))

    # C-004 revision status
    revisions = {s.sheet_no for s in sheets if s.sheet_no}
    results.append(ControlResult(
        control_id="C-004", requirement="Revision status current and non-conflicting",
        status=ControlStatus.PASS if revisions else ControlStatus.MISSING,
        evidence=sheets[:3], rulebook_ver=rulebook.version,
        rationale=(f"sheets identified: {', '.join(sorted(revisions))}" if revisions
                   else "no sheet or revision identifiers captured"),
    ))

    # C-005 calculation reproducibility
    versioned = rulebook.is_approved() and bool(schedule.items)
    results.append(ControlResult(
        control_id="C-005", requirement="Calculation reproducible from versioned inputs",
        status=ControlStatus.PASS if versioned else ControlStatus.REVIEW,
        rulebook_ver=rulebook.version,
        rationale=("quantities produced by the deterministic engine against an "
                   "approved, versioned rulebook" if versioned
                   else "rulebook unapproved or no quantities produced"),
    ))

    # C-006 evidence lineage
    if chain is None:
        status, why = ControlStatus.MISSING, "no evidence chain supplied"
    else:
        valid, msg = chain.verify()
        # evidence is recorded against the element key, so trace that
        subjects = {i.element_key or "" for i in schedule.items if i.element_key}
        blocked = [
            s for s in subjects
            if chain.release_status(s).state is ReleaseState.BLOCK
        ]
        if not valid:
            status, why = ControlStatus.MISSING, f"chain invalid: {msg}"
        elif blocked:
            status, why = ControlStatus.REVIEW, f"{len(blocked)} item(s) lack a complete chain"
        else:
            status, why = ControlStatus.PASS, f"chain verified: {msg}"
    results.append(ControlResult(
        control_id="C-006", requirement="Evidence lineage complete and verifiable",
        status=status, rulebook_ver=rulebook.version, rationale=why,
    ))

    return results
