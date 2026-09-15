"""Test isolation for the persisted project registry.

Once projects survive a restart, they also survive a test run. Without this,
the second `pytest` on a machine finds the first run's projects still there
and a test asserting a slug gets `role-test-tower-2`. Worse, a suite run by a
developer would write into the state file the demo itself loads.

Every test session therefore gets its own state file in a temporary
directory, set before `trustsight.api.main` is imported anywhere.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="trustsight-tests-"))
os.environ["TRUSTSIGHT_STATE"] = str(_TMP / "projects.json")


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    """Each test starts from the same registry: seed project plus corpus.

    The file is redirected *and* the in-process store is rebuilt, because a
    project created by an earlier test in the same session is just as much a
    leak as one left on disk by an earlier run — that is what turned a slug
    assertion into `role-test-tower-3`.
    """
    if not os.environ.get("TRUSTSIGHT_STATE", "").startswith(str(_TMP)):
        yield                      # a test managing its own state file
        return

    monkeypatch.setenv("TRUSTSIGHT_STATE", str(tmp_path / "projects.json"))
    try:
        from trustsight.api import projects as prj
    except Exception:              # a test that does not touch the API
        yield
        return

    prj.STORE.forget_all()
    corpus = Path(os.environ.get("TRUSTSIGHT_CORPUS", "/nonexistent"))
    prj.STORE.seed_from_corpus(corpus, "atlantic-demo")
    yield
