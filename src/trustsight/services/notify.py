"""Alerts (spec v2 NOTIFY-101).

Two alerts are named in the backlog: *clarification required* and *BBS ready*.
Both are things a person needs to be told about a run they are not watching.

What this build will not do is claim to have sent an email. No SMTP is
configured, no Teams webhook is registered, and a product whose entire
argument is "we do not assert what we cannot show" must not print "sent to
tom.keller@demo-client.com" next to a message that went nowhere. So every
alert carries its delivery state explicitly, and in this build that state is
``in_app`` with the reason transport is unavailable. Wiring SES or a Teams
webhook later changes the transport and nothing else — the alert, its
audience and its trigger are already here.

Alerts are derived from run state rather than queued by a side effect. That
is deliberate: a queue can drift from the thing it describes, and an alert
that says "clarification required" after the clarification was answered is
worse than no alert. First-seen and read timestamps are kept per run so an
alert still has a raise time and can be dismissed.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

#: Who each alert is for, by role capability. An alert nobody can act on is
#: noise, so "clarification required" goes to the roles that may answer one.
AUDIENCE = {
    "clarification_required": "clarify",
    "bbs_ready": "export",
    "approval_required": "approve",
}

TRANSPORT_NOTE = (
    "Delivered in this workspace. Email and Teams transport are not "
    "configured in this build, so nothing was sent outside it.")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class AlertState:
    """Per-run memory: when an alert was first seen, and whether it was read."""

    first_seen: dict[str, str] = field(default_factory=dict)
    read: set[str] = field(default_factory=set)


def _key(kind: str, detail: str) -> str:
    return kind + ":" + hashlib.sha256(detail.encode()).hexdigest()[:10]


def current(run: Any, ctx: Any, released_mass_kg: float | None = None) -> list[dict[str, Any]]:
    """The alerts true of this run right now."""
    state: AlertState = getattr(ctx, "alerts", None) or AlertState()
    if getattr(ctx, "alerts", None) is None:
        try:
            ctx.alerts = state
        except Exception:                      # a frozen context in a test
            pass

    alerts: list[dict[str, Any]] = []

    questions = list(getattr(ctx, "questions", []) or [])
    if questions:
        # field names carry their unit as a suffix (run_length_mm); that is
        # right in a payload and reads as a typo in a sentence
        def _human(name: str) -> str:
            return name.removesuffix("_mm").removesuffix("_kg").replace("_", " ")

        fields = ", ".join(sorted({_human(q["field"]) for q in questions}))
        alerts.append({
            "kind": "clarification_required",
            "severity": "action",
            "title": (f"{len(questions)} clarification"
                      f"{'' if len(questions) == 1 else 's'} required"),
            "body": (f"The drawing does not state {fields}. Nothing has been "
                     f"assumed and the affected quantities are held."),
            "goto": "review",
            "detail": fields,
        })

    if getattr(run, "pending_step", None) == 19 and not ctx.rulebook.is_approved():
        alerts.append({
            "kind": "approval_required",
            "severity": "action",
            "title": "Rulebook awaiting signature",
            "body": ("Every quantity is calculated. None of it can release "
                     "until an engineer signs the assumption sheet."),
            "goto": "review",
            "detail": ctx.rulebook.version,
        })

    if released_mass_kg:
        alerts.append({
            "kind": "bbs_ready",
            "severity": "ready",
            "title": "Bar bending schedule ready",
            "body": (f"{released_mass_kg:,.1f} kg has released and the output "
                     f"pack can be generated. Held lines are excluded."),
            "goto": "results",
            "detail": f"{released_mass_kg:.1f}",
        })

    out = []
    for a in alerts:
        key = _key(a["kind"], a["detail"])
        state.first_seen.setdefault(key, _now())
        out.append({
            **a,
            "id": key,
            "raised_at": state.first_seen[key],
            "read": key in state.read,
            "audience_capability": AUDIENCE.get(a["kind"], "export"),
            "delivery": {"channel": "in_app", "sent_externally": False,
                         "note": TRANSPORT_NOTE},
        })
    return out


def mark_read(ctx: Any, alert_id: str) -> None:
    state: AlertState = getattr(ctx, "alerts", None) or AlertState()
    state.read.add(alert_id)
    try:
        ctx.alerts = state
    except Exception:
        pass
