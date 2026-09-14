"""3D spatial interpretation. Spec section 9.

Method is **instantiation, not reconstruction**. Every parameter comes from
validated knowledge-graph data, so the view cannot introduce information —
it can only reveal what the graph already contains, including its gaps
(invariant I4). That is what makes it a completeness check rather than
decoration, and what makes "not BIM-grade" structurally true rather than a
disclaimer.

Reinforcement geometry is nearly free: the bend type gives the leg sequence
and the leg columns give their lengths, so each bar is a polyline whose
segment lengths must sum to the already-computed cutting length. The
geometry is therefore self-checking.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..graph.knowledge_graph import ProjectKnowledgeGraph
from ..models.core import Element, ElementType, Reinforcement
from ..shapes.catalogue import ShapeResolutionError, resolve

#: element type -> primitive and the parameters it requires
PRIMITIVES: dict[ElementType, tuple[str, tuple[str, ...]]] = {
    ElementType.PILE: ("cylinder", ("diameter", "length")),
    ElementType.COLUMN: ("box", ("width", "depth", "height")),
    ElementType.PIER: ("box", ("width", "depth", "height")),
    ElementType.FOOTING: ("box", ("width", "depth", "thickness")),
    ElementType.PILE_CAP: ("box", ("width", "depth", "thickness")),
    ElementType.SLAB: ("extrusion", ("thickness",)),
    ElementType.WALL: ("extrusion", ("thickness", "height")),
    ElementType.BEAM: ("swept_section", ("width", "depth", "span")),
    ElementType.GRADE_BEAM: ("swept_section", ("width", "depth", "span")),
    ElementType.ABUTMENT: ("extrusion", ("thickness", "height")),
}


@dataclass
class SceneNode:
    element_key: str
    element_type: str
    primitive: str
    params: dict[str, float]
    placements: list[dict[str, float]] = field(default_factory=list)
    bars: list[dict[str, Any]] = field(default_factory=list)
    state: str = "ok"                 # ok | unknown | conflicted | missing_location
    issues: list[str] = field(default_factory=list)


@dataclass
class Scene:
    project_id: str
    nodes: list[SceneNode] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "nodes": [n.__dict__ for n in self.nodes],
            "findings": self.findings,
        }


def bar_polyline(r: Reinforcement) -> tuple[list[tuple[float, float]], int] | None:
    """2D polyline for one bar, from its bend type and leg lengths.

    Returns (points, total_length). Segments alternate direction by 90 deg,
    which is a schematic rendering rather than a true bend radius model — it
    is sufficient to show shape and to self-check total length.
    """
    if not r.legs:
        return None
    try:
        shape = resolve(r.bend_type, r.legs)
    except ShapeResolutionError:
        return None

    pts: list[tuple[float, float]] = [(0.0, 0.0)]
    x = y = 0.0
    heading = 0  # 0=+x, 1=+y, 2=-x, 3=-y
    total = 0
    for col in shape.leg_columns:
        seg = r.legs.get(col)
        if seg is None:
            return None
        dx, dy = [(seg, 0), (0, seg), (-seg, 0), (0, -seg)][heading]
        x += dx
        y += dy
        pts.append((x, y))
        total += seg
        heading = (heading + 1) % 4
    return pts, total


def build_scene(graph: ProjectKnowledgeGraph) -> Scene:
    """Instantiate primitives from validated graph data."""
    scene = Scene(project_id=graph.project_id)

    for key, el in graph.elements.items():
        primitive, required = PRIMITIVES.get(el.element_type, ("box", ()))
        params = dict(el.geometry.params)
        node = SceneNode(
            element_key=key,
            element_type=el.element_type.value,
            primitive=el.geometry.primitive or primitive,
            params=params,
        )

        missing = [p for p in required if p not in params]
        if missing:
            node.state = "unknown"
            node.issues.append(f"missing geometry: {', '.join(missing)}")

        if not el.geometry.placements:
            node.state = "missing_location"
            node.issues.append("element has no placement on any sheet")
        else:
            node.placements = [
                {"x": p.x, "y": p.y, "z": p.z, "rotation": p.rotation_deg}
                for p in el.geometry.placements
            ]

        if el.conflicts:
            node.state = "conflicted"
            node.issues.extend(el.conflicts)

        for r in el.reinforcement:
            if r.unknown_fields:
                node.issues.append(
                    f"{r.role.value}: unknown {', '.join(r.unknown_fields)}"
                )
                continue  # never render unvalidated steel as plausible
            poly = bar_polyline(r)
            if poly is None:
                continue
            points, total = poly
            node.bars.append(
                {
                    "role": r.role.value,
                    "size": r.bar_size.value,
                    "shape_code": r.shape_code,
                    "bend_type": r.bend_type,
                    "points": points,
                    "path_length_mm": total,
                }
            )

        scene.nodes.append(node)

    # completeness findings the scene can detect on its own
    report = graph.completeness_report()
    for key in report["no_placement"]:
        scene.findings.append(f"{key}: present in the graph but located on no sheet")
    for key in report["single_sheet_only"]:
        scene.findings.append(f"{key}: appears on only one sheet; cross-check")
    for a, b, dist in graph.find_spatial_duplicates():
        scene.findings.append(
            f"possible duplicate: {a} and {b} are {dist:.0f}mm apart"
        )
    return scene


def self_check(scene: Scene, schedule) -> list[str]:
    """Rendered bar paths must equal their calculated cutting lengths."""
    problems: list[str] = []
    lengths: dict[str, list[int]] = {}
    for item in schedule.items:
        lengths.setdefault(item.element_key or "", []).append(item.cutting_length_mm)
    for node in scene.nodes:
        want = lengths.get(node.element_key, [])
        for bar in node.bars:
            if want and bar["path_length_mm"] not in want:
                problems.append(
                    f"{node.element_key}: rendered path {bar['path_length_mm']}mm "
                    f"is not among calculated cutting lengths {sorted(set(want))}"
                )
    return problems
