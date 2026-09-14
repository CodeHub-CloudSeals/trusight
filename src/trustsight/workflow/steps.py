"""The 23 pipeline steps, classified by execution type. Spec section 6.

The classification is not documentation: ``StepKind`` drives cost, retry
policy and whether a step may suspend the run. Of 23 steps, five invoke a
language model generatively and two suspend for a human. The rest are
ordinary software.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class StepKind(str, Enum):
    DETERMINISTIC = "deterministic"
    LLM = "llm"
    VISION = "vision"
    GEOMETRY = "geometry"
    HUMAN = "human"          # suspends the run
    MIXED = "mixed"


@dataclass(frozen=True)
class StepDef:
    no: int
    name: str
    kind: StepKind
    suspends: bool = False
    note: str = ""


STEPS: tuple[StepDef, ...] = (
    StepDef(1, "receive_drawings", StepKind.DETERMINISTIC, note="hash and store originals immutably"),
    StepDef(2, "preflight", StepKind.DETERMINISTIC, note="vector/raster, text layer, revision"),
    StepDef(3, "sheet_classification", StepKind.LLM, note="title-block regex fallback"),
    StepDef(4, "element_detection", StepKind.VISION, note="vector paths first; VLM only without text layer"),
    StepDef(5, "playbook_retrieval", StepKind.DETERMINISTIC, note="keyed lookup, no generation"),
    StepDef(6, "extraction", StepKind.MIXED, note="tier order in spec s8"),
    StepDef(7, "knowledge_graph", StepKind.DETERMINISTIC, note="typed writes only"),
    StepDef(8, "cross_sheet_resolution", StepKind.MIXED, note="LLM proposes, merge rules decide"),
    StepDef(9, "reinforcement_interpretation", StepKind.LLM, note="schema-validated output"),
    StepDef(10, "missing_conflict_check", StepKind.DETERMINISTIC, note="pure function over the graph"),
    StepDef(11, "clarification", StepKind.LLM, note="generates the question, never the answer"),
    StepDef(12, "human_input", StepKind.HUMAN, suspends=True, note="may wait days"),
    StepDef(13, "approved_knowledge", StepKind.DETERMINISTIC, note="versioned write with scope predicate"),
    StepDef(14, "rulebook", StepKind.DETERMINISTIC, note="versioned reference data"),
    StepDef(15, "standards_mapping", StepKind.DETERMINISTIC, note="status enum only"),
    StepDef(16, "deterministic_calculation", StepKind.DETERMINISTIC, note="invariant I1: no LLM"),
    StepDef(17, "confidence_gates", StepKind.DETERMINISTIC, note="gate vector, spec s5"),
    StepDef(18, "qa", StepKind.MIXED, note="range checks deterministic, narrative LLM"),
    StepDef(19, "approval_gate", StepKind.HUMAN, suspends=True, note="policy-selected reviewer"),
    StepDef(20, "generate_bbs", StepKind.DETERMINISTIC),
    StepDef(21, "evidence_fabric", StepKind.DETERMINISTIC, note="append-only, spec s10"),
    StepDef(22, "handover", StepKind.DETERMINISTIC, note="CSV/PDF/BOQ export"),
    StepDef(23, "continuous_learning", StepKind.DETERMINISTIC, note="approved corrections only"),
)

BY_NAME = {s.name: s for s in STEPS}
SUSPENDING = tuple(s for s in STEPS if s.suspends)
GENERATIVE = tuple(s for s in STEPS if s.kind in (StepKind.LLM, StepKind.VISION))


def summary() -> dict[str, int]:
    out: dict[str, int] = {}
    for s in STEPS:
        out[s.kind.value] = out.get(s.kind.value, 0) + 1
    return out
