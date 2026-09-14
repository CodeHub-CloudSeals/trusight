"""A recorded run must be obviously recorded, and must refuse to pretend.

The failure this guards against is not a crash. It is a demo that quietly
replays a recording while the room believes it is watching a live run — the
exact thing this product is sold to prevent other people doing.
"""
from __future__ import annotations

import json

import pytest

from trustsight.api import fallback


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(fallback, "STORE", tmp_path)
    run = tmp_path / "atlantic"
    run.mkdir()
    (run / "manifest.json").write_text(json.dumps({
        "label": "Atlantic — recorded", "captured_at": "2026-09-14T10:00:00+00:00",
        "released_mass_kg": 5991.4, "verified_against_reference": True}))
    (run / "workspace.json").write_text(json.dumps({
        "run": {"run_id": "original", "state": "completed"},
        "released_mass_kg": 5991.4}))
    (run / "scene.json").write_text(json.dumps({"nodes": []}))
    (run / "page-1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return tmp_path


def test_a_recorded_run_is_labelled_as_recorded(store):
    data = fallback.workspace("recorded-atlantic")
    assert data["recorded"] is True
    assert fallback.summary("recorded-atlantic")["recorded"] is True


def test_recorded_ids_cannot_collide_with_live_ones():
    assert fallback.is_recorded("recorded-atlantic")
    assert not fallback.is_recorded("990e4eed-b265-4152-ad25-a43a7a8855a4")


def test_a_recorded_id_cannot_escape_the_store(store):
    for bad in ("recorded-../../etc", "recorded-.hidden", "recorded-"):
        with pytest.raises(KeyError):
            fallback.workspace(bad)


def test_listing_reports_whether_the_run_reconciled(store):
    runs = fallback.available()
    assert len(runs) == 1
    assert runs[0]["run_id"] == "recorded-atlantic"
    # a fallback that never matched its reference must say so, not hide it
    assert runs[0]["verified_against_reference"] is True


def test_a_missing_store_is_empty_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(fallback, "STORE", tmp_path / "nothing-here")
    assert fallback.available() == []


def test_writes_against_a_recorded_run_are_refused():
    """A recorded run has no context to mutate; the API must say so."""
    from fastapi import HTTPException

    from trustsight.api import main

    with pytest.raises(HTTPException) as exc:
        main._require("recorded-atlantic")
    assert exc.value.status_code == 409
    assert "read-only" in str(exc.value.detail)
