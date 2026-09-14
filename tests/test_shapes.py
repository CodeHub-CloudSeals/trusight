"""The shape catalogue is the most error-prone part of the engine.

These tests encode the two failure modes a naive implementation hits:
populated columns that are not legs, and a bar mark that maps to different
leg sets under different bend types.
"""
import pytest

from trustsight.shapes.catalogue import ShapeResolutionError, infer_from_sum, resolve, verify


def test_straight_bar_carries_length_in_column_b():
    ok, msg = verify(None, {"B": 9000}, 9000)
    assert ok, msg


def test_type2_single_hook():
    ok, _ = verify("2", {"A": 260, "B": 1000}, 1260)
    assert ok


def test_type2_double_hook_uses_g_not_c():
    ok, _ = verify("2", {"A": 260, "B": 4650, "G": 260}, 5170)
    assert ok


def test_type17_hairpin():
    ok, _ = verify("17", {"B": 400, "C": 1120, "D": 400}, 1920)
    assert ok


def test_t3_excludes_column_o():
    """Column O holds 810mm and must NOT enter the cutting length."""
    legs = {"A": 140, "B": 140, "C": 2545, "G": 300, "O": 810}
    ok, _ = verify("T3", legs, 3125)
    assert ok
    assert sum(legs.values()) == 3935, "naive sum would be wrong by 810mm"


def test_b16a_excludes_h_and_k():
    legs = {"A": 270, "B": 375, "C": 295, "D": 470, "E": 270, "H": 325, "K": 320}
    ok, _ = verify("B16A", legs, 1680)
    assert ok
    assert sum(legs.values()) == 2325, "naive sum would be wrong by 645mm"


def test_same_mark_different_bend_types_resolve_differently():
    """15A01 is type 17 (B+C+D) in one project and T3 (A+B+C+G) in another."""
    assert resolve("17", {"B": 625, "C": 500, "D": 625}).leg_columns == ("B", "C", "D")
    assert resolve("T3", {"A": 140, "B": 140, "C": 2545, "G": 300, "O": 810}).leg_columns \
        == ("A", "B", "C", "G")


def test_unknown_bend_type_raises_rather_than_guessing():
    with pytest.raises(ShapeResolutionError):
        resolve("ZZ99", {"A": 100, "B": 200})


def test_infer_is_diagnostic_only():
    assert infer_from_sum({"A": 260, "B": 1000}, 1260) == ("A", "B")
    assert infer_from_sum({"A": 1, "B": 2}, 999) is None
