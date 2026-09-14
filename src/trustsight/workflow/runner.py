"""Durable run state.

Steps 12 and 19 suspend pending human action, potentially for days. That
makes the pipeline a long-running workflow, not a request-response loop.
This module holds the state machine; the AWS deployment binds it to Step
Functions, whose ``waitForTaskToken`` integration is the natural primitive
for an approval gate (see infra/).

Every step is keyed by (run_id, step_no, input_hash) so replay is safe.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

from .steps import BY_NAME, STEPS, StepDef


class RunState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUSPENDED = "suspended"      # waiting on a human
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"          # some elements done, some flagged — normal


@dataclass
class StepMetrics:
    """Per-step telemetry. ROI is measured, never claimed."""

    duration_ms: int = 0
    pages_or_items_processed: int = 0
    values_extracted: int = 0
    exceptions_raised: int = 0
    human_seconds: int = 0
    reused_knowledge_count: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "duration_ms": self.duration_ms,
            "pages_or_items_processed": self.pages_or_items_processed,
            "values_extracted": self.values_extracted,
            "exceptions_raised": self.exceptions_raised,
            "human_seconds": self.human_seconds,
            "reused_knowledge_count": self.reused_knowledge_count,
        }


@dataclass
class StepResult:
    step_no: int
    name: str
    ok: bool
    output: Any = None
    error: str | None = None
    input_hash: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    metrics: StepMetrics = field(default_factory=StepMetrics)


@dataclass
class Run:
    project_id: str
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    state: RunState = RunState.PENDING
    #: recorded so a run can be replayed deterministically (spec s7.1)
    versions: dict[str, str] = field(default_factory=dict)
    results: dict[int, StepResult] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    pending_token: str | None = None
    pending_step: int | None = None

    def completed_steps(self) -> list[int]:
        return sorted(n for n, r in self.results.items() if r.ok)

    def roi(self) -> dict[str, Any]:
        """Measured telemetry across the run. No invented percentages."""
        steps = [
            {
                "step": r.step_no,
                "name": r.name,
                "ok": r.ok,
                **r.metrics.as_dict(),
            }
            for r in sorted(self.results.values(), key=lambda x: x.step_no)
        ]
        return {
            "run_id": self.run_id,
            "steps": steps,
            "machine_ms": sum(s["duration_ms"] for s in steps),
            "human_seconds": sum(s["human_seconds"] for s in steps),
            "exceptions": sum(s["exceptions_raised"] for s in steps),
            "values_extracted": sum(s["values_extracted"] for s in steps),
            "reused_knowledge": sum(s["reused_knowledge_count"] for s in steps),
        }

    def is_done(self, step_no: int) -> bool:
        r = self.results.get(step_no)
        return bool(r and r.ok)


def input_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


class WorkflowRunner:
    """Executes steps in order, skipping any already completed (idempotence).

    Handlers are registered by step name. A handler that raises ``Suspend``
    parks the run; resuming replays from the suspended step with the human
    answer merged into the context.
    """

    class Suspend(Exception):
        def __init__(self, token: str, question: Any = None):
            self.token = token
            self.question = question

    def __init__(self, strict: bool = True) -> None:
        """``strict`` refuses to run when a step has no handler.

        Silently skipping is convenient while scaffolding and dangerous in a
        client demo: a run can report "completed" having skipped
        interpretation entirely. Demo and production use strict=True; only
        tests and development may relax it.
        """
        self.strict = strict
        self._handlers: dict[str, Callable[[Run], Any]] = {}

    def handler(self, name: str) -> Callable:
        if name not in BY_NAME:
            raise KeyError(f"{name!r} is not one of the 23 defined steps")

        def register(fn: Callable[[Run], Any]) -> Callable:
            self._handlers[name] = fn
            return fn

        return register

    def execute(self, run: Run, upto: int = 23) -> Run:
        run.state = RunState.RUNNING
        for step in STEPS:
            if step.no > upto:
                break
            if run.is_done(step.no):
                continue  # idempotent replay
            fn = self._handlers.get(step.name)
            if fn is None:
                if self.strict:
                    run.state = RunState.FAILED
                    run.results[step.no] = StepResult(
                        step_no=step.no, name=step.name, ok=False,
                        error=f"step {step.no} {step.name!r} has no registered handler",
                        finished_at=datetime.now(timezone.utc),
                    )
                    return run
                continue
            result = StepResult(
                step_no=step.no, name=step.name, ok=False,
                input_hash=input_hash(run.context),
            )
            try:
                result.output = fn(run)
                result.ok = True
                if isinstance(result.output, dict):
                    m = result.output.get("_metrics")
                    if isinstance(m, dict):
                        for k, v in m.items():
                            if hasattr(result.metrics, k):
                                setattr(result.metrics, k, v)
            except WorkflowRunner.Suspend as susp:
                run.state = RunState.SUSPENDED
                run.pending_token = susp.token
                run.pending_step = step.no
                run.context.setdefault("open_questions", []).append(susp.question)
                return run
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                result.error = f"{type(exc).__name__}: {exc}"
                run.state = RunState.FAILED
                result.finished_at = datetime.now(timezone.utc)
                run.results[step.no] = result
                return run
            result.finished_at = datetime.now(timezone.utc)
            result.metrics.duration_ms = int(
                (result.finished_at - result.started_at).total_seconds() * 1000
            )
            run.results[step.no] = result

        run.state = (
            RunState.PARTIAL if run.context.get("exceptions") else RunState.COMPLETED
        )
        return run

    def resume(self, run: Run, token: str, answer: Any) -> Run:
        """Deliver a human answer and continue. No default approval, ever."""
        if run.state is not RunState.SUSPENDED or run.pending_token != token:
            raise RuntimeError("run is not suspended on this token")
        run.context.setdefault("answers", []).append(answer)
        if run.pending_step is not None:
            run.results[run.pending_step] = StepResult(
                step_no=run.pending_step,
                name=next(s.name for s in STEPS if s.no == run.pending_step),
                ok=True,
                output=answer,
                finished_at=datetime.now(timezone.utc),
            )
        run.pending_token = None
        run.pending_step = None
        return self.execute(run)
