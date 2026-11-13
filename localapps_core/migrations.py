"""Run Alembic migrations programmatically and report the schema version.

An app runs `upgrade_to_head` when it starts, so deploying new code and bringing an older
backup up to date are the same step: start the app.

The project is expected to keep `alembic.ini` and an `alembic/` folder in its root, and its
`alembic/env.py` to use the connection passed in `config.attributes["connection"]` when there
is one, so the upgrade runs on the app's own engine.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine


class Migrations:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def alembic_config(self) -> Config:
        cfg = Config(str(self.project_root / "alembic.ini"))
        cfg.set_main_option("script_location", str(self.project_root / "alembic"))
        return cfg

    def head_revision(self) -> str | None:
        """The newest revision the code knows about."""
        return ScriptDirectory.from_config(self.alembic_config()).get_current_head()

    def known_revisions(self) -> set[str]:
        script = ScriptDirectory.from_config(self.alembic_config())
        return {rev.revision for rev in script.walk_revisions()}

    def current_revision(self, engine: Engine) -> str | None:
        """The revision the database is at; None for a database that has never been migrated."""
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()

    def is_current(self, engine: Engine) -> bool:
        """True when the database is at the newest revision."""
        current = self.current_revision(engine)
        return current is not None and current == self.head_revision()

    def upgrade(self, engine: Engine, revision: str = "head") -> None:
        cfg = self.alembic_config()
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, revision)

    def upgrade_to_head(self, engine: Engine) -> None:
        self.upgrade(engine, "head")
