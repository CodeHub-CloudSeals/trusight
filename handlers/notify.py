"""Steps 12 and 19: park the run and notify a reviewer.

Invoked by Step Functions with ``waitForTaskToken``. The token is persisted
with the open questions; the run then holds no compute until the estimator
answers through POST /runs/{id}/approve, which calls SendTaskSuccess.

There is no timeout-to-approve path. A 14-day timeout fails the branch; it
never approves by default (invariant I2).
"""
from __future__ import annotations

from typing import Any

from .common import RUNS_TABLE, ddb, put_evidence


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    run_id = event["run_id"]
    token = event["task_token"]
    questions = event.get("questions", [])

    if RUNS_TABLE:
        ddb().Table(RUNS_TABLE).put_item(
            Item={
                "run_id": run_id,
                "state": "suspended",
                "task_token": token,
                "open_questions": questions,
            }
        )

    put_evidence(
        {
            "run_id": run_id,
            "record_id": f"suspend#{run_id}",
            "claim_type": "link",
            "subject": "workflow",
            "value": {"suspended_on": len(questions), "questions": questions},
        }
    )

    # TODO: notify the reviewer (SES or Slack). The run stays parked either way.
    return {"suspended": True, "questions": len(questions)}
