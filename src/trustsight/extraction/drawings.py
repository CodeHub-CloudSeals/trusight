"""Document to structural facts.

This is the layer the demonstrator previously skipped: it starts from the
actual supplied PDF and produces ``Element`` objects, rather than accepting
hand-built Python. Everything it emits carries the sheet reference and
extraction tier that produced it.

The pile reader below is deliberately narrow. It reads the Atlantic Cages
pile schedule and section callouts using the ``pile_v1`` playbook resolution
sequence. It does not attempt twenty element families; it makes one real
case flow end to end and fail safely where the drawing is silent.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from ..models.core import (
    BarSize,
    CountingPattern,
    Element,
    ElementIdentity,
    ElementType,
    Geometry,
    Placement,
    Provenance,
    RebarRole,
    Reinforcement,
    SheetRef,
    SourceTier,
)

#: "12-30M" -> twelve 30M bars
COUNT_SIZE = re.compile(r"\b(\d{1,3})\s*[-–]\s*(\d{1,2}M)\b")
#: "15M@350" or "15M @ 350 O.C."
SIZE_SPACING = re.compile(r"\b(\d{1,2}M)\s*@\s*(\d{2,4})\b")
#: a diameter cell such as "1000 Ø"
DIAMETER = re.compile(r"\b(\d{3,4})\s*Ø")


@dataclass
class ExtractedFact:
    """One value read from a drawing, with where and how it was read."""

    field: str
    value: object
    sheet: SheetRef
    tier: SourceTier
    verbatim: str

    def as_evidence(self) -> dict[str, object]:
        return {
            "field": self.field,
            "value": self.value,
            "sheet": self.sheet.cite(),
            "tier": self.tier.value,
            "verbatim": self.verbatim,
        }


@dataclass
class ExtractionResult:
    elements: list[Element] = field(default_factory=list)
    facts: list[ExtractedFact] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    pages_processed: int = 0

    @property
    def values_extracted(self) -> int:
        return len(self.facts)


def _sheet_from_filename(path: Path) -> str | None:
    """Title-block fallback: many sets carry the sheet number in the filename."""
    m = re.search(r"([A-Z]{2}-\d{3}-[A-Z]-\d{3,4})", path.stem)
    return m.group(1) if m else None


def _words_near(page, anchor_word: str, dx: float, dy: float):
    words = page.get_text("words")
    anchors = [w for w in words if w[4] == anchor_word]
    if not anchors:
        return []
    ax, ay = anchors[0][0], anchors[0][1]
    rows: dict[int, list[tuple[float, str]]] = defaultdict(list)
    for x0, y0, x1, y1, tok, *_ in words:
        if abs(y0 - ay) < dy and abs(x0 - ax) < dx:
            rows[round(y0)].append((x0, tok))
    return [(y, [t for _, t in sorted(cells)]) for y, cells in sorted(rows.items())]


def read_pile_project(pdf_path: str | Path, project_id: str) -> ExtractionResult:
    """Read a pile drawing set following the ``pile_v1`` playbook.

    Resolution sequence: schedule, then referenced section, then detail,
    then general notes. Anything still unresolved is reported rather than
    defaulted.
    """
    path = Path(pdf_path)
    # The unit of work is the drawing set, not one file. A run may be pointed
    # at the project folder, in which case the pile sheet is the first input.
    if path.is_dir():
        inputs = sorted(path.glob("Input*.pdf")) or sorted(path.glob("*.pdf"))
        if not inputs:
            result = ExtractionResult()
            result.unresolved.append(f"no input drawings in {path.name}")
            return result
        path = inputs[0]
    result = ExtractionResult()
    doc = pymupdf.open(path)

    pile_type: str | None = None
    diameter_mm: int | None = None
    length_mm: int | None = None
    long_count: int | None = None
    long_size: str | None = None
    tie_size: str | None = None
    tie_spacing: int | None = None
    instances = 0
    sheet_ref: SheetRef | None = None

    for page_no, page in enumerate(doc, start=1):
        result.pages_processed += 1
        text = page.get_text()
        if not text.strip():
            continue

        m = re.search(r"\b([A-Z]{2}-\d{3}-[A-Z]-\d{3,4})\b", text)
        sheet_no = m.group(1) if m else _sheet_from_filename(path)
        ref = SheetRef(document_id=path.name, page=page_no, sheet_no=sheet_no)
        sheet_ref = sheet_ref or ref

        # -- step 1 of the playbook: the schedule
        rows = _words_near(page, "SCHEDULE", dx=340, dy=170)
        for _y, cells in rows:
            line = " ".join(cells)
            dm = DIAMETER.search(line)
            if dm and diameter_mm is None:
                diameter_mm = int(dm.group(1))
                result.facts.append(ExtractedFact(
                    "pile_diameter_mm", diameter_mm, ref,
                    SourceTier.NATIVE_TEXT, line[:80]))
                # the length cell follows the diameter on the same row
                nums = [int(t) for t in cells if t.isdigit() and len(t) >= 4]
                candidates = [n for n in nums if n != diameter_mm and n < 60000]
                if candidates:
                    length_mm = candidates[0]
                    result.facts.append(ExtractedFact(
                        "pile_length_mm", length_mm, ref,
                        SourceTier.NATIVE_TEXT, line[:80]))

        # -- instance count: repeated type rows in the pile location table
        type_rows = re.findall(r"TYPE\s+([A-Z])\b", text)
        if type_rows:
            counted = sum(1 for t in type_rows if t == type_rows[0])
            # the schedule header itself contributes one occurrence
            if counted > 1:
                instances = max(instances, counted - 1)
                pile_type = pile_type or type_rows[0]

        # -- step 2/3: section and detail callouts
        for cnt, size in COUNT_SIZE.findall(text):
            if size in {s.value for s in BarSize} and long_count is None:
                long_count, long_size = int(cnt), size
                result.facts.append(ExtractedFact(
                    "longitudinal", f"{cnt}-{size}", ref,
                    SourceTier.NATIVE_TEXT, f"{cnt}-{size}"))
        for size, spacing in SIZE_SPACING.findall(text):
            if size in {s.value for s in BarSize} and tie_size is None:
                tie_size, tie_spacing = size, int(spacing)
                result.facts.append(ExtractedFact(
                    "transverse", f"{size}@{spacing}", ref,
                    SourceTier.NATIVE_TEXT, f"{size}@{spacing}"))

    doc.close()

    if sheet_ref is None:
        result.unresolved.append("no readable page in document")
        return result
    if not instances:
        result.unresolved.append(
            "pile instance count not found: no repeated type rows in a location table"
        )
        return result
    if long_count is None or long_size is None:
        result.unresolved.append("longitudinal reinforcement callout not found")
        return result

    # -- assemble the element -------------------------------------------
    identity = ElementIdentity(
        project_id=project_id, element_type=ElementType.PILE, mark=f"P-{pile_type or 'A'}"
    )
    reinforcement: list[Reinforcement] = []

    reinforcement.append(
        Reinforcement(
            bar_size=BarSize(long_size),
            role=RebarRole.LONGITUDINAL,
            count=long_count,
            cover_condition="pile",
            source=[sheet_ref],
            source_tier=SourceTier.NATIVE_TEXT,
        )
    )

    if tie_size and tie_spacing:
        # Run length is NOT the pile length: reinforcement stops short of the
        # cut-off and the cover face. The drawing does not state the run, so
        # it is left unresolved rather than assumed from the pile length.
        reinforcement.append(
            Reinforcement(
                bar_size=BarSize(tie_size),
                role=RebarRole.SPIRAL,
                spacing_mm=tie_spacing,
                run_length_mm=None,
                cover_condition="pile",
                source=[sheet_ref],
                source_tier=SourceTier.NATIVE_TEXT,
                unknown_fields=["run_length_mm"],
            )
        )
        result.unresolved.append(
            f"spiral run length for {tie_size}@{tie_spacing}: the drawing states "
            f"pile length {length_mm}mm but not the reinforced run, so the tie "
            f"count cannot be derived without the estimator's endpoint convention"
        )
    else:
        result.unresolved.append("transverse reinforcement callout not found")

    geometry = Geometry(
        primitive="cylinder",
        params={k: v for k, v in (("diameter", diameter_mm), ("length", length_mm))
                if v is not None},
        placements=[
            # evenly spaced for legibility; the schedule gives a count, not
            # plan coordinates, so the placement is schematic
            Placement(x=i * 3000.0, y=0.0, sheet=sheet_ref, schematic=True)
            for i in range(instances)
        ],
        source=SourceTier.NATIVE_TEXT,
    )

    result.elements.append(
        Element(
            identity=identity,
            element_type=ElementType.PILE,
            mark=identity.mark,
            instances=instances,
            instance_basis=Provenance(
                pattern=CountingPattern.SCHEDULE_QTY,
                evidence=[sheet_ref],
                detail=f"pile location table: {instances} rows of type {pile_type}",
            ),
            geometry=geometry,
            reinforcement=reinforcement,
            sheets=[sheet_ref],
        )
    )
    return result
