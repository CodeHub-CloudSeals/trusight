"""Tenant, project and session model. Spec v2 sections 2 and 3.

What this is, stated plainly, because the difference matters more here than
anywhere else in the build:

**This is not authentication.** There is no password, no token, no identity
provider. A person picks a demo user from a list and the app carries that
choice. Spec v2 asks for "email/password login with demo tenant", and a
password box that accepts anything is worse than no password box: it teaches
a room full of buyers that TrustSight checks credentials when it does not,
and the whole product is sold on not claiming things it cannot show. So the
sign-in screen offers named demo users and says what it is, and the roadmap
to real identity (Cognito, then OIDC/SAML) is labelled as roadmap.

**The role model, on the other hand, is real.** Roles gate the actions they
say they gate: a Viewer cannot approve a clarification, and the API refuses
it rather than the UI merely hiding the button. That is worth having even in
a demo, because "who may release steel" is a governance question and the
answer should not be "whoever has the URL".

**Projects persist.** Spec v2 PRJ-101 asks for project metadata to survive,
and a demonstrator that forgets the project a buyer created ninety seconds
ago is making the opposite point to the one this product exists to make. The
store writes a single JSON file, replaced atomically, and reloads it at
startup. It is not a database and does not pretend to be one — concurrent
writers would need one — but it is durable across the restart that actually
happens, which is the deploy between a rehearsal and the session.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: One tenant in the demo. Every project, document and run carries it, so the
#: isolation story is visible in the data rather than asserted on a slide.
DEMO_TENANT = "cloudseals-demo"


@dataclass(frozen=True)
class Role:
    key: str
    label: str
    summary: str
    can_create_project: bool = False
    can_upload: bool = False
    can_clarify: bool = False      # answer a missing fact
    can_approve: bool = False      # sign off the rulebook / release
    can_export: bool = True


ROLES: dict[str, Role] = {r.key: r for r in (
    Role("admin", "Client Admin",
         "Creates projects, manages documents and approves.",
         True, True, True, True),
    Role("estimator", "Estimator / Engineer",
         "Uploads drawings and answers clarifications.",
         True, True, True, False),
    Role("reviewer", "Reviewer",
         "Signs off the rulebook and releases quantities.",
         False, False, True, True),
    Role("viewer", "Viewer",
         "Reads the schedule and the evidence. Changes nothing.",
         False, False, False, False),
    Role("support", "CloudSeals Support",
         "Diagnostic access. Cannot approve on the client's behalf.",
         False, True, False, False),
)}


@dataclass(frozen=True)
class DemoUser:
    email: str
    name: str
    role: str
    tenant_id: str = DEMO_TENANT

    def as_dict(self) -> dict[str, Any]:
        role = ROLES[self.role]
        return {"email": self.email, "name": self.name, "tenant_id": self.tenant_id,
                "role": self.role, "role_label": role.label,
                "can": {"create_project": role.can_create_project,
                        "upload": role.can_upload,
                        "clarify": role.can_clarify,
                        "approve": role.can_approve,
                        "export": role.can_export}}


USERS: dict[str, DemoUser] = {u.email: u for u in (
    DemoUser("priya.raman@demo-client.com", "Priya Raman", "admin"),
    DemoUser("tom.keller@demo-client.com", "Tom Keller", "estimator"),
    DemoUser("ana.duarte@demo-client.com", "Ana Duarte", "reviewer"),
    DemoUser("commercial@demo-client.com", "Commercial Team", "viewer"),
    DemoUser("support@cloudseals.co.uk", "CloudSeals Support", "support"),
)}


@dataclass
class Document:
    """One revision of one drawing. Originals are immutable (spec v2 s6).

    Uploading the same filename again does not overwrite anything. It appends
    a new revision that records its own bytes and its own hash, and names the
    revision it supersedes. Nothing already registered is mutated, so the
    hash a run cited last week still resolves to the bytes it was computed
    from — which is the only reason an evidence chain over documents means
    anything.

    ``family`` is the logical drawing (its filename); ``revision`` counts from
    1 within that family.
    """

    document_id: str
    filename: str
    channel: str                  # web_upload | corpus | api
    bytes_: int
    sha256: str
    uploaded_by: str
    uploaded_at: str
    path: str | None = None
    revision: int = 1
    supersedes: str | None = None      # document_id of the previous revision
    superseded_by: str | None = None   # set once, when the next revision lands

    @property
    def family(self) -> str:
        return self.filename

    @property
    def is_current(self) -> bool:
        return self.superseded_by is None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["bytes"] = d.pop("bytes_")
        d["family"] = self.family
        d["is_current"] = self.is_current
        return d


@dataclass
class Project:
    project_id: str
    name: str
    tenant_id: str
    client: str = ""
    site: str = ""
    standard: str = "ACI / RebarCAD bend types"
    estimator: str = ""
    manual_baseline_minutes: int | None = None
    created_by: str = ""
    created_at: str = ""
    #: corpus projects point at a folder; created projects collect uploads
    source_dir: str | None = None
    seeded: bool = False
    #: True only for projects backed by the read-only corpus mount. A created
    #: project also gains a source_dir the moment it receives its first
    #: upload, so source_dir alone cannot be the test for "read-only" — using
    #: it that way locked every project after one file and broke multi-file
    #: upload entirely.
    corpus_backed: bool = False
    documents: list[Document] = field(default_factory=list)
    runs: list[str] = field(default_factory=list)

    def current_documents(self) -> list[Document]:
        """The newest revision of each drawing — what a run should read."""
        return [d for d in self.documents if d.is_current]

    def revisions(self, family: str) -> list[Document]:
        return sorted((d for d in self.documents if d.family == family),
                      key=lambda d: d.revision)

    def as_dict(self) -> dict[str, Any]:
        current = self.current_documents()
        return {
            "project_id": self.project_id, "name": self.name,
            "tenant_id": self.tenant_id, "client": self.client, "site": self.site,
            "standard": self.standard, "estimator": self.estimator,
            "manual_baseline_minutes": self.manual_baseline_minutes,
            "created_by": self.created_by, "created_at": self.created_at,
            "seeded": self.seeded,
            # every revision is listed, because the superseded ones are the
            # point of keeping originals immutable
            "documents": [d.as_dict() for d in self.documents],
            "document_count": len(current),
            "revision_count": len(self.documents),
            "families": sorted({d.family for d in self.documents}),
            "runs": list(self.runs),
        }


_SLUG = re.compile(r"[^a-z0-9]+")


def slug(name: str) -> str:
    return _SLUG.sub("-", name.lower()).strip("-")[:48] or "project"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def state_path() -> Path:
    """Where the registry is written. Overridable so tests get their own."""
    env = os.getenv("TRUSTSIGHT_STATE")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3] / "data" / "state" / "projects.json"


#: Bumped when the on-disk shape changes in a way older files cannot satisfy.
STATE_VERSION = 2


class ProjectStore:
    """Project registry, persisted to one JSON file and seeded from the corpus.

    A corpus folder is exposed as a project rather than a special case, so
    "open Project 5" and "open the one I just created" take the same path
    through the app. The only difference is where the documents came from.

    Durability is deliberately modest. One file, replaced atomically under a
    lock, flushed on every mutation. That survives the restart that actually
    happens between a rehearsal and a client session. It would not survive two
    processes writing at once, and this says so rather than implying a
    database is here.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._projects: dict[str, Project] = {}
        self._path = path
        self._lock = threading.Lock()

    # ---- persistence ---------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path or state_path()

    def _flush(self) -> None:
        """Write the whole registry, atomically.

        os.replace is the point: a half-written file never appears at the real
        path, so a crash mid-write loses the last change rather than the
        entire project list.
        """
        payload = {
            "version": STATE_VERSION,
            "saved_at": now(),
            "projects": [
                {**{k: v for k, v in asdict(p).items() if k != "documents"},
                 "documents": [asdict(d) for d in p.documents]}
                for p in self._projects.values()
            ],
        }
        target = self.path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except OSError:
            # A read-only or full filesystem must not take the demo down: the
            # session continues in memory and the next restart reseeds.
            pass

    def load(self) -> int:
        """Restore from disk. Returns how many projects came back."""
        target = self.path
        if not target.exists():
            return 0
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if data.get("version") != STATE_VERSION:
            return 0
        restored = 0
        for row in data.get("projects", []):
            docs = [Document(**d) for d in row.pop("documents", [])]
            try:
                project = Project(**row, documents=docs)
            except TypeError:
                continue      # a field this build no longer has; skip the row
            self._projects[project.project_id] = project
            restored += 1
        return restored

    def forget_all(self) -> None:
        """Drop everything, on disk too. Used by tests and by /reset."""
        with self._lock:
            self._projects.clear()
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass

    # ---- seeding -------------------------------------------------------

    def seed_from_corpus(self, corpus: Path, seeded_id: str) -> None:
        with self._lock:
            self._projects.setdefault(seeded_id, Project(
                project_id=seeded_id, name="Atlantic Cages (seeded walkthrough)",
                tenant_id=DEMO_TENANT, client="TrustSight demonstration",
                site="Illustrative schematic", estimator="demo-estimator",
                created_by="system", created_at=now(), seeded=True))
            # A persisted corpus project whose folder is no longer mounted is
            # a lie on the projects screen: it would open and fail. Drop it.
            for pid, p in list(self._projects.items()):
                if p.source_dir and not Path(p.source_dir).exists():
                    del self._projects[pid]
            if corpus.exists():
                for d in sorted(p for p in corpus.glob("*") if p.is_dir()):
                    if d.name in self._projects:
                        continue
                    docs = [
                        Document(document_id="doc-" + uuid.uuid4().hex[:10],
                                 filename=f.name, channel="corpus",
                                 bytes_=f.stat().st_size, sha256="",
                                 uploaded_by="corpus mount", uploaded_at=now(),
                                 path=str(f))
                        for f in sorted(d.glob("Input*.pdf"))
                    ]
                    self._projects[d.name] = Project(
                        project_id=d.name, name=d.name, tenant_id=DEMO_TENANT,
                        client="Reference corpus", site="", estimator="",
                        created_by="corpus mount", created_at=now(),
                        source_dir=str(d), corpus_backed=True, documents=docs)
        self._flush()

    # ---- reads ---------------------------------------------------------

    def list(self, tenant_id: str) -> list[Project]:
        return [p for p in self._projects.values() if p.tenant_id == tenant_id]

    def get(self, project_id: str, tenant_id: str) -> Project | None:
        p = self._projects.get(project_id)
        # Tenant isolation is checked on read, not assumed from the URL: an id
        # guessed from another tenant returns nothing rather than a project.
        return p if p and p.tenant_id == tenant_id else None

    # ---- writes --------------------------------------------------------

    def create(self, **kw: Any) -> Project:
        name = kw.pop("name")
        with self._lock:
            pid = slug(name)
            n, base = 2, pid
            while pid in self._projects:
                pid, n = f"{base}-{n}", n + 1
            project = Project(project_id=pid, name=name, created_at=now(), **kw)
            self._projects[pid] = project
        self._flush()
        return project

    def add_document(self, project: Project, doc: Document) -> Document:
        """Register a drawing, as a new revision when the name is already here.

        The previous revision is marked superseded but kept, with its bytes and
        its hash untouched.
        """
        with self._lock:
            existing = [d for d in project.documents
                        if d.family == doc.filename and d.is_current]
            if existing:
                previous = existing[-1]
                previous.superseded_by = doc.document_id
                doc.revision = previous.revision + 1
                doc.supersedes = previous.document_id
            project.documents.append(doc)
        self._flush()
        return doc

    def touch(self) -> None:
        """Persist a mutation made directly on a Project object."""
        self._flush()


STORE = ProjectStore()
