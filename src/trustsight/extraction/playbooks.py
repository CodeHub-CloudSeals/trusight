"""Playbook selection.

Two element families are implemented. Choosing between them is itself a
governed decision: a set that matches neither playbook must be reported as
unreadable, not pushed through the nearest reader. Running the pile playbook
over a footing sketch does not fail loudly — it finds nothing and returns an
empty result, which reads like "this drawing has no reinforcement" rather
than "TrustSight cannot read this drawing yet". Those are very different
statements to put in front of an estimator.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pymupdf

from ..models.core import BarSize
from .drawings import (
    COUNT_SIZE, DIAMETER, ExtractionResult, read_pile_project,
)
from .footings import BAR_CALLOUT, read_footing_project


@dataclass(frozen=True)
class Playbook:
    name: str
    element_family: str
    reader: Callable[[str | Path, str], ExtractionResult]
    description: str


PILE_V1 = Playbook(
    "pile_v1", "pile", read_pile_project,
    "Pile schedule with section and detail callouts.")
FOOTING_V1 = Playbook(
    "footing_v1", "footing", read_footing_project,
    "Pad footing sketch with a mat reinforcement callout.")

PLAYBOOKS = (PILE_V1, FOOTING_V1)


class NoPlaybook(LookupError):
    """No implemented playbook recognises this drawing set."""


def _sample_text(source: Path, max_pages: int = 24) -> str:
    """Enough text from the set to recognise the drawing family."""
    paths = (sorted(source.glob("Input*.pdf")) or sorted(source.glob("*.pdf"))
             if source.is_dir() else [source])
    chunks: list[str] = []
    budget = max_pages
    for path in paths:
        if budget <= 0:
            break
        doc = pymupdf.open(path)
        for page in doc:
            if budget <= 0:
                break
            chunks.append(page.get_text())
            budget -= 1
        doc.close()
    return " ".join(chunks).upper()


def _valid_size(token: str) -> bool:
    return token.upper() in {s.value for s in BarSize}


def select(source: str | Path) -> Playbook:
    """Pick the playbook whose anchors this set actually contains.

    The test is the anchor each reader genuinely depends on, not a keyword
    that happens to appear nearby. Matching on the word "FOUNDATION" alone
    claims three of the five reference projects and delivers one: a selector
    that promises more than its reader can read is the same overclaim the
    engine refuses to make about a quantity.
    """
    text = _sample_text(Path(source))

    # pile_v1 resolves through a schedule table anchored on the word
    # SCHEDULE, a diameter cell, and a count-size callout such as "12-30M".
    if ("SCHEDULE" in text and DIAMETER.search(text)
            and any(_valid_size(size) for _n, size in COUNT_SIZE.findall(text))):
        return PILE_V1

    # footing_v1 resolves through a foundation sketch carrying a mat callout
    # such as "6 - 20M BARS (TOP & BOTTOM, EW)".
    if "FOUNDATION" in text:
        match = BAR_CALLOUT.search(" ".join(text.split()))
        if match and _valid_size(match.group(2)):
            return FOOTING_V1

    raise NoPlaybook(
        "no implemented playbook recognises this drawing set: it carries "
        "neither a pile schedule with a diameter and count callout, nor a "
        "foundation sketch with a mat reinforcement callout. Reading it needs "
        "a new element playbook, so nothing is extracted rather than "
        "reporting an empty drawing."
    )


def read(source: str | Path, project_id: str) -> tuple[ExtractionResult, Playbook]:
    playbook = select(source)
    return playbook.reader(source, project_id), playbook
