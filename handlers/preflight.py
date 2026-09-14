"""Step 2: preflight and ingestion."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from .common import DRAWINGS_BUCKET, put_evidence, s3

from trustsight.extraction.pdf import extract  # noqa: E402


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    run_id = event["run_id"]
    sheets: list[dict[str, Any]] = []

    for key in event["keys"]:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            s3().download_fileobj(DRAWINGS_BUCKET, key, tmp)
            local = Path(tmp.name)
        doc = extract(local)
        for sheet in doc.sheets:
            sheets.append(
                {
                    "key": key,
                    "page": sheet.ref.page,
                    "sheet_no": sheet.ref.sheet_no,
                    "tier": sheet.tier.value,
                    "text_chars": sheet.text_chars,
                    "vector_ops": sheet.vector_ops,
                    "rebar_mentions": sheet.rebar_mentions,
                    "needs_vision": sheet.needs_vision,
                    "notes": sheet.notes,
                }
            )
        put_evidence(
            {
                "run_id": run_id,
                "record_id": f"preflight#{key}",
                "claim_type": "extraction",
                "subject": key,
                "value": {"tier": doc.tier.value, "sheets": len(doc.sheets)},
            }
        )

    return {
        "run_id": run_id,
        "sheets": sheets,
        "needs_vision": any(s["needs_vision"] for s in sheets),
        "unreadable": [s["key"] for s in sheets if s["tier"] == "ocr" and not s["text_chars"]],
    }
