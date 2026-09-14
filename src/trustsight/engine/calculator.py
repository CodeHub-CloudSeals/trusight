"""Deterministic rebar calculation engine.

Invariant I1: no language model participates in this module. Given the same
validated inputs and the same rulebook version it returns the same numbers.

Partial completion is the normal outcome, not an error. Element-wide
blockers stop an element; an item blocker stops only that reinforcement
item, so 72 resolved longitudinal bars still release while one unresolved
spiral goes to clarification.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..models.core import (
    BarItem,
    BarSchedule,
    Element,
    GateVector,
    Reinforcement,
)
from ..shapes.catalogue import ShapeResolutionError, resolve
from .rulebook import Rulebook, RuleNotFound


class QuantityConflict(RuntimeError):
    """An explicit count disagrees with the spacing-derived count."""


@dataclass
class ItemException:
    element_key: str
    claim_id: str | None
    role: str
    reasons: list[str]

    def __str__(self) -> str:
        return f"{self.element_key} [{self.role}]: {'; '.join(self.reasons)}"


def derive_spacing_count(run_length_mm: int, spacing_mm: int, convention: str) -> int:
    n = run_length_mm / spacing_mm
    if convention == "floor":
        return math.floor(n)
    if convention == "ceil":
        return math.ceil(n)
    if convention == "floor_plus_one":
        return math.floor(n) + 1
    raise RuleNotFound(f"unknown spacing convention {convention!r}")


class RebarCalculator:
    """Turns validated ``Element`` objects into a ``BarSchedule``."""

    def __init__(self, rulebook: Rulebook):
        self.rulebook = rulebook

    # -- counting ---------------------------------------------------------
    def bar_count(self, r: Reinforcement, instances: int) -> int:
        """Bars for one reinforcement entry across all element instances.

        When both an explicit count and spacing metadata are present they
        must agree. A silent preference for the explicit count is how a
        real discrepancy reaches a tender undetected.
        """
        if r.count is not None and r.spacing_mm and r.run_length_mm:
            derived = derive_spacing_count(
                r.run_length_mm, r.spacing_mm, self.rulebook.convention()
            )
            if r.count != derived:
                raise QuantityConflict(
                    f"explicit count {r.count} disagrees with spacing-derived "
                    f"{derived} ({r.run_length_mm}mm at {r.spacing_mm}mm, "
                    f"{self.rulebook.convention()}); the basis must be agreed "
                    f"with the estimator before release"
                )
        if r.count is not None:
            return r.count * instances
        if r.spacing_mm is None or r.run_length_mm is None:
            raise RuleNotFound(
                "spacing-driven count needs both spacing and run length"
            )
        per = derive_spacing_count(
            r.run_length_mm, r.spacing_mm, self.rulebook.convention()
        )
        return per * instances

    # -- cover ------------------------------------------------------------
    def resolve_cover(self, element: Element, r: Reinforcement) -> tuple[int, str] | None:
        """Resolve cover for items whose cutting length depends on it."""
        if not r.requires_cover():
            return None
        if r.cover_mm is not None:
            return r.cover_mm, "stated_on_drawing"
        return self.rulebook.resolve_cover(
            element.element_type.value, r.role.value, r.cover_condition
        )

    # -- cutting length ---------------------------------------------------
    def cutting_length(self, r: Reinforcement) -> int:
        """Cutting length in mm, out to out, with no bend deduction.

        The bar lists state the rule directly: "ALL DIMENSIONS ARE OUT TO
        OUT" and "FOR TYPICAL BEND TYPES REFER TO ACI".
        """
        if not r.legs:
            raise RuleNotFound(
                "no leg dimensions; cutting length cannot be derived without "
                "geometry or an approved shape"
            )
        if r.bend_type is None:
            # An unstated bend type is not a straight bar.
            #
            # The catalogue keys straight bars on "" because that is how the
            # bar lists encode them, and ``resolve`` maps a missing bend type
            # to the same entry. That is right when checking a supplied bar
            # list and wrong when generating one: it silently took column B
            # alone as the cutting length. A pile bar of A 510 + B 11,955
            # came out as 11,955 mm — the hook quietly dropped — and a spiral
            # of five legs came out as 140 mm. Both were then *released*,
            # which is the one outcome this product exists to prevent.
            #
            # Which columns are legs is a property of the shape, so with no
            # shape there is no sum to make. Ask.
            raise ShapeResolutionError(
                "the bend type is not stated, and the shape is what decides "
                "which legs enter the cutting length; it cannot be inferred "
                "from the leg dimensions alone"
            )
        shape = resolve(r.bend_type, r.legs)
        missing = shape.missing_legs(r.legs)
        if missing:
            raise ShapeResolutionError(
                f"shape {r.bend_type} needs legs {missing} which are absent"
            )
        raw = shape.cutting_length(r.legs)
        step = self.rulebook.rounding_mm
        return int(math.ceil(raw / step) * step) if step > 1 else int(raw)

    # -- splicing ---------------------------------------------------------
    def split_for_stock(self, length_mm: int) -> tuple[int, int]:
        stock = self.rulebook.stock_length()
        if length_mm <= stock:
            return 1, length_mm
        return math.ceil(length_mm / stock), stock

    # -- gates ------------------------------------------------------------
    def gates_for(self, element: Element, r: Reinforcement,
                  rule_key: str | None) -> GateVector:
        pattern = element.instance_basis.pattern if element.instance_basis else None
        return GateVector(
            g1_fields_complete=not element.reinforcement_blockers(r),
            g2_no_conflict=not element.conflicts,
            g3_rule_resolved=self.rulebook.is_approved() and rule_key is not None,
            g4_source_quality=r.source_tier,
            g5_pattern_known=pattern is not None,
            g6_within_bounds=True,
            force_review=bool(pattern and pattern.always_review),
            notes=[f"cover rule: {rule_key}"] if rule_key else [],
        )

    # -- entry point ------------------------------------------------------
    def calculate(self, project_id: str, elements: list[Element]) -> BarSchedule:
        items: list[BarItem] = []
        exceptions: list[str] = []
        item_no = 1

        for element in elements:
            blockers = element.element_blockers()
            if blockers:
                exceptions.append(f"{element.identity.key()}: {'; '.join(blockers)}")
                continue

            for r in element.reinforcement:
                item_blockers = element.reinforcement_blockers(r)
                if item_blockers:
                    exceptions.append(
                        str(ItemException(element.identity.key(), r.claim_id,
                                          r.role.value, item_blockers))
                    )
                    continue

                rule_key: str | None = None
                try:
                    cover = self.resolve_cover(element, r)
                    rule_key = cover[1] if cover else "not_required"
                    length = self.cutting_length(r)
                    qty = self.bar_count(r, element.instances)
                except (RuleNotFound, ShapeResolutionError, QuantityConflict) as exc:
                    exceptions.append(
                        str(ItemException(element.identity.key(), r.claim_id,
                                          r.role.value, [str(exc)]))
                    )
                    continue

                items.append(
                    BarItem(
                        item_no=item_no,
                        quantity=qty,
                        size=r.bar_size,
                        cutting_length_mm=length,
                        mark=r.shape_code,
                        bend_type=r.bend_type,
                        legs=dict(r.legs),
                        element_key=element.identity.key(),
                        claim_id=r.claim_id,
                        gates=self.gates_for(element, r, rule_key),
                        evidence=list(r.source) or list(element.sheets),
                    )
                )
                item_no += 1

        return BarSchedule(project_id=project_id, items=items, exceptions=exceptions)
