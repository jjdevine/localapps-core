"""A throwaway app called `notes`, built in a temp folder for each test: an Alembic project
with two revisions, a login template, and a FastAPI application wired the way a real app
wires the package."""

from __future__ import annotations

import csv
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from localapps_core.auth import Auth
from localapps_core.backup import Backups, Export
from localapps_core.db import Database
from localapps_core.migrations import Migrations
from localapps_core.web.auth import Login
from localapps_core.web.health import health_router

PASSWORD = "correct horse battery"

ENV_PY = """
from alembic import context

config = context.config
connection = config.attributes.get("connection")
assert connection is not None, "the tests always pass a connection"
context.configure(connection=connection, target_metadata=None, render_as_batch=True)
with context.begin_transaction():
    context.run_migrations()
"""

REVISION_1 = '''
"""notes table"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("body", sa.String, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("notes")
'''

REVISION_2 = '''
"""done flag"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("notes") as batch:
        batch.add_column(sa.Column("done", sa.Boolean, nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("notes") as batch:
        batch.drop_column("done")
'''

LOGIN_HTML = """<form method="post" action="/login">
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<input type="hidden" name="next" value="{{ next }}">
<p>Stays logged in for {{ session_days }} days.</p>
</form>
"""


def write_notes_csv(session: Session, path: Path, _now: datetime) -> None:
    rows = session.execute(text("select id, body from notes order by id")).all()
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "body"])
        writer.writerows(rows)


@dataclass
class Project:
    root: Path
    auth: Auth
    database: Database
    migrations: Migrations
    backups: Backups

    @property
    def db_file(self) -> Path:
        return self.root / "data" / "notes.db"

    def add_note(self, body: str) -> None:
        with self.database.get_session_factory()() as session:
            session.execute(text("insert into notes (body) values (:b)"), {"b": body})
            session.commit()

    def notes(self) -> list[str]:
        with self.database.get_session_factory()() as session:
            return [r[0] for r in session.execute(text("select body from notes order by id"))]


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Project]:
    root = tmp_path / "project"
    (root / "alembic" / "versions").mkdir(parents=True)
    (root / "alembic.ini").write_text("[alembic]\nscript_location = alembic\n", encoding="utf-8")
    (root / "alembic" / "env.py").write_text(ENV_PY, encoding="utf-8")
    (root / "alembic" / "versions" / "0001_notes.py").write_text(REVISION_1, encoding="utf-8")
    (root / "alembic" / "versions" / "0002_done.py").write_text(REVISION_2, encoding="utf-8")
    (root / "templates" / "auth").mkdir(parents=True)
    (root / "templates" / "auth" / "login.html").write_text(LOGIN_HTML, encoding="utf-8")
    (root / "static").mkdir()
    (root / "static" / "app.css").write_text("body { margin: 0 }\n", encoding="utf-8")

    url = f"sqlite:///{(root / 'data' / 'notes.db').as_posix()}"
    database = Database(lambda: url)
    made = Project(
        root=root,
        auth=Auth("notes", root / ".env"),
        database=database,
        migrations=Migrations(root),
        backups=Backups("notes-", Export("export-", ".csv", write_notes_csv), keep=3),
    )
    yield made
    database.reset_engine()


@pytest.fixture
def migrated(project: Project) -> Project:
    project.migrations.upgrade_to_head(project.database.get_engine())
    return project


def build_app(project: Project) -> FastAPI:
    templates = Jinja2Templates(directory=str(project.root / "templates"))
    login = Login(project.auth, templates)
    app = FastAPI(docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(project.root / "static")), name="static")
    app.middleware("http")(login.require_login)
    app.include_router(login.router)
    app.include_router(health_router(project.database, project.migrations))

    @app.get("/")
    def home(request: Request) -> dict[str, bool]:
        return {"logged_in": request.state.logged_in}

    @app.get("/notes")
    def notes() -> list[str]:
        return project.notes()

    @app.post("/notes")
    def add() -> dict[str, bool]:
        project.add_note("posted")
        return {"ok": True}

    return app


@pytest.fixture
def client(migrated: Project) -> Iterator[TestClient]:
    with TestClient(build_app(migrated)) as c:
        yield c
