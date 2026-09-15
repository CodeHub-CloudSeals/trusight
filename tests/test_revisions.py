"""Revisions through the API, and what a rev-to-rev comparison may claim.

ING-101 wants "multi-file upload with hash, revision and immutable originals".
REV-101 wants "rev-to-rev changes and quantity impact". The interesting part
of both is the refusal: a comparison over a drawing the engine cannot fully
read must not produce a kilogram figure, because a revision delta is exactly
the number someone forwards to a subcontractor.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

ADMIN = {"x-trustsight-user": "priya.raman@demo-client.com"}


def _pdf(text: str) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUSTSIGHT_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("TRUSTSIGHT_CORPUS", str(tmp_path / "corpus"))
    (tmp_path / "corpus").mkdir()
    import importlib

    from trustsight.api import main as main_module
    importlib.reload(main_module)
    with TestClient(main_module.app, headers=ADMIN) as c:
        yield c


def _new_project(client, name="Revision Project"):
    r = client.post("/api/projects", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["project_id"]


def _upload(client, pid, filename, body):
    return client.post(f"/api/projects/{pid}/documents", content=body,
                       headers={**ADMIN, "x-filename": filename,
                                "content-type": "application/pdf"})


def test_the_same_filename_twice_produces_two_revisions(client):
    pid = _new_project(client)
    first = _upload(client, pid, "S101.pdf", _pdf("PILE SCHEDULE rev A"))
    assert first.status_code == 200, first.text
    assert first.json()["revision"] == 1
    assert first.json()["supersedes"] is None

    second = _upload(client, pid, "S101.pdf", _pdf("PILE SCHEDULE rev B"))
    assert second.status_code == 200, second.text
    assert second.json()["revision"] == 2
    assert second.json()["supersedes"] == first.json()["document"]["document_id"]
    # one current drawing, two registered revisions
    assert second.json()["document_count"] == 1


def test_the_superseded_original_is_still_on_disk_with_its_own_hash(client):
    pid = _new_project(client)
    a = _upload(client, pid, "S101.pdf", _pdf("rev A")).json()["document"]
    b = _upload(client, pid, "S101.pdf", _pdf("rev B")).json()["document"]
    assert a["sha256"] != b["sha256"]

    history = client.get(f"/api/projects/{pid}/revisions").json()["families"]
    assert len(history) == 1
    revs = history[0]["revisions"]
    assert [r["revision"] for r in revs] == [1, 2]
    assert history[0]["current"] == b["document_id"]

    # the point of "immutable originals": rev 1's bytes are still readable and
    # still hash to what was registered
    import hashlib
    from pathlib import Path

    old = Path(revs[0]["path"])
    assert old.exists(), "the superseded original was deleted or overwritten"
    assert hashlib.sha256(old.read_bytes()).hexdigest() == revs[0]["sha256"]


def test_the_superseded_file_is_not_read_by_the_extraction_glob(client):
    """Two revisions on disk must not become two drawings in one run."""
    from pathlib import Path

    pid = _new_project(client)
    _upload(client, pid, "S101.pdf", _pdf("rev A"))
    _upload(client, pid, "S101.pdf", _pdf("rev B"))
    project = client.get(f"/api/projects/{pid}").json()
    current = [d for d in project["documents"] if d["is_current"]]
    folder = Path(current[0]["path"]).parent
    assert len(sorted(folder.glob("Input*.pdf"))) == 1, (
        "the shallow glob every reader uses must see only the current revision")
    assert (folder / "_revisions").is_dir()


def test_one_revision_cannot_be_compared_with_itself(client):
    pid = _new_project(client)
    _upload(client, pid, "S101.pdf", _pdf("rev A"))
    r = client.get(f"/api/projects/{pid}/revisions/compare", params={"family": "S101.pdf"})
    assert r.status_code == 409
    assert "two" in r.json()["detail"]


def test_an_unreadable_revision_yields_no_quantity_impact(client):
    """The refusal this file exists for."""
    pid = _new_project(client)
    _upload(client, pid, "S101.pdf", _pdf("rev A: not a drawing this build reads"))
    _upload(client, pid, "S101.pdf", _pdf("rev B: also not one"))

    body = client.get(f"/api/projects/{pid}/revisions/compare",
                      params={"family": "S101.pdf"}).json()
    assert body["identical"] is False
    impact = body["quantity_impact"]
    assert impact["available"] is False
    assert "no quantity impact is claimed" in impact["reason"] or "guess" in impact["reason"]
    # and nothing in the payload smuggles a number back in
    assert "mass_kg" not in impact and "pct" not in impact


def test_an_identical_reupload_says_so_rather_than_inventing_a_diff(client):
    pid = _new_project(client)
    same = _pdf("identical bytes")
    _upload(client, pid, "S101.pdf", same)
    _upload(client, pid, "S101.pdf", same)
    body = client.get(f"/api/projects/{pid}/revisions/compare",
                      params={"family": "S101.pdf"}).json()
    assert body["identical"] is True
    assert body["changes"] == []
    assert body["quantity_impact"] is None


def test_a_viewer_cannot_upload_a_revision(client):
    pid = _new_project(client)
    r = client.post(f"/api/projects/{pid}/documents", content=_pdf("x"),
                    headers={"x-trustsight-user": "commercial@demo-client.com",
                             "x-filename": "S101.pdf"})
    assert r.status_code == 403


def test_a_corpus_project_refuses_uploads_rather_than_mutating_the_mount(
        tmp_path, monkeypatch):
    """The read-only guard still holds, now that it no longer over-fires."""
    import importlib

    corpus = tmp_path / "corpus"
    (corpus / "Project 9").mkdir(parents=True)
    monkeypatch.setenv("TRUSTSIGHT_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("TRUSTSIGHT_CORPUS", str(corpus))
    from trustsight.api import main as main_module
    importlib.reload(main_module)
    with TestClient(main_module.app, headers=ADMIN) as c:
        listed = c.get("/api/projects").json()
        rows = listed["projects"] if isinstance(listed, dict) else listed
        mounted = [p for p in rows if p.get("project_id") == "Project 9"]
        assert mounted, "the corpus project should be visible"
        r = _upload(c, "Project 9", "S101.pdf", _pdf("x"))
        assert r.status_code == 409
        assert "read-only" in r.json()["detail"]


def test_multiple_files_upload_to_one_project(client):
    """Multi-file upload, actually exercised (ING-101).

    This was previously 'verified' by finding a `multiple` attribute in the
    markup. The API refused every file after the first, because a created
    project gained a source_dir on its first upload and the read-only guard
    tested source_dir.
    """
    pid = _new_project(client, "Multi File")
    for name in ("S101.pdf", "S102.pdf", "S103.pdf"):
        r = _upload(client, pid, name, _pdf(f"sheet {name}"))
        assert r.status_code == 200, f"{name}: {r.text}"
    project = client.get(f"/api/projects/{pid}").json()
    assert project["document_count"] == 3
    assert sorted(project["families"]) == ["S101.pdf", "S102.pdf", "S103.pdf"]


def test_an_upload_survives_a_restart(client, tmp_path, monkeypatch):
    """PRJ-101 and ING-101 together, through the API."""
    import importlib

    pid = _new_project(client, "Durable")
    _upload(client, pid, "S101.pdf", _pdf("rev A"))
    _upload(client, pid, "S101.pdf", _pdf("rev B"))

    from trustsight.api import main as main_module
    importlib.reload(main_module)                    # a restart
    with TestClient(main_module.app, headers=ADMIN) as fresh:
        project = fresh.get(f"/api/projects/{pid}").json()
        assert project["name"] == "Durable"
        assert project["revision_count"] == 2
        assert project["document_count"] == 1


def test_a_password_protected_pdf_is_named_as_such(client):
    """The error must say what is wrong, not blame the file generically."""
    import pymupdf

    doc = pymupdf.open()
    doc.new_page()
    body = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256,
                       owner_pw="o", user_pw="u")
    doc.close()
    pid = _new_project(client, "Locked")
    r = _upload(client, pid, "S101.pdf", body)
    assert r.status_code == 422
    assert "password" in r.json()["detail"].lower()
