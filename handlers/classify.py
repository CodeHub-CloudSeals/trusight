"""Lambda entry point: classify.

Wraps the same service function the local runner uses, so cloud and local
behaviour cannot drift. See handlers/preflight.py and handlers/notify.py for
the full pattern including evidence persistence.
"""
from __future__ import annotations

from typing import Any

from .common import idempotency_key, put_evidence


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Bind trustsight.services.pipeline handlers to Lambda.

    Implementation order per the remediation plan: complete the local
    five-screen demo first, then wrap these same functions. Raising here is
    deliberate — a stub that returns a plausible empty result would let a
    cloud run report success having done nothing.
    """
    raise NotImplementedError(
        "classify: bind to trustsight.services.pipeline after the local demo is "
        "stable (remediation plan s12.3)"
    )
