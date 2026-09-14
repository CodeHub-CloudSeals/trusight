"""Project knowledge graph: element identity, merge and de-duplication.

Spec section 3. This is where the highest-risk correctness problem lives:
the same physical element read from a plan, a section and a detail must be
counted once, and an element that exists must not be missed.

Two independent checks run here:
  * identity merge  - same key, union the sheet references
  * spatial dedup   - same type at the same coordinates, flag for review

Conflicts are preserved, never resolved by precedence and never by
preferring the higher-confidence value (invariant I2).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..models.core import (
    Element,
    ElementIdentity,
    ItemState,
    Placement,
    SheetRef,
)

#: mm. Two same-type elements closer than this are a suspected duplicate.
DEFAULT_DEDUP_TOLERANCE_MM = 50.0  # spec D7


@dataclass
class GraphEdge:
    src: str
    dst: str
    kind: str            # appears_on | references | detail_of | schedule_for
    evidence: SheetRef | None = None


@dataclass
class ProjectKnowledgeGraph:
    project_id: str
    elements: dict[str, Element] = field(default_factory=dict)
    edges: list[GraphEdge] = field(default_factory=list)
    provisional: list[Element] = field(default_factory=list)
    duplicate_flags: list[tuple[str, str, float]] = field(default_factory=list)

    # -- ingestion --------------------------------------------------------
    def add(self, element: Element) -> str:
        """Insert or merge an element. Returns its key."""
        identity = element.identity
        if identity.is_provisional:
            # No mark and no grid reference: never merge automatically.
            element.state = ItemState.UNKNOWN
            element.conflicts.append(
                "provisional identity: no mark or grid reference; needs human resolution"
            )
            self.provisional.append(element)
            return f"provisional:{len(self.provisional) - 1}"

        key = identity.key()
        if key not in self.elements:
            self.elements[key] = element
        else:
            self._merge(self.elements[key], element)

        for sheet in element.sheets:
            self.edges.append(
                GraphEdge(src=key, dst=sheet.cite(), kind="appears_on", evidence=sheet)
            )
        return key

    def _merge(self, into: Element, other: Element) -> None:
        """Union sheet references; preserve every disagreement."""
        seen = {s.cite() for s in into.sheets}
        into.sheets.extend(s for s in other.sheets if s.cite() not in seen)

        for field_name in ("element_type", "storey"):
            a, b = getattr(into, field_name), getattr(other, field_name)
            if a is not None and b is not None and a != b:
                into.conflicts.append(f"{field_name}: {a!r} vs {b!r}")

        if into.instances != other.instances:
            a_src = into.instance_basis.pattern.value if into.instance_basis else "?"
            b_src = other.instance_basis.pattern.value if other.instance_basis else "?"
            into.conflicts.append(
                f"instances: {into.instances} (via {a_src}) vs "
                f"{other.instances} (via {b_src})"
            )

        existing = {(r.bar_size, r.role, r.spacing_mm, r.count) for r in into.reinforcement}
        for r in other.reinforcement:
            if (r.bar_size, r.role, r.spacing_mm, r.count) not in existing:
                into.reinforcement.append(r)

        if not into.geometry.placements and other.geometry.placements:
            into.geometry = other.geometry

        if into.conflicts:
            into.state = ItemState.CONFLICTED

    # -- spatial checks ---------------------------------------------------
    def find_spatial_duplicates(
        self, tolerance_mm: float = DEFAULT_DEDUP_TOLERANCE_MM
    ) -> list[tuple[str, str, float]]:
        """Same-type elements at effectively the same coordinates."""
        flags: list[tuple[str, str, float]] = []
        keys = list(self.elements)
        for i, ka in enumerate(keys):
            a = self.elements[ka]
            for kb in keys[i + 1:]:
                b = self.elements[kb]
                if a.element_type is not b.element_type:
                    continue
                for pa in a.geometry.placements:
                    for pb in b.geometry.placements:
                        d = _distance(pa, pb)
                        if d <= tolerance_mm:
                            flags.append((ka, kb, d))
                            break
                    else:
                        continue
                    break
        self.duplicate_flags = flags
        return flags

    def completeness_report(self) -> dict[str, list[str]]:
        """Gaps the graph can detect on its own. Feeds the 3D view (spec s9.2)."""
        report: dict[str, list[str]] = {
            "no_placement": [],
            "single_sheet_only": [],
            "conflicted": [],
            "unknown_reinforcement": [],
            "provisional_identity": [f"{e.element_type.value}" for e in self.provisional],
        }
        for key, el in self.elements.items():
            if not el.geometry.placements:
                report["no_placement"].append(key)
            if len(el.sheets) <= 1:
                report["single_sheet_only"].append(key)
            if el.conflicts:
                report["conflicted"].append(f"{key}: {'; '.join(el.conflicts)}")
            for r in el.reinforcement:
                if r.unknown_fields:
                    report["unknown_reinforcement"].append(
                        f"{key} [{r.role.value}]: {', '.join(r.unknown_fields)}"
                    )
        return report

    def stats(self) -> dict[str, int]:
        return {
            "elements": len(self.elements),
            "edges": len(self.edges),
            "provisional": len(self.provisional),
            "conflicted": sum(1 for e in self.elements.values() if e.conflicts),
            "duplicate_flags": len(self.duplicate_flags),
        }


def _distance(a: Placement, b: Placement) -> float:
    return math.dist((a.x, a.y, a.z), (b.x, b.y, b.z))
