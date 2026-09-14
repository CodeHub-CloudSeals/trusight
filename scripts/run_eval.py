#!/usr/bin/env python3
"""Run the evaluation harness against the reference corpus.

    python scripts/run_eval.py <corpus_root> [--json out.json]

Exit code is non-zero if any reference item fails shape verification, so the
harness can gate CI.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trustsight.eval import harness  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus", help="directory containing <project>/Output*.pdf")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()

    results, totals = harness.run(args.corpus)
    print(harness.report(results, totals))

    if args.json_out:
        Path(args.json_out).write_text(harness.to_json(results, totals))
        print(f"\nwrote {args.json_out}")

    return 0 if totals["shape_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
