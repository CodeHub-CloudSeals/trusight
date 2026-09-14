"""The five generative agents. Spec section 6.

Of the 23 pipeline steps, only these involve a language model in a
generative capacity. Everything else is ordinary software. That ratio is the
strongest argument that the output is reproducible, and it should be stated
to the client rather than hidden.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .base import StructuredAgent


# -- step 3: sheet classification -----------------------------------------
class SheetClass(BaseModel):
    sheet_no: str | None = None
    discipline: Literal["structural", "architectural", "civil", "mechanical",
                        "electrical", "landscape", "unknown"]
    sheet_type: Literal["plan", "section", "detail", "schedule", "notes",
                        "cover", "other"]
    revision: str | None = None
    contains_reinforcement: bool
    rationale: str


class SheetClassifier(StructuredAgent[SheetClass]):
    name = "sheet_classifier"
    output_model = SheetClass

    def system_prompt(self) -> str:
        return (
            "You classify construction drawing sheets. You are given the "
            "sheet's extracted text and title block. Decide the discipline "
            "and sheet type, and whether the sheet carries any reinforcement "
            "information at all. A sheet with no rebar content is excluded "
            "from downstream extraction, so a false positive costs review "
            "time and a false negative loses steel."
        )

    def user_prompt(self, payload: dict[str, Any]) -> str:
        return f"Sheet text:\n---\n{payload['text'][:6000]}\n---"


# -- step 9: reinforcement interpretation ---------------------------------
class RebarCallout(BaseModel):
    bar_size: str = Field(description="e.g. 10M, 15M, 20M, 30M")
    role: Literal["longitudinal", "tie", "stirrup", "spiral", "top", "bottom",
                  "each_way", "dowel"]
    count: int | None = Field(None, description="null unless the drawing states a count")
    spacing_mm: int | None = Field(None, description="null unless spacing is stated")
    direction: str | None = None
    layer: str | None = None
    cover_mm: int | None = Field(None, description="null unless cover is stated ON THIS SHEET")
    verbatim: str = Field(description="the callout text exactly as written")


class ReinforcementRead(BaseModel):
    element_mark: str | None = None
    element_type: str | None = None
    callouts: list[RebarCallout] = Field(default_factory=list)
    unstated_fields: list[str] = Field(
        default_factory=list,
        description="fields a reader would expect but which this sheet does not state",
    )


class ReinforcementInterpreter(StructuredAgent[ReinforcementRead]):
    name = "reinforcement_interpreter"
    output_model = ReinforcementRead

    def system_prompt(self) -> str:
        return (
            "You read reinforcement callouts from structural drawings, "
            "following the supplied interpretation playbook exactly.\n\n"
            "Notation: '15M @ 300 O.C.' is a 15M bar at 300 mm on centre. "
            "'12-30M' is twelve 30M bars. 'E.W.' is each way. 'TYP.' means "
            "the callout repeats for similar conditions.\n\n"
            "You transcribe; you do not estimate. If the sheet does not state "
            "cover, spacing or a count, return null for it and name it in "
            "unstated_fields. A plausible guess is the most expensive error "
            "this system can make."
        )

    def user_prompt(self, payload: dict[str, Any]) -> str:
        pb = payload.get("playbook", "(no playbook supplied)")
        return (
            f"Interpretation playbook:\n{pb}\n\n"
            f"Sheet region text:\n---\n{payload['text'][:6000]}\n---"
        )


# -- step 8: cross-sheet resolution ---------------------------------------
class ProposedLink(BaseModel):
    from_sheet: str
    to_sheet: str
    kind: Literal["detail_of", "section_of", "schedule_for", "note_applies_to"]
    element_mark: str | None = None
    evidence: str = Field(description="the reference text that justifies the link")
    confidence: Literal["high", "medium", "low"]


class CrossSheetLinks(BaseModel):
    links: list[ProposedLink] = Field(default_factory=list)


class CrossSheetResolver(StructuredAgent[CrossSheetLinks]):
    name = "cross_sheet_resolver"
    output_model = CrossSheetLinks

    def system_prompt(self) -> str:
        return (
            "You propose links between drawing sheets by following explicit "
            "references such as 'SEE DETAIL 3/S-12' or a section marker. You "
            "propose only; the merge rules decide what is accepted. Never "
            "invent a link that no reference text supports."
        )

    def user_prompt(self, payload: dict[str, Any]) -> str:
        return f"Sheets and their reference text:\n{payload['sheets']}"


# -- step 11: clarification ------------------------------------------------
class ClarificationQuestion(BaseModel):
    field_name: str
    element_mark: str | None = None
    question: str = Field(description="one precise question an estimator can answer quickly")
    why_needed: str
    candidate_values: list[str] = Field(
        default_factory=list,
        description="options to offer; never a recommendation",
    )
    blocking: bool


class ClarificationSet(BaseModel):
    questions: list[ClarificationQuestion] = Field(default_factory=list)


class ClarificationAgent(StructuredAgent[ClarificationSet]):
    name = "clarification_agent"
    output_model = ClarificationSet

    def system_prompt(self) -> str:
        return (
            "You turn unresolved facts into precise questions for a "
            "structural estimator. One question per missing fact. State which "
            "element and which field. Offer candidate values only where the "
            "project rulebook lists them; never recommend one, and never "
            "answer the question yourself. The estimator's time is the "
            "scarcest resource in the workflow: no question should require "
            "them to open a drawing to understand what is being asked."
        )

    def user_prompt(self, payload: dict[str, Any]) -> str:
        return f"Unresolved facts:\n{payload['unknowns']}"


# -- step 18: QA ------------------------------------------------------------
class Anomaly(BaseModel):
    item_ref: str
    severity: Literal["info", "warning", "error"]
    finding: str


class QAReport(BaseModel):
    anomalies: list[Anomaly] = Field(default_factory=list)
    summary: str


class QAAgent(StructuredAgent[QAReport]):
    name = "qa_agent"
    output_model = QAReport

    def system_prompt(self) -> str:
        return (
            "You review a generated bar bending schedule for anomalies an "
            "experienced estimator would notice: quantities out of proportion "
            "to the element, cutting lengths exceeding stock length without a "
            "splice, bar sizes unusual for the element type, totals "
            "inconsistent with the element count. You report; you do not "
            "correct. Deterministic range checks run separately — your job is "
            "the judgement call a rule cannot express."
        )

    def user_prompt(self, payload: dict[str, Any]) -> str:
        return f"Generated schedule:\n{payload['schedule']}\n\nElements:\n{payload['elements']}"
