"""A project a buyer creates must still be there after a restart.

PRJ-101 says project metadata is persisted. The store used to be a dict, and
"restart loses them" was written into the module docstring as if it were a
design choice. It is not a defensible one in front of a client: the product
argues that nothing is lost and everything is traceable, and then forgets the
project they made two minutes ago.

ING-101 is tested here too, because a revision is only immutable if the older
bytes survive the same restart.
"""
from __future__ import annotations

import json

import pytest

from trustsight.api.projects import (
    DEMO_TENANT, Document, ProjectStore, now,
)


@pytest.fixture()
def store_path(tmp_path):
    return tmp_path / "nested" / "projects.json"


def _doc(name: str, sha: str, size: int = 10) -> Document:
    return Document(document_id="doc-" + sha[:8], filename=name,
                    channel="web_upload", bytes_=size, sha256=sha,
                    uploaded_by="priya.raman@demo-client.com", uploaded_at=now())


def test_a_created_project_survives_a_restart(store_path):
    first = ProjectStore(store_path)
    first.create(name="Harbour Piles", tenant_id=DEMO_TENANT,
                 client="Client A", site="Dock 3",
                 created_by="priya.raman@demo-client.com")

    second = ProjectStore(store_path)          # "restart"
    assert second.load() == 1
    back = second.get("harbour-piles", DEMO_TENANT)
    assert back is not None
    assert back.name == "Harbour Piles"
    assert back.client == "Client A"
    assert back.site == "Dock 3"


def test_the_state_file_is_written_where_it_was_asked_for(store_path):
    store = ProjectStore(store_path)
    store.create(name="Any", tenant_id=DEMO_TENANT)
    assert store_path.exists(), "the parent directory must be created too"
    data = json.loads(store_path.read_text())
    assert data["projects"][0]["name"] == "Any"


def test_a_tenant_cannot_read_another_tenants_restored_project(store_path):
    first = ProjectStore(store_path)
    first.create(name="Theirs", tenant_id="someone-else")
    second = ProjectStore(store_path)
    second.load()
    assert second.get("theirs", DEMO_TENANT) is None
    assert second.get("theirs", "someone-else") is not None


def test_a_reupload_is_a_revision_not_an_overwrite(store_path):
    store = ProjectStore(store_path)
    project = store.create(name="Rev Test", tenant_id=DEMO_TENANT)
    first = store.add_document(project, _doc("S101.pdf", "a" * 64))
    second = store.add_document(project, _doc("S101.pdf", "b" * 64))

    assert first.revision == 1 and second.revision == 2
    assert second.supersedes == first.document_id
    assert first.superseded_by == second.document_id
    assert first.is_current is False and second.is_current is True
    # the original record is untouched where it matters
    assert first.sha256 == "a" * 64
    assert len(project.documents) == 2
    assert len(project.current_documents()) == 1


def test_revisions_survive_a_restart_with_their_own_hashes(store_path):
    store = ProjectStore(store_path)
    project = store.create(name="Rev Test", tenant_id=DEMO_TENANT)
    store.add_document(project, _doc("S101.pdf", "a" * 64))
    store.add_document(project, _doc("S101.pdf", "b" * 64))

    reopened = ProjectStore(store_path)
    reopened.load()
    back = reopened.get("rev-test", DEMO_TENANT)
    history = back.revisions("S101.pdf")
    assert [d.revision for d in history] == [1, 2]
    assert [d.sha256 for d in history] == ["a" * 64, "b" * 64]
    assert back.as_dict()["document_count"] == 1, "one current drawing"
    assert back.as_dict()["revision_count"] == 2, "two registered revisions"


def test_a_different_drawing_is_its_own_family(store_path):
    store = ProjectStore(store_path)
    project = store.create(name="Two Sheets", tenant_id=DEMO_TENANT)
    store.add_document(project, _doc("S101.pdf", "a" * 64))
    store.add_document(project, _doc("S103.pdf", "c" * 64))
    assert len(project.current_documents()) == 2
    assert all(d.revision == 1 for d in project.documents)


def test_a_corrupt_state_file_does_not_take_the_app_down(store_path):
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text("{ this is not json")
    store = ProjectStore(store_path)
    assert store.load() == 0
    # and the store is still usable, so the demo starts from a clean seed
    store.create(name="Fresh", tenant_id=DEMO_TENANT)
    assert store.get("fresh", DEMO_TENANT) is not None


def test_a_state_file_from_an_older_shape_is_ignored_rather_than_half_read(store_path):
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps({"version": 0, "projects": [
        {"project_id": "old", "name": "Old", "tenant_id": DEMO_TENANT}]}))
    assert ProjectStore(store_path).load() == 0


def test_a_corpus_project_whose_folder_vanished_is_dropped(tmp_path, store_path):
    """A project that would open and fail must not sit on the projects screen."""
    corpus = tmp_path / "corpus"
    (corpus / "Project 9").mkdir(parents=True)
    store = ProjectStore(store_path)
    store.seed_from_corpus(corpus, "atlantic-demo")
    assert store.get("Project 9", DEMO_TENANT) is not None

    (corpus / "Project 9").rmdir()
    reopened = ProjectStore(store_path)
    reopened.load()
    reopened.seed_from_corpus(corpus, "atlantic-demo")
    assert reopened.get("Project 9", DEMO_TENANT) is None
    assert reopened.get("atlantic-demo", DEMO_TENANT) is not None
