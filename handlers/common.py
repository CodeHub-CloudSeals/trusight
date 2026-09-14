from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import boto3  # noqa: E402

EVIDENCE_TABLE = os.environ.get("EVIDENCE_TABLE", "")
GRAPH_TABLE = os.environ.get("GRAPH_TABLE", "")
RUNS_TABLE = os.environ.get("RUNS_TABLE", "")
DRAWINGS_BUCKET = os.environ.get("DRAWINGS_BUCKET", "")

_ddb = None
_s3 = None


def ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb")
    return _ddb


def s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def idempotency_key(run_id: str, step_no: int, payload: Any) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    return f"{run_id}#{step_no}#{digest}"


class EvidenceOverwrite(RuntimeError):
    """An evidence record already exists under this key."""


def put_evidence(record: dict[str, Any]) -> None:
    """Append-only, enforced twice.

    IAM denies UpdateItem and DeleteItem, but PutItem alone can still
    overwrite an item with the same key. The condition expression closes
    that gap: a second write to the same (run_id, record_id) is rejected by
    DynamoDB rather than silently replacing prior evidence.
    """
    if not EVIDENCE_TABLE:
        return
    table = ddb().Table(EVIDENCE_TABLE)
    try:
        table.put_item(
            Item=record,
            ConditionExpression=(
                "attribute_not_exists(run_id) AND attribute_not_exists(record_id)"
            ),
        )
    except Exception as exc:  # ClientError: ConditionalCheckFailedException
        if "ConditionalCheckFailed" in str(exc):
            raise EvidenceOverwrite(
                f"evidence record {record.get('record_id')} already exists for "
                f"run {record.get('run_id')}; evidence is append-only"
            ) from exc
        raise
