"""Approved project knowledge.

A human answer becomes reusable only through this store, and only with a
scope predicate, an approver and a rationale. Subsequent matching elements
retrieve the approved value instead of asking the same question again.

This is knowledge accumulation, not self-training. Fine-tuning is a later
option once enough approved examples exist; calling this "learning" to a
client overstates what is happening.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class Scope(BaseModel):
    """Where an approved answer applies."""

    project_id: str
    element_type: str | None = None
    mark: str | None = None
    role: str | None = None

    def matches(self, *, project_id: str, element_type: str,
                mark: str | None, role: str | None) -> bool:
        if self.project_id != project_id:
            return False
        for mine, theirs in (
            (self.element_type, element_type),
            (self.mark, mark),
            (self.role, role),
        ):
            if mine is not None and mine != theirs:
                return False
        return True

    def describe(self) -> str:
        parts = [f"project={self.project_id}"]
        for name in ("element_type", "mark", "role"):
            v = getattr(self, name)
            if v:
                parts.append(f"{name}={v}")
        return " AND ".join(parts)


class ApprovedFact(BaseModel):
    field: str
    value: object
    scope: Scope
    approver: str
    rationale: str
    version: int = 1
    approved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ProjectKnowledge:
    """Versioned store of human-approved project facts."""

    def __init__(self, project_id: str):
        self.project_id = project_id
        self._facts: list[ApprovedFact] = []
        self.reuse_count = 0

    def approve(self, field: str, value: object, scope: Scope,
                approver: str, rationale: str) -> ApprovedFact:
        prior = [f for f in self._facts if f.field == field
                 and f.scope.describe() == scope.describe()]
        fact = ApprovedFact(
            field=field, value=value, scope=scope, approver=approver,
            rationale=rationale, version=len(prior) + 1,
        )
        self._facts.append(fact)
        return fact

    def all(self) -> list[ApprovedFact]:
        """Every approved fact, newest last. Read-only view for reporting."""
        return list(self._facts)

    def lookup(self, field: str, *, element_type: str, mark: str | None = None,
               role: str | None = None) -> ApprovedFact | None:
        matches = [
            f for f in self._facts
            if f.field == field and f.scope.matches(
                project_id=self.project_id, element_type=element_type,
                mark=mark, role=role,
            )
        ]
        if not matches:
            return None
        self.reuse_count += 1
        # most specific scope wins, then latest version
        return max(
            matches,
            key=lambda f: (
                sum(1 for n in ("element_type", "mark", "role")
                    if getattr(f.scope, n) is not None),
                f.version,
            ),
        )

    @property
    def facts(self) -> list[ApprovedFact]:
        return list(self._facts)
