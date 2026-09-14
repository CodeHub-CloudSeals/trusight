"""Bar bending schedule writers: CSV, XLSX and PDF.

Column order follows the supplied RebarCAD bar lists (spec s9.1) so the
output drops into an estimator's existing process rather than asking them
to learn ours: Item, No., Size, Length, Mark, Type, then the populated leg
columns, then weight.

PDF is written with PyMuPDF, which the project already depends on for
extraction. A second PDF library would be a new dependency in the runtime
image for one report.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any

import pymupdf

from ..models.core import LEG_COLUMNS

#: Fixed header. The leg columns are always present so two exports of the
#: same project stay diffable even when one line happens to use fewer legs.
HEADER = ["Item", "No.", "Size", "Length (mm)", "Mark", "Type",
          *list(LEG_COLUMNS), "Weight (kg)", "Release"]


def _basis(data: dict[str, Any]) -> str:
    return ("SAMPLE - NOT FOR CONSTRUCTION" if data.get("seeded")
            else "FROM SOURCE DRAWINGS - ESTIMATOR REVIEW REQUIRED")


def _meta(data: dict[str, Any]) -> dict[str, str]:
    run = data.get("run", {})
    rb = data.get("rulebook", {})
    return {
        "Project": str(run.get("project_id", "")),
        "Run": str(run.get("run_id", ""))[:8],
        "Rulebook": str(rb.get("version", "")),
        "Rulebook approved by": str(rb.get("approved_by") or "not approved"),
        "Playbook": str(data.get("playbook") or "not recorded"),
        "Generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "Data basis": _basis(data),
    }


def bbs_rows(data: dict[str, Any]) -> list[list[Any]]:
    """One row per schedule line, in reference-bar-list column order."""
    rows: list[list[Any]] = []
    for item in data.get("schedule", {}).get("items", []):
        legs = item.get("legs") or {}
        rows.append([
            item.get("item_no"),
            item.get("quantity"),
            item.get("size"),
            item.get("cutting_length_mm"),
            item.get("mark") or "",
            item.get("bend_type") or "",
            *[legs.get(c, "") for c in LEG_COLUMNS],
            round(float(item.get("total_mass_kg") or 0), 1),
            item.get("release", ""),
        ])
    return rows


def weight_summary(data: dict[str, Any]) -> list[tuple[str, int, float]]:
    """Mass by bar size: (size, bars, kg), heaviest first."""
    by: dict[str, list[float]] = {}
    for item in data.get("schedule", {}).get("items", []):
        entry = by.setdefault(str(item.get("size")), [0, 0.0])
        entry[0] += int(item.get("quantity") or 0)
        entry[1] += float(item.get("total_mass_kg") or 0)
    return sorted(((k, int(v[0]), round(v[1], 1)) for k, v in by.items()),
                  key=lambda r: -r[2])


def _guard(value: Any) -> Any:
    """Stop a spreadsheet treating a source-supplied string as a formula."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


# ── CSV ─────────────────────────────────────────────────────────────────────

def schedule_csv(data: dict[str, Any]) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    for key, value in _meta(data).items():
        w.writerow([key, value])
    w.writerow([])
    w.writerow(HEADER)
    for row in bbs_rows(data):
        w.writerow([_guard(v) for v in row])
    w.writerow([])
    w.writerow(["Weight summary"])
    w.writerow(["Size", "Bars", "Mass (kg)"])
    for size, bars, kg in weight_summary(data):
        w.writerow([size, bars, kg])
    w.writerow(["Released total (kg)", "", round(float(data.get("released_mass_kg") or 0), 1)])
    w.writerow(["Held for review (kg)", "", round(float(data.get("review_mass_kg") or 0), 1)])
    return out.getvalue()


def exceptions_csv(data: dict[str, Any]) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    for key, value in _meta(data).items():
        w.writerow([key, value])
    w.writerow([])
    w.writerow(["Kind", "Detail"])
    for e in data.get("schedule", {}).get("exceptions", []) or []:
        w.writerow(["calculation exception", _guard(e)])
    for u in data.get("unresolved", []) or []:
        w.writerow(["unresolved", _guard(u)])
    for q in data.get("questions", []) or []:
        w.writerow(["open question", _guard(f"[{q.get('field')}] {q.get('question')}")])
    for h in data.get("history", []) or []:
        value = h.get("value", {})
        w.writerow(["approved answer", _guard(
            f"{value.get('field')} = {value.get('value')} "
            f"by {h.get('approver')} - {h.get('rationale')}")])
    return out.getvalue()


