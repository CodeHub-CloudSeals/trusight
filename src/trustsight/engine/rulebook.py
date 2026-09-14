"""Engineering rulebook: versioned reference data, never code.

Spec section 4.4 and D1-D5. Every value here is a project decision that must
be approved by the responsible engineer. Nothing in this module may be used
as a silent default: ``Rulebook.get`` raises when a rule is absent so that a
missing rule becomes an exception rather than an assumption (invariant I2).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..models.core import BarSize


class RuleNotFound(LookupError):
    """No approved rule covers this case. Route to clarification."""


@dataclass(frozen=True)
class Rulebook:
    version: str
    project_id: str
    #: mm, by exposure condition. Populated from the client's assumption sheet.
    cover_mm: dict[str, int] = field(default_factory=dict)
    #: lap length as a multiple of bar diameter, by bar size
    lap_multiplier: dict[BarSize, float] = field(default_factory=dict)
    #: standard hook allowance in mm, by bar size and hook kind
    hook_mm: dict[tuple[BarSize, str], int] = field(default_factory=dict)
    stock_length_mm: int | None = None          # spec D1
    rounding_mm: int = 5                         # spec D5
    #: floor | ceil | floor_plus_one — spec D3, shifts every tie count by one
    spacing_convention: str | None = None
    approved_by: str | None = None

    def cover(self, condition: str) -> int:
        if condition not in self.cover_mm:
            raise RuleNotFound(
                f"no approved cover for condition {condition!r} in rulebook "
                f"{self.version}"
            )
        return self.cover_mm[condition]

    def resolve_cover(self, element_type: str, role: str,
                      condition: str | None) -> tuple[int, str]:
        """Return (cover_mm, rule_key) for an explicit condition only.

        There is deliberately no fallback to a generic condition. Resolving a
        pile's cover from a slab rule because no pile rule exists is exactly
        the silent substitution invariant I2 forbids.
        """
        if condition:
            if condition in self.cover_mm:
                return self.cover_mm[condition], condition
            raise RuleNotFound(
                f"cover condition {condition!r} is not in rulebook {self.version}"
            )
        key = f"{element_type}_{role}"
        if key in self.cover_mm:
            return self.cover_mm[key], key
        if element_type in self.cover_mm:
            return self.cover_mm[element_type], element_type
        raise RuleNotFound(
            f"no cover condition stated and no approved rule for "
            f"{element_type}/{role} in rulebook {self.version}"
        )

    def is_approved(self) -> bool:
        return bool(self.approved_by)

    def stock_length(self) -> int:
        if self.stock_length_mm is None:
            raise RuleNotFound(f"stock length not set in rulebook {self.version}")
        return self.stock_length_mm

    def convention(self) -> str:
        if self.spacing_convention is None:
            raise RuleNotFound(
                f"spacing start/end convention not set in rulebook "
                f"{self.version} (spec D3)"
            )
        return self.spacing_convention


#: Demo rulebook. Values marked TO CONFIRM are placeholders for the POC and
#: must be replaced by the client's approved assumption sheet before any
#: figure is released.
DEMO_RULEBOOK = Rulebook(
    version="demo-0.1-UNAPPROVED",
    project_id="demo",
    cover_mm={"slab_top": 50, "slab_bottom": 75, "pile": 75},   # TO CONFIRM D2
    # NOTE: no generic fallback key. An element whose cover condition is not
    # listed raises RuleNotFound and becomes a clarification.
    lap_multiplier={BarSize.M10: 40, BarSize.M15: 40, BarSize.M20: 40,
                    BarSize.M30: 40},                            # TO CONFIRM D2
    hook_mm={(BarSize.M10, "90"): 260, (BarSize.M15, "90"): 260},  # observed
    stock_length_mm=9000,                                         # TO CONFIRM D1
    rounding_mm=5,
    spacing_convention="floor_plus_one",                          # TO CONFIRM D3
    approved_by=None,
)
