"""Pad-footing reader — the ``footing_v1`` playbook.

Second element family after ``pile_v1``. The Kingston set is not a drawing
sheet with a schedule table; it is three RFI packages, each carrying a
sketch of one CS-01 concrete foundation. So the resolution sequence differs
from the pile playbook: there is no schedule to anchor on, and the facts
come from the plan callout and the section dimension strings.

What this reader will and will not do:

* The mat multiplier is **read, not assumed**. ``6 - 20M BARS (TOP &
  BOTTOM, EW)`` states six bars, two layers and two directions on the face
  of the drawing, so 24 bars per foundation is extraction. Had the callout
  said only ``6 - 20M BARS`` the reader would emit six and say so.
* The **cutting length is never derived here.** The sketches give the
  foundation outline and the cover, but not the bar shape, its bend type or
  its leg dimensions. Those become clarifications. A 1,200 mm footing with
  75 mm cover has an obvious-looking 1,050 mm bar in it; the reference bar
  list says 1,120 mm. Guessing that number is precisely the failure this
  product exists to prevent.
"""
from __future__ import annotations

import re
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
from .drawings import ExtractedFact, ExtractionResult

#: "6 - 20M BARS (TOP & BOTTOM, EW)" — the callout may wrap across lines, so
#: the page text is whitespace-normalised before this is applied.
BAR_CALLOUT = re.compile(
    r"\b(\d{1,3})\s*[-–]\s*(\d{1,2}M)\s+BARS?\s*\(?([^)]{0,40})\)?", re.I)
#: a dimension string such as "1,200 mm" or "300 mm"
DIM_MM = re.compile(r"\b(\d{1,3}(?:,\d{3})*)\s*mm\b", re.I)
#: the foundation tag, e.g. "CS-01"
FDN_TAG = re.compile(r"\b([A-Z]{2}-\d{2})\b")
#: RFI number in the filename or body, used as the sheet reference
RFI_REF = re.compile(r"RFI[-\s]?(\d{3})", re.I)


def _norm(text: str) -> str:
    return " ".join(text.split())


def _dims(text: str) -> list[int]:
    return [int(m.replace(",", "")) for m in DIM_MM.findall(text)]


def _covers_near_clr(page) -> list[int]:
    """Every cover value annotated ``CLR`` on this sketch, nearest-first.

    These sketches carry more than one cover — a soil face and an interior
    face do not share a value — and the plain text order does not preserve
    which number belongs to which label, so this matches on page geometry.
    Returning all of them rather than the closest one is deliberate: when a
    drawing states two covers, silently taking one is the substitution that
    invariant I2 forbids. The caller decides, or the rulebook does.
    """
    words = page.get_text("words")
    clrs = [w for w in words if w[4].upper().strip("().,") == "CLR"]
    nums = [w for w in words if w[4].replace(",", "").isdigit()]
    if not clrs or not nums:
        return []
    found: list[tuple[float, int]] = []
    for cx, cy in ((w[0], w[1]) for w in clrs):
        nearest: tuple[float, int] | None = None
        for w in nums:
            value = int(w[4].replace(",", ""))
            if not 20 <= value <= 150:
                continue
            d = abs(w[0] - cx) + abs(w[1] - cy)
            if nearest is None or d < nearest[0]:
                nearest = (d, value)
        if nearest is not None:
            found.append(nearest)
    seen: list[int] = []
    for _d, value in sorted(found):
        if value not in seen:
            seen.append(value)
    return seen


def _is_sketch_page(text: str) -> bool:
    """A foundation sketch: the concrete foundation, drawn with dimensions."""
    upper = text.upper()
    return ("CONCRETE" in upper and "FOUNDATION" in upper
            and len(DIM_MM.findall(text)) >= 5)


def _documents(source: Path) -> list[Path]:
    """One run may be pointed at a single PDF or at the project folder."""
    if source.is_dir():
        return sorted(source.glob("Input*.pdf")) or sorted(source.glob("*.pdf"))
    return [source]


