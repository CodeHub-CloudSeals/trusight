"""RebarCAD bar-list parser.

Loads reference bar lists into ``BarItem`` objects. Used by the evaluation
harness (spec s11) to load ground truth, and during ingestion when a client
supplies existing schedules.

Column assignment uses the table's **ruled vertical lines**, not text
positions. Bar-list values are right-aligned inside their cells while header
captions are not, so nearest-header matching silently misassigns the
quantity column on some sheets. The ruled grid is exact: the five supplied
lists each yield 19 vertical rules bounding the 18 columns
(Item, No., Size, Length, Mark, Type, A-R).
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import pymupdf

from ..models.core import LEG_COLUMNS, BarItem, BarSchedule, BarSize, SheetRef

_MAIN = ("Item", "No.", "Size", "Length", "Mark", "Type")
_EXPECTED = list(_MAIN) + list(LEG_COLUMNS)


def _vertical_rules(page) -> list[float]:
    """X positions of the table's vertical rules."""
    xs: set[float] = set()
    for drawing in page.get_drawings():
        for item in drawing["items"]:
            if item[0] == "l":
                p1, p2 = item[1], item[2]
                if abs(p1.x - p2.x) < 0.6 and abs(p1.y - p2.y) > 6:
                    xs.add(round(p1.x, 1))
            elif item[0] == "re":
                r = item[1]
                if r.width < 1.2 and r.height > 6:
                    xs.add(round((r.x0 + r.x1) / 2, 1))
    return sorted(xs)


def _column_of(x: float, rules: list[float]) -> int | None:
    for i in range(len(rules) - 1):
        if rules[i] <= x < rules[i + 1]:
            return i
    return None


def _row_buckets(page, header_y: float) -> list[list[tuple[float, float, str]]]:
    buckets: dict[int, list[tuple[float, float, str]]] = defaultdict(list)
    for x0, y0, x1, y1, tok, *_ in page.get_text("words"):
        if y0 <= header_y + 4:
            continue
        buckets[round((y0 + y1) / 2)].append(((x0 + x1) / 2, y0, tok))
    merged: list[tuple[int, list[tuple[float, float, str]]]] = []
    for k in sorted(buckets):
        if merged and k - merged[-1][0] <= 3:
            merged[-1][1].extend(buckets[k])
        else:
            merged.append((k, list(buckets[k])))
    return [cells for _, cells in merged]


def parse_bar_list(path: str | Path, project_id: str | None = None) -> BarSchedule:
    """Parse a RebarCAD bar list PDF into a :class:`BarSchedule`."""
    path = Path(path)
    doc = pymupdf.open(path)
    page = doc[0]
    words = page.get_text("words")

    header_y = None
    for x0, y0, x1, y1, tok, *_ in words:
        if tok == "Item":
            header_y = round(y0)
            break
    if header_y is None:
        doc.close()
        raise ValueError(f"{path.name}: no 'Item' header found; not a bar list")

    rules = _vertical_rules(page)
    if len(rules) < len(_EXPECTED):
        doc.close()
        raise ValueError(
            f"{path.name}: found {len(rules)} vertical rules, expected at least "
            f"{len(_EXPECTED)}; table grid could not be read"
        )

    col_name: dict[int, str] = {}
    for x0, y0, x1, y1, tok, *_ in words:
        if round(y0) != header_y or tok not in _EXPECTED:
            continue
        idx = _column_of((x0 + x1) / 2, rules)
        if idx is not None:
            col_name[idx] = tok

    m = re.search(r"Dwg\.\s*No\s*:?\s*(\S+)", page.get_text())
    ref = SheetRef(document_id=path.name, page=1, sheet_no=m.group(1) if m else None)

    valid_sizes = {s.value for s in BarSize}
    items: list[BarItem] = []

    for cells in _row_buckets(page, header_y):
        rec: dict[str, str] = {}
        for cx, _y, tok in sorted(cells):
            idx = _column_of(cx, rules)
            if idx is None:
                continue
            name = col_name.get(idx)
            if name:
                rec[name] = rec.get(name, "") + tok

        if not re.fullmatch(r"\d{1,2}", rec.get("Item", "")):
            continue
        if rec.get("Size") not in valid_sizes:
            continue

        legs = {
            c: int(rec[c])
            for c in LEG_COLUMNS
            if c in rec and rec[c].lstrip("-").isdigit()
        }
        items.append(
            BarItem(
                item_no=int(rec["Item"]),
                quantity=int(rec.get("No.") or 0),
                size=BarSize(rec["Size"]),
                cutting_length_mm=int(rec["Length"]),
                mark=rec.get("Mark"),
                bend_type=rec.get("Type"),
                legs=legs,
                evidence=[ref],
            )
        )

    doc.close()
    return BarSchedule(project_id=project_id or path.parent.name, items=items)
