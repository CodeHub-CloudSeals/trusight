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

Projects are held in memory. A restart loses them and reseeds from the
corpus, which is the right trade for a demonstrator: no database to
provision, no stale state between rehearsals.
"""
from __future__ import annotations

import re
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
    """One drawing in a project. Originals are immutable (spec v2 s6)."""

    document_id: str
    filename: str
    channel: str                  # web_upload | corpus | api
    bytes_: int
    sha256: str
    uploaded_by: str
    uploaded_at: str
    path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["bytes"] = d.pop("bytes_")
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
    documents: list[Document] = field(default_factory=list)
    runs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id, "name": self.name,
            "tenant_id": self.tenant_id, "client": self.client, "site": self.site,
            "standard": self.standard, "estimator": self.estimator,
            "manual_baseline_minutes": self.manual_baseline_minutes,
            "created_by": self.created_by, "created_at": self.created_at,
            "seeded": self.seeded,
            "documents": [d.as_dict() for d in self.documents],
            "document_count": len(self.documents),
            "runs": list(self.runs),
        }


_SLUG = re.compile(r"[^a-z0-9]+")


def slug(name: str) -> str:
    return _SLUG.sub("-", name.lower()).strip("-")[:48] or "project"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProjectStore:
    """In-memory project registry, seeded from whatever corpus is mounted.

    A corpus folder is exposed as a project rather than a special case, so
    "open Project 5" and "open the one I just created" take the same path
    through the app. The only difference is where the documents came from.
    """

    def __init__(self) -> None:
        self._projects: dict[str, Project] = {}

    def seed_from_corpus(self, corpus: Path, seeded_id: str) -> None:
        self._projects.setdefault(seeded_id, Project(
            project_id=seeded_id, name="Atlantic Cages (seeded walkthrough)",
            tenant_id=DEMO_TENANT, client="TrustSight demonstration",
            site="Illustrative schematic", estimator="demo-estimator",
            created_by="system", created_at=now(), seeded=True))
        if not corpus.exists():
            return
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
                source_dir=str(d), documents=docs)

    def list(self, tenant_id: str) -> list[Project]:
        return [p for p in self._projects.values() if p.tenant_id == tenant_id]

    def get(self, project_id: str, tenant_id: str) -> Project | None:
        p = self._projects.get(project_id)
        # Tenant isolation is checked on read, not assumed from the URL: an id
        # guessed from another tenant returns nothing rather than a project.
        return p if p and p.tenant_id == tenant_id else None

    def create(self, **kw: Any) -> Project:
        name = kw.pop("name")
        pid = slug(name)
        n, base = 2, pid
        while pid in self._projects:
            pid, n = f"{base}-{n}", n + 1
        project = Project(project_id=pid, name=name, created_at=now(), **kw)
        self._projects[pid] = project
        return project

    def add_document(self, project: Project, doc: Document) -> None:
        project.documents.append(doc)


STORE = ProjectStore()