# ── XLSX ────────────────────────────────────────────────────────────────────

class ExportUnavailable(RuntimeError):
    """A format this build cannot write. Say so; do not fail with a 500."""


def xlsx_available() -> bool:
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        return False
    return True


def schedule_xlsx(data: dict[str, Any]) -> bytes:
    if not xlsx_available():
        # A missing optional dependency must not surface as a server error in
        # front of a client. The CSV and PDF carry the same schedule.
        raise ExportUnavailable(
            "XLSX export needs openpyxl, which is not installed in this "
            "environment. Run 'pip install -r requirements.txt'. The same "
            "schedule is available as bbs.csv and bbs.pdf.")
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Bar bending schedule"

    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="E8EEF9")
    thin = Side(style="thin", color="C9D3E0")
    box = Border(bottom=thin)

    r = 1
    for key, value in _meta(data).items():
        ws.cell(r, 1, key).font = bold
        ws.cell(r, 2, value)
        r += 1
    r += 1

    header_row = r
    for c, name in enumerate(HEADER, start=1):
        cell = ws.cell(r, c, name)
        cell.font = bold
        cell.fill = head_fill
        cell.border = box
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
    r += 1

    for row in bbs_rows(data):
        for c, value in enumerate(row, start=1):
            cell = ws.cell(r, c, _guard(value))
            if isinstance(value, (int, float)):
                cell.alignment = Alignment(horizontal="right")
        r += 1

    r += 1
    ws.cell(r, 1, "Weight summary").font = bold
    r += 1
    for c, name in enumerate(("Size", "Bars", "Mass (kg)"), start=1):
        ws.cell(r, c, name).font = bold
    r += 1
    for size, bars, kg in weight_summary(data):
        ws.cell(r, 1, size); ws.cell(r, 2, bars); ws.cell(r, 3, kg)
        r += 1
    r += 1
    ws.cell(r, 1, "Released total (kg)").font = bold
    ws.cell(r, 3, round(float(data.get("released_mass_kg") or 0), 1))
    r += 1
    ws.cell(r, 1, "Held for review (kg)").font = bold
    ws.cell(r, 3, round(float(data.get("review_mass_kg") or 0), 1))

    # the header row stays visible when an estimator scrolls a long schedule
    ws.freeze_panes = ws.cell(header_row + 1, 1)
    widths = [6, 7, 7, 12, 10, 7] + [6] * len(LEG_COLUMNS) + [12, 11]
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width

    exc = wb.create_sheet("Exceptions")
    exc.append(["Kind", "Detail"])
    for c in (1, 2):
        exc.cell(1, c).font = bold
    for line in exceptions_csv(data).splitlines():
        parts = next(csv.reader([line]), [])
        if len(parts) == 2 and parts[0] in (
                "calculation exception", "unresolved", "open question",
                "approved answer"):
            exc.append(parts)
    exc.column_dimensions["A"].width = 22
    exc.column_dimensions["B"].width = 110

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── PDF ─────────────────────────────────────────────────────────────────────

_PAGE = pymupdf.paper_rect("a4-l")
_MARGIN = 36


def _pdf_open() -> tuple[pymupdf.Document, pymupdf.Page, float]:
    doc = pymupdf.open()
    page = doc.new_page(width=_PAGE.width, height=_PAGE.height)
    return doc, page, _MARGIN


def _text(page, x, y, s, size=8, bold=False, color=(0.07, 0.11, 0.19)):
    page.insert_text((x, y), str(s), fontsize=size,
                     fontname="hebo" if bold else "helv", color=color)


