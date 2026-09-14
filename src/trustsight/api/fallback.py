"""Recorded runs. Spec section 15, "Fallback".

A client session is not the place to discover that the corpus volume did not
mount, that a PDF is missing, or that a laptop lost its network. This module
serves a previously verified run from disk, read-only, so the walkthrough can
continue from the same screens with the same numbers.

Two design decisions worth stating, because both are the kind of thing that
quietly turns a safety net into a lie:

* A recorded run is labelled as recorded, everywhere it appears. The payload
  carries ``recorded: true`` and the UI prints it. A frozen run presented as
  a live one is a demo that proves nothing, and it is the exact failure mode
  this product is sold against.
* A recorded run refuses writes. Answering a clarification against it would
  show a number changing that nothing recalculated.

The freeze itself is done by ``scripts/freeze_run.py`` against a live server,
which is why what lands here is a real run's output and not a fixture
someone typed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

#: Recorded runs live outside the source tree so freezing one does not dirty
#: the repository, and so an operator can point at a different set per host.
STORE = Path(os.getenv(
    "TRUSTSIGHT_FALLBACK",
    str(Path(__file__).parents[3] / "data" / "fallback")))

#: Frozen ids carry this prefix. Real run ids are uuid4, so the two can never
#: collide and no lookup has to guess which kind it is holding.
PREFIX = "recorded-"


def is_recorded(run_id: str) -> bool:
    return run_id.startswith(PREFIX)


def _dir(run_id: str) -> Path:
    # the prefix is fixed and the remainder must be a single safe segment
    stem = run_id[len(PREFIX):]
    if not stem or "/" in stem or "\\" in stem or stem.startswith("."):
        raise KeyError(run_id)
    return STORE / stem


def available() -> list[dict[str, Any]]:
    """Every recorded run this instance can serve, newest first."""
    out: list[dict[str, Any]] = []
    if not STORE.exists():
        return out
    for d in sorted(p for p in STORE.glob("*") if p.is_dir()):
        manifest = d / "manifest.json"
        if not manifest.exists():
            continue
        try:
            meta = json.loads(manifest.read_text())
        except (OSError, ValueError):
            continue
        meta["run_id"] = PREFIX + d.name
        out.append(meta)
    return sorted(out, key=lambda m: m.get("captured_at", ""), reverse=True)


def _read(run_id: str, name: str) -> Any:
    path = _dir(run_id) / name
    if not path.exists():
        raise KeyError(run_id)
    return json.loads(path.read_text())


def workspace(run_id: str) -> dict[str, Any]:
    data = _read(run_id, "workspace.json")
    data["recorded"] = True
    return data


def scene(run_id: str) -> dict[str, Any]:
    return _read(run_id, "scene.json")


def document(run_id: str) -> dict[str, Any]:
    pages = len(list(_dir(run_id).glob("page-*.png")))
    return {"pages": max(pages, 1), "sample": pages == 0, "recorded": True}


def page_png(run_id: str, page: int) -> bytes:
    path = _dir(run_id) / f"page-{page}.png"
    if not path.exists():
        raise KeyError(run_id)
    return path.read_bytes()


def summary(run_id: str) -> dict[str, Any]:
    """What ``GET /runs/{id}`` returns for a recorded run."""
    data = workspace(run_id)
    run = dict(data.get("run") or {})
    run["recorded"] = True
    run["run_id"] = run_id
    return run
