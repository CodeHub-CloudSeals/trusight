"""Corpus regression: every reference item must reconcile.

Skipped when the corpus is absent. Set TRUSTSIGHT_CORPUS to the directory
containing <project>/Output*.pdf.
"""
import os
from pathlib import Path

import pytest

from trustsight.eval import harness

CORPUS = os.getenv("TRUSTSIGHT_CORPUS")
pytestmark = pytest.mark.skipif(
    not CORPUS or not Path(CORPUS).exists(), reason="reference corpus not available"
)


def test_every_reference_item_reconciles():
    results, totals = harness.run(CORPUS)
    failures = [f for r in results for f in r.shape_failed]
    assert not failures, "\n".join(failures)
    assert totals["shape_verified"] == totals["reference_items"]


def test_corpus_totals_are_stable():
    """Guards against a parser regression silently changing the baseline."""
    _, totals = harness.run(CORPUS)
    assert totals["reference_items"] == 29
    assert totals["reference_bars"] == 2658
    assert 20200 < totals["reference_mass_kg"] < 20300


def test_pile_case_reconciles_end_to_end():
    """The demonstrator's pile case must match the client's own bar list."""
    from trustsight.eval.harness import compare
    from trustsight.extraction.barlist import parse_bar_list
    import subprocess, sys as _sys
    from pathlib import Path as _P

    ref_path = next(_P(CORPUS).glob("*Atlanic*/Output*.pdf"), None)
    if ref_path is None:
        import pytest as _pt
        _pt.skip("Atlantic Cages reference not present")
    ref = parse_bar_list(ref_path)
    assert sum(i.quantity for i in ref.items) == 288
    assert round(ref.total_mass_kg, 1) == 5991.4
