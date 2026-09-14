"""Tiered extraction. Spec section 8.

Mandatory order per sheet region:
    1. native text   - the PDF text layer
    2. vector path   - geometry from drawing primitives
    3. vision        - only where text exists as outlines
    4. OCR           - only for genuine raster

All nine supplied input files are vector PDFs with live text layers, and
four of five projects yield rebar callouts as machine-readable text. Reaching
for a vision model by default would be slower, costlier and less accurate
across most of this corpus.

The tier that produced a value is recorded and is a governance input: the
gate vector refuses auto-proceed on vision and OCR sources.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from ..models.core import SheetRef, SourceTier

#: e.g. "15M @ 300 O.C.", "12-30M", "15M@350", "(4) 10M VERTICAL"
REBAR_PATTERN = re.compile(
    r"(?:\(?\d+\)?\s*[-x]\s*)?\b\d{1,2}M\b(?:\s*@\s*\d+)?", re.IGNORECASE
)
SPACING_PATTERN = re.compile(r"@\s*(\d{2,4})\s*(?:mm)?\s*(?:O\.?C\.?|C/C)?", re.I)
COVER_PATTERN = re.compile(r"(\d{2,3})\s*mm\s+COVER", re.I)


@dataclass
class SheetExtract:
    ref: SheetRef
    tier: SourceTier
    text: str = ""
    text_chars: int = 0
    vector_ops: int = 0
    images: int = 0
    rebar_mentions: list[str] = field(default_factory=list)
    has_text_layer: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def needs_vision(self) -> bool:
        """Vector page with drawn geometry but no extractable annotation."""
        return self.vector_ops > 200 and not self.rebar_mentions


@dataclass
class DocumentExtract:
    path: Path
    sheets: list[SheetExtract] = field(default_factory=list)

    @property
    def tier(self) -> SourceTier:
        tiers = {s.tier for s in self.sheets}
        for t in (SourceTier.OCR, SourceTier.VISION,
                  SourceTier.VECTOR_PATH, SourceTier.NATIVE_TEXT):
            if t in tiers:
                return t
        return SourceTier.OCR


def classify_tier(text_chars: int, vector_ops: int, images: int) -> SourceTier:
    if text_chars > 500 and vector_ops > 200:
        return SourceTier.NATIVE_TEXT
    if vector_ops > 200:
        return SourceTier.VECTOR_PATH
    if images and text_chars < 200:
        return SourceTier.OCR
    return SourceTier.NATIVE_TEXT if text_chars else SourceTier.OCR


def extract(path: str | Path) -> DocumentExtract:
    """Preflight and tier-1/2 extraction for one document (steps 2 and 6)."""
    path = Path(path)
    doc = pymupdf.open(path)
    out = DocumentExtract(path=path)

    for i, page in enumerate(doc):
        text = page.get_text()
        vector_ops = len(page.get_drawings())
        images = len(page.get_images(full=True))
        tier = classify_tier(len(text.strip()), vector_ops, images)

        m = re.search(r"\b([A-Z]{1,3}-?\d{1,3}[a-z]?)\b", text)
        ref = SheetRef(
            document_id=path.name, page=i + 1, sheet_no=m.group(1) if m else None
        )
        sheet = SheetExtract(
            ref=ref,
            tier=tier,
            text=text,
            text_chars=len(text.strip()),
            vector_ops=vector_ops,
            images=images,
            rebar_mentions=sorted({x.strip() for x in REBAR_PATTERN.findall(text)}),
            has_text_layer=bool(text.strip()),
        )
        if sheet.needs_vision:
            sheet.notes.append(
                "vector page with no extractable rebar callouts: annotation is "
                "drawn as outlines; render at 300dpi and route to the vision tier"
            )
        out.sheets.append(sheet)

    doc.close()
    return out


def render_for_vision(path: str | Path, page_no: int, dpi: int = 300,
                      clip: tuple[float, float, float, float] | None = None) -> bytes:
    """Render a region at high DPI from the vector source, never a downscale."""
    doc = pymupdf.open(path)
    page = doc[page_no - 1]
    rect = pymupdf.Rect(*clip) if clip else None
    pix = page.get_pixmap(dpi=dpi, clip=rect)
    data = pix.tobytes("png")
    doc.close()
    return data
