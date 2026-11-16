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
from sqlalchemy import Connection, Engine


def _dangling_rows(conn: Connection) -> set[tuple[str, str, int | None]]:
    """Every row that breaks a foreign key, as (its table, the table it points at, its rowid)."""
    rows = conn.exec_driver_sql("PRAGMA foreign_key_check").all()
    return {(str(row[0]), str(row[2]), row[1]) for row in rows}


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
        """Run the migrations up to `revision`, with SQLite's foreign keys switched off.

        Many changes to a table can only be made on SQLite by building the table again:
        Alembic's batch mode copies the rows to a new table, drops the old one and renames
        the copy. With foreign keys on, dropping the old table deletes its rows first, and
        every row elsewhere that points at one of them `ON DELETE CASCADE` goes with it. So
        the keys are off while the migrations run, as SQLite's own instructions for changing
        a table say, and the upgrade fails if it left a row pointing at nothing.
        """
        cfg = self.alembic_config()
        with engine.connect() as conn:
            sqlite = conn.dialect.name == "sqlite"
            if sqlite:
                # Outside a transaction: inside one SQLite ignores this pragma.
                conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
                conn.commit()
            try:
                with conn.begin():
                    before = _dangling_rows(conn) if sqlite else set()
                    cfg.attributes["connection"] = conn
                    command.upgrade(cfg, revision)
                    left = (_dangling_rows(conn) if sqlite else set()) - before
                    if left:
                        tables = ", ".join(sorted({f"{row[0]} (to {row[1]})" for row in left}))
                        raise RuntimeError(
                            f"the migrations left {len(left)} rows pointing at rows that are "
                            f"not there: {tables}"
                        )
            finally:
                if sqlite:
                    # The connection goes back to the pool, so it is put back as it was.
                    conn.rollback()
                    conn.exec_driver_sql("PRAGMA foreign_keys=ON")
                    conn.commit()

    def upgrade_to_head(self, engine: Engine) -> None:
        self.upgrade(engine, "head")
