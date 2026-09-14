"""Canonical data model. See Engineering Specification section 2.

Invariant I2: an unresolved fact is never given a default value. A field that
is ``None`` because the drawing does not state it is recorded in
``unknown_fields`` so that "the source is silent" can never be mistaken for
"we have not looked yet".
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------
class BarSize(str, Enum):
    M10 = "10M"
    M15 = "15M"
    M20 = "20M"
    M25 = "25M"
    M30 = "30M"
    M35 = "35M"

    @property
    def unit_mass_kg_per_m(self) -> float:
        return UNIT_MASS[self]


#: CSA G30.18 nominal masses (kg/m). Verified against all five supplied bar lists.
UNIT_MASS: dict[BarSize, float] = {
    BarSize.M10: 0.785,
    BarSize.M15: 1.570,
    BarSize.M20: 2.355,
    BarSize.M25: 3.925,
    BarSize.M30: 5.495,
    BarSize.M35: 7.850,
}

#: Bar-list leg columns, in sheet order. Note there is no "I" column.
LEG_COLUMNS = "ABCDEFGHJKOR"


class ElementType(str, Enum):
    PILE = "pile"
    PILE_CAP = "pile_cap"
    FOOTING = "footing"
    WALL = "wall"
    BEAM = "beam"
    COLUMN = "column"
    PIER = "pier"
    SLAB = "slab"
    GRADE_BEAM = "grade_beam"
    ABUTMENT = "abutment"  # Project 4 is a bridge abutment (spec D10)


class RebarRole(str, Enum):
    LONGITUDINAL = "longitudinal"
    TIE = "tie"
    STIRRUP = "stirrup"
    SPIRAL = "spiral"
    TOP = "top"
    BOTTOM = "bottom"
    EACH_WAY = "each_way"
    DOWEL = "dowel"


class SourceTier(str, Enum):
    """Extraction tier. A governance input, not just a diagnostic (spec s8)."""

    NATIVE_TEXT = "native_text"
    VECTOR_PATH = "vector_path"
    VISION = "vision"
    OCR = "ocr"

    @property
    def auto_proceed_allowed(self) -> bool:
        return self in (SourceTier.NATIVE_TEXT, SourceTier.VECTOR_PATH)


class CountingPattern(str, Enum):
    """How ``instances`` was derived. Spec section 3.3."""

    SCHEDULE_QTY = "schedule_qty"
    PLAN_INSTANCE_COUNT = "plan_instance_count"
    AREA_DIVIDED_BY_SPACING = "area_divided_by_spacing"
    LENGTH_DIVIDED_BY_SPACING = "length_divided_by_spacing"
    EXPLICIT_CALLOUT = "explicit_callout"

    @property
    def always_review(self) -> bool:
        """Area-derived counts always route to a human during the POC."""
        return self is CountingPattern.AREA_DIVIDED_BY_SPACING


class ItemState(str, Enum):
    PROPOSED = "proposed"
    RESOLVED = "resolved"
    UNKNOWN = "unknown"
    CONFLICTED = "conflicted"
    APPROVED = "approved"


class Decision(str, Enum):
    AUTO_PROCEED = "auto_proceed"
    REVIEW = "review"
    CLARIFY = "clarify"
    BLOCK = "block"


# --------------------------------------------------------------------------
# References and provenance
# --------------------------------------------------------------------------
class SheetRef(BaseModel):
    document_id: str
    page: int = 1
    sheet_no: str | None = None  # e.g. "S101", "R01", "C-01"
    bbox: tuple[float, float, float, float] | None = None

    def cite(self) -> str:
        s = self.sheet_no or f"{self.document_id}:p{self.page}"
        return f"{s}@{self.bbox}" if self.bbox else s


class Provenance(BaseModel):
    """Why a derived count holds. Mandatory on any counted quantity."""

    pattern: CountingPattern
    evidence: list[SheetRef] = Field(default_factory=list)
    detail: str | None = None


# --------------------------------------------------------------------------
# Gate vector (spec section 5) — deliberately NOT a blended score
# --------------------------------------------------------------------------
class GateVector(BaseModel):
    g1_fields_complete: bool = False
    g2_no_conflict: bool = True
    g3_rule_resolved: bool = False
    g4_source_quality: SourceTier = SourceTier.OCR
    g5_pattern_known: bool = False
    g6_within_bounds: bool = True

    #: forced by a counting pattern regardless of the other gates
    force_review: bool = False
    notes: list[str] = Field(default_factory=list)

    def failures(self) -> list[str]:
        out: list[str] = []
        if not self.g1_fields_complete:
            out.append("g1_fields_complete")
        if not self.g2_no_conflict:
            out.append("g2_no_conflict")
        if not self.g3_rule_resolved:
            out.append("g3_rule_resolved")
        if not self.g4_source_quality.auto_proceed_allowed:
            out.append("g4_source_quality")
        if not self.g5_pattern_known:
            out.append("g5_pattern_known")
        if not self.g6_within_bounds:
            out.append("g6_within_bounds")
        return out

    def decide(self) -> Decision:
        """Policy is a readable statement, not a threshold."""
        if not self.g2_no_conflict:
            return Decision.BLOCK  # a conflict is never auto-resolved
        if not self.g1_fields_complete:
            return Decision.CLARIFY  # the missing field IS the question
        if self.force_review or self.failures():
            return Decision.REVIEW
        return Decision.AUTO_PROCEED

    def explain(self) -> str:
        d = self.decide()
        if d is Decision.AUTO_PROCEED:
            return "All gates passed from a native-text or vector source."
        reasons = self.failures() or ["counting pattern forces human review"]
        return f"{d.value}: {', '.join(reasons)}"


# --------------------------------------------------------------------------
# Element identity and geometry (spec section 3)
# --------------------------------------------------------------------------
class ElementIdentity(BaseModel):
    project_id: str
    element_type: ElementType
    mark: str | None = None
    storey: str | None = None
    grid_ref: str | None = None

    model_config = {"frozen": True}

    @property
    def is_provisional(self) -> bool:
        """No mark and no grid reference -> never merges automatically."""
        return not self.mark and not self.grid_ref

    def key(self) -> str:
        if self.mark:
            return f"{self.project_id}|{self.element_type.value}|{self.mark.upper()}"
        return (
            f"{self.project_id}|{self.element_type.value}"
            f"|{self.storey or '-'}|{self.grid_ref or '-'}"
        )


class Placement(BaseModel):
    x: float
    y: float
    z: float = 0.0
    rotation_deg: float = 0.0
    sheet: SheetRef | None = None
    #: True when the coordinates are a legible arrangement rather than a
    #: position read from the drawing. A schedule gives a count, not plan
    #: coordinates; spacing those elements evenly is a drawing convenience.
    #: Anything that displays geometry must say which of the two it has —
    #: a laid-out row of piles looks exactly like a surveyed one.
    schematic: bool = False


class Geometry(BaseModel):
    primitive: Literal["cylinder", "box", "extrusion", "swept_section"] | None = None
    params: dict[str, float] = Field(default_factory=dict)
    placements: list[Placement] = Field(default_factory=list)
    source: SourceTier = SourceTier.NATIVE_TEXT


class HookSpec(BaseModel):
    kind: str  # "90", "180", "seismic"
    length_mm: int | None = None


class Reinforcement(BaseModel):
    #: stable identity for evidence and release decisions at item level
    claim_id: str = Field(default_factory=lambda: uuid4().hex[:12])
    #: which cover rule applies, e.g. "pile_external", "slab_top".
    #: Never inferred from element type inside the calculator.
    cover_condition: str | None = None
    bar_size: BarSize
    role: RebarRole
    count: int | None = None  # None when spacing-driven
    spacing_mm: int | None = None  # None when count-driven
    run_length_mm: int | None = None  # for spacing-driven counts
    direction: str | None = None
    layer: str | None = None
    cover_mm: int | None = None  # None => NOT stated. Never defaulted here.
    lap_mm: int | None = None
    hook: HookSpec | None = None
    shape_code: str | None = None
    bend_type: str | None = None
    legs: dict[str, int] = Field(default_factory=dict)
    source: list[SheetRef] = Field(default_factory=list)
    source_tier: SourceTier = SourceTier.NATIVE_TEXT
    state: ItemState = ItemState.PROPOSED
    unknown_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _record_unknowns(self) -> "Reinforcement":
        missing = list(self.unknown_fields)
        if self.count is None and self.spacing_mm is None:
            missing.append("count_or_spacing")
        if self.cover_mm is None and self._cover_required() and not self.cover_condition:
            missing.append("cover_mm")
        self.unknown_fields = sorted(set(missing))
        return self

    def _cover_required(self) -> bool:
        """Cover is needed for any bar whose cutting length depends on it.

        A tie or spiral is dimensioned from the concrete face inward, so an
        unstated cover makes its cutting length underivable. Reporting it here
        is what turns a silent default into a question (invariant I2).
        """
        return self.role in (
            RebarRole.TIE,
            RebarRole.STIRRUP,
            RebarRole.SPIRAL,
            RebarRole.EACH_WAY,
        )

    def requires_cover(self) -> bool:
        return self._cover_required()


class Element(BaseModel):
    identity: ElementIdentity
    element_type: ElementType
    mark: str | None = None
    storey: str | None = None
    geometry: Geometry = Field(default_factory=Geometry)
    reinforcement: list[Reinforcement] = Field(default_factory=list)
    instances: int = 1
    instance_basis: Provenance | None = None  # mandatory before calculation
    sheets: list[SheetRef] = Field(default_factory=list)
    state: ItemState = ItemState.PROPOSED
    conflicts: list[str] = Field(default_factory=list)

    def element_blockers(self) -> list[str]:
        """Conditions that stop the whole element.

        Deliberately narrow: an element-wide blocker means no bar in this
        element can be trusted. A missing field on one tie is an item
        blocker, not an element blocker, otherwise one unresolved spiral
        would suppress perfectly good longitudinal bars.
        """
        problems: list[str] = []
        if self.instance_basis is None:
            problems.append("instance_basis missing: the count has no recorded basis")
        if self.state is ItemState.CONFLICTED or self.conflicts:
            problems.append(f"unresolved conflicts: {'; '.join(self.conflicts)}")
        if not self.reinforcement:
            problems.append("no reinforcement interpreted")
        return problems

    def reinforcement_blockers(self, r: Reinforcement) -> list[str]:
        """Conditions that stop one reinforcement item only."""
        return list(r.unknown_fields)

    def ready_for_calculation(self) -> tuple[bool, list[str]]:
        """Retained for callers that want the element-wide view."""
        problems = self.element_blockers()
        return (not problems, problems)


# --------------------------------------------------------------------------
# Output type (spec section 2.1) — mirrors the RebarCAD bar list schema
# --------------------------------------------------------------------------
class BarItem(BaseModel):
    item_no: int
    quantity: int
    size: BarSize
    cutting_length_mm: int
    mark: str | None = None
    bend_type: str | None = None
    legs: dict[str, int] = Field(default_factory=dict)

    element_key: str | None = None
    claim_id: str | None = None
    gates: GateVector | None = None
    evidence: list[SheetRef] = Field(default_factory=list)

    @property
    def unit_mass(self) -> float:
        return UNIT_MASS[self.size]

    @property
    def total_mass_kg(self) -> float:
        return self.quantity * (self.cutting_length_mm / 1000.0) * self.unit_mass

    @property
    def is_straight(self) -> bool:
        return not self.bend_type and not self.mark


class BarSchedule(BaseModel):
    project_id: str
    items: list[BarItem] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def total_mass_kg(self) -> float:
        return sum(i.total_mass_kg for i in self.items)

    def by_size(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for i in self.items:
            out[i.size.value] = out.get(i.size.value, 0.0) + i.total_mass_kg
        return out