def _title_block(page, data, title) -> float:
    meta = _meta(data)
    _text(page, _MARGIN, _MARGIN + 6, "TRUSTSIGHT", size=13, bold=True)
    _text(page, _MARGIN + 92, _MARGIN + 6, title, size=13)
    page.draw_line(pymupdf.Point(_MARGIN, _MARGIN + 14),
                   pymupdf.Point(_PAGE.width - _MARGIN, _MARGIN + 14),
                   color=(0.8, 0.84, 0.9), width=.8)
    y = _MARGIN + 30
    for i, (key, value) in enumerate(meta.items()):
        col = _MARGIN + (i % 3) * 250
        if i and i % 3 == 0:
            y += 12
        _text(page, col, y, f"{key}:", size=7, color=(0.42, 0.47, 0.56))
        _text(page, col + 82, y, value, size=7)
    return y + 22


def schedule_pdf(data: dict[str, Any]) -> bytes:
    doc, page, x0 = _pdf_open()
    y = _title_block(page, data, "Bar bending schedule")

    widths = [28, 32, 32, 52, 44, 30] + [26] * len(LEG_COLUMNS) + [50, 52]
    xs, acc = [], x0
    for w in widths:
        xs.append(acc); acc += w

    def header(y):
        for x, name in zip(xs, HEADER):
            _text(page, x, y, name, size=6.5, bold=True, color=(0.42, 0.47, 0.56))
        page.draw_line(pymupdf.Point(x0, y + 3),
                       pymupdf.Point(acc, y + 3), color=(0.8, 0.84, 0.9), width=.7)
        return y + 14

    y = header(y)
    for row in bbs_rows(data):
        if y > _PAGE.height - 90:
            page = doc.new_page(width=_PAGE.width, height=_PAGE.height)
            y = header(_MARGIN + 20)
        for x, value in zip(xs, row):
            _text(page, x, y, "" if value == "" else value, size=7)
        y += 12

    y += 10
    page.draw_line(pymupdf.Point(x0, y - 6), pymupdf.Point(acc, y - 6),
                   color=(0.8, 0.84, 0.9), width=.7)
    _text(page, x0, y + 6, "Weight summary", size=8, bold=True)
    y += 20
    for size, bars, kg in weight_summary(data):
        _text(page, x0, y, f"{size}   {bars} bars   {kg:,.1f} kg", size=7.5)
        y += 12
    _text(page, x0, y + 4,
          f"Released ... "
          f"held for review {float(data.get('review_mass_kg') or 0):,.1f} kg",
          size=8, bold=True)

    _text(page, x0, _PAGE.height - _MARGIN,
          "Quantities describe the estimation schedule. Stock splitting, lap "
          "design and fabrication approval are outside this document.",
          size=6.5, color=(0.42, 0.47, 0.56))
    out = doc.tobytes()
    doc.close()
    return out


def exceptions_pdf(data: dict[str, Any]) -> bytes:
    doc, page, x0 = _pdf_open()
    y = _title_block(page, data, "Exception and clarification report")

    sections = (
        ("Calculation exceptions - excluded from the released totals",
         data.get("schedule", {}).get("exceptions", []) or []),
        ("Unresolved on the drawing", data.get("unresolved", []) or []),
        ("Open questions", [f"[{q.get('field')}] {q.get('question')}"
                            for q in data.get("questions", []) or []]),
        ("Approved answers", [
            f"{h.get('value', {}).get('field')} = {h.get('value', {}).get('value')}"
            f"  -  {h.get('approver')}: {h.get('rationale')}"
            for h in data.get("history", []) or []]),
    )
    for title, lines in sections:
        if y > _PAGE.height - 110:
            page = doc.new_page(width=_PAGE.width, height=_PAGE.height)
            y = _MARGIN + 30
        _text(page, x0, y, title, size=8.5, bold=True)
        y += 14
        if not lines:
            _text(page, x0 + 10, y, "none", size=7.5, color=(0.42, 0.47, 0.56))
            y += 16
            continue
        for line in lines:
            for chunk in _wrap(str(line), 150):
                if y > _PAGE.height - 60:
                    page = doc.new_page(width=_PAGE.width, height=_PAGE.height)
                    y = _MARGIN + 30
                _text(page, x0 + 10, y, chunk, size=7.5)
                y += 11
            y += 3
        y += 10

    out = doc.tobytes()
    doc.close()
    return out


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines or [""]
