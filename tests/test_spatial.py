"""3D instantiation. The rendered bar path must equal the cutting length."""
from trustsight.engine.spatial import bar_polyline
from trustsight.models.core import BarSize, RebarRole, Reinforcement


def _bar(bend_type, legs):
    return Reinforcement(
        bar_size=BarSize.M20, role=RebarRole.LONGITUDINAL, count=6,
        bend_type=bend_type, legs=legs, cover_mm=75, lap_mm=0,
    )


def test_hairpin_path_matches_cutting_length():
    points, total = bar_polyline(_bar("17", {"B": 400, "C": 1120, "D": 400}))
    assert total == 1920
    assert len(points) == 4


def test_t3_path_excludes_column_o():
    _, total = bar_polyline(_bar("T3", {"A": 140, "B": 140, "C": 2545, "G": 300, "O": 810}))
    assert total == 3125


def test_straight_bar():
    _, total = bar_polyline(_bar(None, {"B": 9000}))
    assert total == 9000


def test_unknown_shape_returns_none_rather_than_guessing():
    assert bar_polyline(_bar("ZZ", {"A": 1})) is None
