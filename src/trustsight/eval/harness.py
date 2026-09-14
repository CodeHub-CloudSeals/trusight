"""Evaluation harness. Spec section 11.

Built first, before pipeline code. Five ground-truth pairs with a harness
make every POC claim reproducible; without one they are anecdotes.

Item matching is by (size, cutting_length, mark) with quantity compared
separately, so a count error and a length error are distinguishable rather
than being reported as one failure.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

from ..extraction.barlist import parse_bar_list
from ..models.core import BarItem, BarSchedule
from ..shapes.catalogue import infer_from_sum, verify


@dataclass
class ProjectMetrics:
    project_id: str
    reference_items: int = 0
    generated_items: int = 0
    matched_items: int = 0
    reference_bars: int = 0
    generated_bars: int = 0
    reference_mass_kg: float = 0.0
    generated_mass_kg: float = 0.0
    shape_verified: int = 0
    shape_failed: list[str] = field(default_factory=list)
    quantity_mismatches: list[str] = field(default_factory=list)
    length_mismatches: list[str] = field(default_factory=list)
    unmatched_reference: list[str] = field(default_factory=list)
    unmatched_generated: list[str] = field(default_factory=list)

    @property
    def element_recall(self) -> float:
        if not self.reference_items:
            return 0.0
        return self.matched_items / self.reference_items

    @property
    def mass_variance_pct(self) -> float:
        if not self.reference_mass_kg:
            return 0.0
        return (self.generated_mass_kg - self.reference_mass_kg) / self.reference_mass_kg * 100


def _key(item: BarItem) -> tuple[str, int, str]:
    return (item.size.value, item.cutting_length_mm, (item.mark or "").upper())


def compare(reference: BarSchedule, generated: BarSchedule | None) -> ProjectMetrics:
    m = ProjectMetrics(project_id=reference.project_id)
    m.reference_items = len(reference.items)
    m.reference_bars = sum(i.quantity for i in reference.items)
    m.reference_mass_kg = reference.total_mass_kg

    # Shape catalogue validation always runs, generated schedule or not.
    for it in reference.items:
        ok, msg = verify(it.bend_type, it.legs, it.cutting_length_mm)
        if ok:
            m.shape_verified += 1
        else:
            hint = infer_from_sum(it.legs, it.cutting_length_mm)
            m.shape_failed.append(
                f"item {it.item_no} ({it.mark or 'straight'}/{it.bend_type or '-'}): "
                f"{msg}" + (f"; columns {'+'.join(hint)} would sum correctly" if hint else "")
            )

    if generated is None:
        m.unmatched_reference = [f"item {i.item_no}" for i in reference.items]
        return m

    m.generated_items = len(generated.items)
    m.generated_bars = sum(i.quantity for i in generated.items)
    m.generated_mass_kg = generated.total_mass_kg

    pool = {}
    for it in generated.items:
        pool.setdefault(_key(it), []).append(it)

    for ref in reference.items:
        bucket = pool.get(_key(ref))
        if not bucket:
            near = [
                g for g in generated.items
                if g.size == ref.size and abs(g.cutting_length_mm - ref.cutting_length_mm) <= 25
            ]
            if near:
                m.length_mismatches.append(
                    f"item {ref.item_no}: reference {ref.cutting_length_mm}mm vs "
                    f"generated {near[0].cutting_length_mm}mm"
                )
            else:
                m.unmatched_reference.append(
                    f"item {ref.item_no}: {ref.quantity}x {ref.size.value} "
                    f"@{ref.cutting_length_mm}mm"
                )
            continue
        got = bucket.pop(0)
        m.matched_items += 1
        if got.quantity != ref.quantity:
            m.quantity_mismatches.append(
                f"item {ref.item_no}: reference {ref.quantity} bars vs "
                f"generated {got.quantity}"
            )

    for key, leftovers in pool.items():
        for g in leftovers:
            m.unmatched_generated.append(
                f"{g.quantity}x {g.size.value} @{g.cutting_length_mm}mm"
            )
    return m


def discover(corpus_root: str | Path) -> dict[str, Path]:
    """Map project_id -> reference bar list path."""
    root = Path(corpus_root)
    out: dict[str, Path] = {}
    for pdf in sorted(root.glob("*/Output*.pdf")):
        out[pdf.parent.name] = pdf
    return out


def run(corpus_root: str | Path,
        generate=None) -> tuple[list[ProjectMetrics], dict]:
    """Run the harness. ``generate`` maps a project dir to a BarSchedule."""
    results: list[ProjectMetrics] = []
    for project_id, ref_path in discover(corpus_root).items():
        reference = parse_bar_list(ref_path, project_id=project_id)
        generated = generate(Path(ref_path).parent) if generate else None
        results.append(compare(reference, generated))

    totals = {
        "projects": len(results),
        "reference_items": sum(r.reference_items for r in results),
        "reference_bars": sum(r.reference_bars for r in results),
        "reference_mass_kg": round(sum(r.reference_mass_kg for r in results), 1),
        "shape_verified": sum(r.shape_verified for r in results),
        "shape_failed": sum(len(r.shape_failed) for r in results),
        "matched_items": sum(r.matched_items for r in results),
        "generated_mass_kg": round(sum(r.generated_mass_kg for r in results), 1),
    }
    return results, totals


def report(results: list[ProjectMetrics], totals: dict) -> str:
    lines = ["TrustSight evaluation harness", "=" * 78, ""]
    lines.append(
        f"{'project':<34}{'items':>6}{'bars':>7}{'mass kg':>11}{'shape':>9}{'recall':>9}"
    )
    lines.append("-" * 78)
    for r in results:
        shape = f"{r.shape_verified}/{r.reference_items}"
        recall = f"{r.element_recall:.0%}" if r.generated_items else "n/a"
        lines.append(
            f"{r.project_id[:33]:<34}{r.reference_items:>6}{r.reference_bars:>7}"
            f"{r.reference_mass_kg:>11,.1f}{shape:>10}{recall:>9}"
        )
    lines.append("-" * 78)
    shape_total = f"{totals['shape_verified']}/{totals['reference_items']}"
    lines.append(
        f"{'TOTAL':<34}{totals['reference_items']:>6}{totals['reference_bars']:>7}"
        f"{totals['reference_mass_kg']:>11,.1f}{shape_total:>10}"
    )
    lines.append("")
    lines.append(
        f"Shape catalogue: {totals['shape_verified']}/{totals['reference_items']} "
        f"items reconcile against the bend-type table."
    )
    failures = [f for r in results for f in r.shape_failed]
    if failures:
        lines.append("")
        lines.append("Shape failures:")
        lines.extend(f"  - {f}" for f in failures)
    for r in results:
        issues = r.quantity_mismatches + r.length_mismatches + r.unmatched_reference
        if issues and r.generated_items:
            lines.append("")
            lines.append(f"{r.project_id}:")
            lines.extend(f"  - {i}" for i in issues[:20])
    return "\n".join(lines)


def to_json(results: list[ProjectMetrics], totals: dict) -> str:
    return json.dumps(
        {"totals": totals, "projects": [asdict(r) for r in results]},
        indent=2, default=str,
    )
