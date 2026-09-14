"""Gate vector policy. Spec section 5 — a vector, never a blended score."""
from trustsight.models.core import CountingPattern, Decision, GateVector, SourceTier


def _all_pass(**over) -> GateVector:
    base = dict(
        g1_fields_complete=True, g2_no_conflict=True, g3_rule_resolved=True,
        g4_source_quality=SourceTier.NATIVE_TEXT, g5_pattern_known=True,
        g6_within_bounds=True,
    )
    base.update(over)
    return GateVector(**base)


def test_all_gates_pass_auto_proceeds():
    assert _all_pass().decide() is Decision.AUTO_PROCEED


def test_conflict_blocks_and_is_never_auto_resolved():
    assert _all_pass(g2_no_conflict=False).decide() is Decision.BLOCK


def test_missing_field_becomes_a_question():
    assert _all_pass(g1_fields_complete=False).decide() is Decision.CLARIFY


def test_ocr_source_cannot_auto_proceed():
    assert _all_pass(g4_source_quality=SourceTier.OCR).decide() is Decision.REVIEW


def test_vision_source_cannot_auto_proceed():
    assert _all_pass(g4_source_quality=SourceTier.VISION).decide() is Decision.REVIEW


def test_area_derived_counts_always_review():
    assert CountingPattern.AREA_DIVIDED_BY_SPACING.always_review
    assert _all_pass(force_review=True).decide() is Decision.REVIEW


def test_explanation_names_the_failing_gate():
    assert "g3_rule_resolved" in _all_pass(g3_rule_resolved=False).explain()


def test_unstated_cover_on_a_tie_becomes_a_question():
    """A spiral dimensioned from the concrete face needs cover; absent it,
    the item must CLARIFY rather than proceed on a default."""
    from trustsight.models.core import BarSize, RebarRole, Reinforcement

    tie = Reinforcement(
        bar_size=BarSize.M15, role=RebarRole.SPIRAL, count=36,
        spacing_mm=350, run_length_mm=11955, cover_mm=None,
    )
    assert "cover_mm" in tie.unknown_fields

    longitudinal = Reinforcement(
        bar_size=BarSize.M30, role=RebarRole.LONGITUDINAL, count=12, cover_mm=None,
    )
    assert "cover_mm" not in longitudinal.unknown_fields
