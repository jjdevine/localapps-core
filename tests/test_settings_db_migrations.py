"""Settings, the engine and session factory, and migrations."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from localapps_core import settings as settings_module
from localapps_core.db import Database, make_engine
from localapps_core.settings import AppSettings, default_data_dir, settings_config
from tests.conftest import Project


def make_settings_class(root: Path) -> type[AppSettings]:
    class Settings(AppSettings):
        model_config = settings_config("notes", root)
        db_filename = "notes.db"
        data_dir: Path = root / "default-data"
        port: int = 8050
        greeting: str = "hello"  # an app's own field

    return Settings


def test_settings_defaults_and_derived_paths(tmp_path: Path) -> None:
    settings = make_settings_class(tmp_path)()
    assert settings.host == "127.0.0.1" and settings.port == 8050
    assert settings.db_path == tmp_path / "default-data" / "notes.db"
    assert settings.sqlalchemy_url == f"sqlite:///{settings.db_path.as_posix()}"


def test_settings_read_the_prefixed_environment_and_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "NOTES_PORT=9000\nNOTES_GREETING=from file\n"
        "NOTES_PASSWORD_HASH=scrypt$aa$bb\nOTHER_PORT=1\n",  # lines that are not settings
        encoding="utf-8",
    )
    monkeypatch.setenv("NOTES_DATA_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("NOTES_GREETING", "from environment")
    settings = make_settings_class(tmp_path)()
    assert settings.port == 9000  # from the file
    assert settings.greeting == "from environment"  # the environment wins over the file
    assert settings.data_dir == tmp_path / "elsewhere"

    monkeypatch.setenv("NOTES_DATABASE_URL", "sqlite:///somewhere/else.db")
    assert make_settings_class(tmp_path)().sqlalchemy_url == "sqlite:///somewhere/else.db"


def test_default_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_data_dir("NotesApp") == tmp_path / "NotesApp"
    monkeypatch.delenv("LOCALAPPDATA")
    monkeypatch.setattr(settings_module.Path, "home", lambda: tmp_path / "home")
    assert default_data_dir("NotesApp") == tmp_path / "home" / ".local" / "share" / "NotesApp"


def test_engine_creates_the_folder_and_sets_the_pragmas(tmp_path: Path) -> None:
    target = tmp_path / "new" / "folder" / "x.db"
    engine = make_engine(f"sqlite:///{target.as_posix()}")
    try:
        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
            assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert target.parent.is_dir()
    finally:
        engine.dispose()


def test_database_is_lazy_and_can_be_reset(tmp_path: Path) -> None:
    calls: list[str] = []

    def url() -> str:
        calls.append("asked")
        return f"sqlite:///{(tmp_path / f'db{len(calls)}.db').as_posix()}"

    database = Database(url)
    assert calls == []  # nothing happens until the engine is needed
    first = database.get_engine()
    assert database.get_engine() is first and calls == ["asked"]
    assert database.get_session_factory().kw["bind"] is first

    database.reset_engine()
    second = database.get_engine()
    assert second is not first and len(calls) == 2
    database.reset_engine()


def test_get_db_yields_one_session_and_closes_it(migrated: Project) -> None:
    sessions = migrated.database.get_db()
    session = next(sessions)
    assert session.execute(text("select count(*) from notes")).scalar() == 0
    assert session.connection().closed is False
    with pytest.raises(StopIteration):
        next(sessions)
    assert not session.in_transaction()


def test_migrations(project: Project) -> None:
    migrations, engine = project.migrations, project.database.get_engine()
    assert migrations.head_revision() == "0002"
    assert migrations.known_revisions() == {"0001", "0002"}
    assert migrations.current_revision(engine) is None and not migrations.is_current(engine)

    migrations.upgrade(engine, "0001")
    assert migrations.current_revision(engine) == "0001" and not migrations.is_current(engine)

    migrations.upgrade_to_head(engine)
    assert migrations.current_revision(engine) == "0002" and migrations.is_current(engine)
    migrations.upgrade_to_head(engine)  # running it again is harmless
    project.add_note("still works")
    assert project.notes() == ["still works"]


REVISION_TAGS = '''
"""tags, each deleted with its note"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tags",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("note_id", sa.Integer, sa.ForeignKey("notes.id", ondelete="CASCADE")),
        sa.Column("label", sa.String, nullable=False),
    )
'''

REVISION_REBUILD = '''
"""a change SQLite can only make by building the notes table again"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("notes") as batch:
        batch.alter_column("body", existing_type=sa.String, type_=sa.Text)
'''

REVISION_ORPHANS = '''
"""deletes the notes and leaves their tags pointing at nothing"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM notes")
'''


def note_with_a_tag(project: Project, last_revision: str) -> None:
    versions = project.root / "alembic" / "versions"
    (versions / "0003_tags.py").write_text(REVISION_TAGS, encoding="utf-8")
    (versions / "0004_last.py").write_text(last_revision, encoding="utf-8")
    project.migrations.upgrade(project.database.get_engine(), "0003")
    project.add_note("kept")
    with project.database.get_session_factory()() as session:
        session.execute(text("insert into tags (note_id, label) values (1, 'x')"))
        session.commit()


def count(project: Project, table: str) -> int:
    with project.database.get_session_factory()() as session:
        return int(session.execute(text(f"select count(*) from {table}")).scalar_one())


def test_rebuilding_a_table_keeps_the_rows_that_point_at_it(project: Project) -> None:
    note_with_a_tag(project, REVISION_REBUILD)
    engine = project.database.get_engine()
    project.migrations.upgrade_to_head(engine)
    assert project.migrations.current_revision(engine) == "0004"
    assert project.notes() == ["kept"]
    assert count(project, "tags") == 1  # dropping the old notes table did not cascade

    # Foreign keys are back on afterwards, on the connection the upgrade used as on any other.
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        conn.execute(text("delete from notes"))
        conn.commit()
    assert count(project, "tags") == 0


def test_an_upgrade_that_leaves_rows_pointing_at_nothing_fails(project: Project) -> None:
    note_with_a_tag(project, REVISION_ORPHANS)
    engine = project.database.get_engine()
    with pytest.raises(RuntimeError, match="tags"):
        project.migrations.upgrade_to_head(engine)
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
