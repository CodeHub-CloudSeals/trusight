"""Evidence Fabric: append-only, hash-chained claim records.

Spec section 10. Granularity is per claim, not per report: a released BBS
line must be reachable back through calculation -> rule -> interpretation ->
extraction -> sheet region. If any link is absent the line cannot be
released.

Corrections are new records referencing the superseded record_id. Nothing is
ever updated in place, so a gap in the prev_hash chain is detectable.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from ..models.core import Decision, GateVector, SheetRef

GENESIS = "0" * 64


class ClaimType(str, Enum):
    EXTRACTION = "extraction"
    LINK = "link"
    INTERPRETATION = "interpretation"
    RULE_APPLICATION = "rule_application"
    CALCULATION = "calculation"
    APPROVAL = "approval"
    CORRECTION = "correction"


class ReleaseState(str, Enum):
    RELEASED = "released"
    REVIEW = "review"
    BLOCK = "block"


class ReleaseDecision(BaseModel):
    state: ReleaseState
    reason: str
    missing_claims: list[str] = Field(default_factory=list)

    @property
    def releasable(self) -> bool:
        return self.state is ReleaseState.RELEASED


class EvidenceRecord(BaseModel):
    record_id: str = ""
    prev_hash: str = GENESIS
    run_id: str
    claim_type: ClaimType
    subject: str
    value: Any = None
    source: SheetRef | None = None
    produced_by: str | None = None
    playbook_ver: str | None = None
    rulebook_ver: str | None = None
    rulebook_approved: bool = False
    gates: GateVector | None = None
    approver: str | None = None
    rationale: str | None = None
    supersedes: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def content_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude={"record_id"})
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()


class EvidenceChain:
    """In-process chain. The AWS deployment persists each record to DynamoDB."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.records: list[EvidenceRecord] = []

    @property
    def head(self) -> str:
        return self.records[-1].record_id if self.records else GENESIS

    def append(self, **kwargs: Any) -> EvidenceRecord:
        rec = EvidenceRecord(run_id=self.run_id, prev_hash=self.head, **kwargs)
        rec.record_id = rec.content_hash()
        self.records.append(rec)
        return rec

    def correct(self, superseded_id: str, **kwargs: Any) -> EvidenceRecord:
        return self.append(
            claim_type=ClaimType.CORRECTION, supersedes=superseded_id, **kwargs
        )

    def verify(self) -> tuple[bool, str]:
        """Recompute the chain. Any break is detectable."""
        prev = GENESIS
        for i, rec in enumerate(self.records):
            if rec.prev_hash != prev:
                return False, f"chain break at record {i}: prev_hash mismatch"
            if rec.record_id != rec.content_hash():
                return False, f"record {i} content does not match its id"
            prev = rec.record_id
        return True, f"{len(self.records)} records verified"

    def trace(self, subject: str) -> list[EvidenceRecord]:
        """Every claim touching one subject, oldest first."""
        return [r for r in self.records if r.subject == subject]

    #: claim types every released figure must carry
    REQUIRED = (
        ClaimType.EXTRACTION,
        ClaimType.INTERPRETATION,
        ClaimType.RULE_APPLICATION,
        ClaimType.CALCULATION,
    )

    def release_status(self, subject: str, *,
                       rulebook_approved: bool | None = None) -> ReleaseDecision:
        """Whether one claim may be released.

        Having a calculation record is not sufficient. A calculated figure
        whose gate said REVIEW, or which rests on an unapproved rulebook, is
        not releasable until a human approval record exists *after* the
        calculation. "Calculated" and "releasable" are different questions
        and conflating them is how an unapproved number reaches a tender.
        """
        valid, msg = self.verify()
        if not valid:
            return ReleaseDecision(state=ReleaseState.BLOCK,
                                   reason=f"evidence chain invalid: {msg}")

        claims = self.trace(subject)
        if not claims:
            return ReleaseDecision(state=ReleaseState.BLOCK,
                                   reason="no evidence for this subject",
                                   missing_claims=[c.value for c in self.REQUIRED])

        kinds = {c.claim_type for c in claims}
        missing = sorted(k.value for k in set(self.REQUIRED) - kinds)
        if missing:
            return ReleaseDecision(
                state=ReleaseState.BLOCK,
                reason=f"incomplete evidence chain: missing {', '.join(missing)}",
                missing_claims=missing,
            )

        # a correction with no later calculation invalidates the release
        last_correction = _last_index(claims, ClaimType.CORRECTION)
        last_calculation = _last_index(claims, ClaimType.CALCULATION)
        if last_correction is not None and last_correction > last_calculation:
            return ReleaseDecision(
                state=ReleaseState.BLOCK,
                reason="a correction supersedes the calculation; recalculate",
            )

        gated = [c for c in claims if c.gates is not None]
        gate = gated[-1].gates if gated else None
        if gate is not None:
            decision = gate.decide()
            if decision in (Decision.BLOCK, Decision.CLARIFY):
                return ReleaseDecision(state=ReleaseState.BLOCK,
                                       reason=f"unresolved gate: {gate.explain()}")
            if decision is Decision.REVIEW and not self._approved_after_calculation(claims):
                return ReleaseDecision(
                    state=ReleaseState.REVIEW,
                    reason=f"human approval required: {gate.explain()}",
                )

        approved = rulebook_approved
        if approved is None:
            rule_claims = [c for c in claims
                           if c.claim_type is ClaimType.RULE_APPLICATION]
            approved = bool(rule_claims and rule_claims[-1].rulebook_approved)
        if not approved and not self._approved_after_calculation(claims):
            return ReleaseDecision(state=ReleaseState.REVIEW,
                                   reason="rulebook not approved by an engineer")

        return ReleaseDecision(state=ReleaseState.RELEASED,
                               reason="all release conditions satisfied")

    def _approved_after_calculation(self, claims: list["EvidenceRecord"]) -> bool:
        calc = _last_index(claims, ClaimType.CALCULATION)
        appr = _last_index(claims, ClaimType.APPROVAL)
        return appr is not None and calc is not None and appr > calc

    def is_releasable(self, subject: str) -> tuple[bool, list[str]]:
        """Backwards-compatible view over :meth:`release_status`."""
        decision = self.release_status(subject)
        return decision.releasable, decision.missing_claims


def _last_index(claims: list["EvidenceRecord"], kind: ClaimType) -> int | None:
    found = [i for i, c in enumerate(claims) if c.claim_type is kind]
    return found[-1] if found else None