def read_footing_project(source: str | Path, project_id: str) -> ExtractionResult:
    """Read a pad-footing set following the ``footing_v1`` playbook.

    Resolution sequence: locate the foundation sketch pages, read the plan
    dimensions and the cover, then the reinforcement callout. Each document
    in the set that carries a foundation sketch is one instance.
    """
    root = Path(source)
    result = ExtractionResult()

    plan_mm: int | None = None
    thickness_mm: int | None = None
    covers: list[int] = []
    bar_size: str | None = None
    per_layer: int | None = None
    layers = 1
    directions = 1
    callout_text = ""
    fdn_tag: str | None = None
    sheet_ref: SheetRef | None = None
    instances = 0
    seen_docs: list[str] = []

    for pdf in _documents(root):
        doc = pymupdf.open(pdf)
        doc_has_sketch = False

        for page_no, page in enumerate(doc, start=1):
            text = page.get_text()
            result.pages_processed += 1
            if not _is_sketch_page(text):
                continue
            doc_has_sketch = True
            flat = _norm(text)

            rfi = RFI_REF.search(pdf.stem) or RFI_REF.search(flat)
            ref = SheetRef(
                document_id=pdf.name,
                page=page_no,
                sheet_no=f"RFI-{rfi.group(1)}" if rfi else None,
            )
            sheet_ref = sheet_ref or ref

            if fdn_tag is None:
                tag = FDN_TAG.search(flat)
                if tag:
                    fdn_tag = tag.group(1)

            # -- step 1: the foundation outline -------------------------
            values = _dims(flat)
            if plan_mm is None and values:
                # the plan is square and its dimension is the largest value
                # stated twice on the sketch
                repeated = [v for v in set(values)
                            if values.count(v) >= 2 and v >= 500]
                if repeated:
                    plan_mm = max(repeated)
                    result.facts.append(ExtractedFact(
                        "footing_plan_mm", plan_mm, ref, SourceTier.NATIVE_TEXT,
                        f"{plan_mm:,} mm (stated {values.count(plan_mm)} times)"))
                    smaller = [v for v in set(values)
                               if values.count(v) >= 2 and 150 <= v < plan_mm]
                    if smaller:
                        thickness_mm = max(smaller)
                        result.facts.append(ExtractedFact(
                            "footing_thickness_mm", thickness_mm, ref,
                            SourceTier.NATIVE_TEXT, f"{thickness_mm} mm"))

            # -- step 2: cover, read beside the CLR annotations ----------
            if not covers:
                covers = _covers_near_clr(page)
                for value in covers:
                    result.facts.append(ExtractedFact(
                        "cover_mm", value, ref, SourceTier.NATIVE_TEXT,
                        f"{value} mm CLR"))

            # -- step 3: the reinforcement callout ----------------------
            if per_layer is None:
                m = BAR_CALLOUT.search(flat)
                if m and m.group(2) in {s.value for s in BarSize}:
                    per_layer = int(m.group(1))
                    bar_size = m.group(2)
                    callout_text = _norm(m.group(0))
                    qualifier = m.group(3).upper()
                    # both multipliers are stated on the face of the drawing
                    if "BOTTOM" in qualifier and "TOP" in qualifier:
                        layers = 2
                    if "EW" in qualifier or "EACH WAY" in qualifier:
                        directions = 2
                    result.facts.append(ExtractedFact(
                        "mat_reinforcement", callout_text, ref,
                        SourceTier.NATIVE_TEXT, callout_text))

        doc.close()
        if doc_has_sketch:
            instances += 1
            seen_docs.append(pdf.name)

    # -- refuse to invent anything that is missing --------------------------
    if sheet_ref is None:
        result.unresolved.append(
            "no foundation sketch page found: no page carries a concrete "
            "foundation with dimension callouts")
        return result
    if per_layer is None or bar_size is None:
        result.unresolved.append(
            "mat reinforcement callout not found on any foundation sketch")
        return result

    count = per_layer * layers * directions
    identity_mark = fdn_tag or "F-A"
    identity = ElementIdentity(
        project_id=project_id, element_type=ElementType.FOOTING,
        mark=identity_mark)

    # One stated cover is a fact. Two are a question the drawing does not
    # answer, so the approved rulebook governs instead of a coin toss.
    cover_mm = covers[0] if len(covers) == 1 else None
    if len(covers) > 1:
        result.unresolved.append(
            f"{identity_mark} states more than one cover ("
            + ", ".join(f"{c} mm" for c in covers)
            + "); the governing face is not annotated, so cover is taken from "
              "the approved rulebook rather than picked from the sketch")

    reinforcement = [
        Reinforcement(
            bar_size=BarSize(bar_size),
            role=RebarRole.EACH_WAY if directions == 2 else RebarRole.BOTTOM,
            count=count,
            cover_mm=cover_mm,
            cover_condition="footing",
            source=[sheet_ref],
            source_tier=SourceTier.NATIVE_TEXT,
            # Absent legs are not listed in ``unknown_fields``. That list is
            # for fields the drawing should have carried and did not; missing
            # legs are raised by the calculator and by the clarification
            # builder, which already asks for the shape code alongside them.
            # Listing them here as well produces two questions for one gap and
            # leaves a blocker the approval path does not clear.
        )
    ]
    result.unresolved.append(
        f"cutting length for {bar_size} mat in {identity.mark}: the sketch gives "
        f"the {plan_mm or '?'}mm foundation and its cover but not "
        f"the bar shape, bend type or leg dimensions, so the cutting length "
        f"cannot be derived without the estimator's detailing convention"
    )

    geometry = Geometry(
        primitive="box",
        params={k: v for k, v in (("width", plan_mm), ("depth", plan_mm),
                                  ("length", thickness_mm)) if v is not None},
        # Spacing here is a layout convenience, not a surveyed position: the
        # sketches give no plan coordinates. It is tagged schematic so the
        # spatial view says so rather than implying a source-derived layout.
        placements=[Placement(x=i * 4000.0, y=0.0, sheet=sheet_ref,
                              schematic=True)
                    for i in range(instances)],
        source=SourceTier.NATIVE_TEXT,
    )

    result.elements.append(
        Element(
            identity=identity,
            element_type=ElementType.FOOTING,
            mark=identity.mark,
            instances=instances,
            instance_basis=Provenance(
                pattern=CountingPattern.PLAN_INSTANCE_COUNT,
                evidence=[sheet_ref],
                detail=(f"{instances} foundation sketches in the set: "
                        + ", ".join(seen_docs)),
            ),
            geometry=geometry,
            reinforcement=reinforcement,
            sheets=[sheet_ref],
        )
    )
    return result
