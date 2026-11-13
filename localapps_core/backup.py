"""Backup and restore for one SQLite database.

A backup is a consistent copy of the database file made with `VACUUM INTO`: SQLite reads
through its own locking, so the app keeps running, and the copy is a single file with no
`-wal` sidecar. An app can have a second file written beside each copy (a CSV or JSON export)
so the data is readable without the app. The newest `keep` backups are kept.

File names carry the time, `<prefix>YYYY-MM-DD-HHMM.db`, with `-SS` added when two backups
land in the same minute.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from localapps_core.db import Database
from localapps_core.migrations import Migrations

KEEP = 24


class BackupError(Exception):
    """A backup or restore that could not be done, with a message fit for the screen."""


@dataclass(frozen=True, slots=True)
class Export:
    """A second file written beside each database copy, named `<prefix><stamp><suffix>`.
    `write` is given a session, the path to write and the time of the backup."""

    prefix: str
    suffix: str
    write: Callable[[Session, Path, datetime], None]


@dataclass(slots=True)
class BackupResult:
    db_path: Path
    export_path: Path | None
    removed: list[Path] = field(default_factory=list)


@dataclass(slots=True)
class BackupInfo:
    name: str
    db_path: Path
    export_path: Path | None
    created: datetime
    size: int
    schema_version: str | None


@dataclass(slots=True)
class RestoreResult:
    restored_from: Path
    before_copy: Path
    schema_version: str | None


def database_file(engine: Engine) -> Path:
    database = engine.url.database
    if not database or database == ":memory:":
        raise BackupError("The database is not a file, so it cannot be backed up")
    return Path(database)


def stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%d-%H%M")


def unique_path(directory: Path, prefix: str, stamp_text: str, suffix: str, now: datetime) -> Path:
    """`<prefix><stamp><suffix>`, or with the seconds added if that name is taken."""
    path = directory / f"{prefix}{stamp_text}{suffix}"
    if path.exists():
        path = directory / f"{prefix}{stamp_text}-{now:%S}{suffix}"
    return path


def vacuum_into(source: Path, dest: Path) -> None:
    """Write a consistent single-file copy of `source` to `dest`, which must not exist."""
    try:
        conn = sqlite3.connect(str(source), isolation_level=None)
        try:
            conn.execute("VACUUM INTO ?", (str(dest),))
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise BackupError(f"Backup failed: {exc}") from exc


def read_schema_version(path: Path) -> str | None:
    """Open a backup read-only, check it is a healthy SQLite file, return its Alembic revision."""
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise BackupError(f"{path.name} cannot be opened: {exc}") from exc
    try:
        ok = conn.execute("PRAGMA quick_check").fetchone()
        if not ok or ok[0] != "ok":
            raise BackupError(f"{path.name} failed SQLite's integrity check")
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"{path.name} is not one of this app's databases: {exc}") from exc
    finally:
        conn.close()
    return row[0] if row else None


class Backups:
    """The backups of one app. `db_prefix` starts every database copy's name, for example
    `notes-`; `export` describes the optional second file."""

    def __init__(self, db_prefix: str, export: Export | None = None, keep: int = KEEP) -> None:
        self.db_prefix = db_prefix
        self.export = export
        self.keep = keep
        self.before_restore_prefix = f"{db_prefix}before-restore-"

    # -- names ---------------------------------------------------------------------------

    def _stamp_of(self, db_path: Path) -> str:
        return db_path.name[len(self.db_prefix) : -len(".db")]

    def _sort_key(self, path: Path) -> tuple[str, int]:
        """Order `<prefix>YYYY-MM-DD-HHMM[-SS].db` by time; by name alone `-SS` would sort
        before the bare stamp."""
        parts = self._stamp_of(path).split("-")
        seconds = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else -1
        return "-".join(parts[:4]), seconds

    def _db_backups(self, directory: Path) -> list[Path]:
        return sorted(
            (
                p
                for p in directory.glob(f"{self.db_prefix}*.db")
                if not p.name.startswith(self.before_restore_prefix)
            ),
            key=self._sort_key,
        )

    def _export_for(self, db_path: Path) -> Path | None:
        if self.export is None:
            return None
        name = f"{self.export.prefix}{self._stamp_of(db_path)}{self.export.suffix}"
        return db_path.parent / name

    # -- backup --------------------------------------------------------------------------

    def create_backup(
        self,
        session: Session,
        directory: Path,
        *,
        now: datetime | None = None,
        keep: int | None = None,
    ) -> BackupResult:
        """Write the database copy and the export, then prune to the newest `keep`."""
        now = now or datetime.now()
        engine = session.get_bind()
        assert isinstance(engine, Engine)
        source = database_file(engine)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BackupError(f"Cannot create the backup folder {directory}: {exc}") from exc
        if not directory.is_dir():
            raise BackupError(f"The backup folder is not a directory: {directory}")

        db_path = unique_path(directory, self.db_prefix, stamp(now), ".db", now)
        session.commit()  # VACUUM INTO copies committed data only
        vacuum_into(source, db_path)
        export_path = self._export_for(db_path)
        if self.export is not None and export_path is not None:
            try:
                self.export.write(session, export_path, now)
            except OSError as exc:
                raise BackupError(f"Backup failed: {exc}") from exc
        removed = self.prune(directory, keep=keep)
        return BackupResult(db_path=db_path, export_path=export_path, removed=removed)

    def prune(self, directory: Path, *, keep: int | None = None) -> list[Path]:
        """Delete all but the newest `keep` backups, each with its export. A `keep` of zero
        keeps everything."""
        keep = self.keep if keep is None else keep
        removed: list[Path] = []
        for old in self._db_backups(directory)[:-keep] if keep else []:
            for path in (old, self._export_for(old)):
                if path is not None and path.exists():
                    path.unlink()
                    removed.append(path)
        return removed

    def list_backups(
        self, directory: Path | None, *, schema_versions: bool = False
    ) -> list[BackupInfo]:
        """The database copies in the folder, newest first. With `schema_versions` each file is
        opened and checked, which costs a moment per backup; an unreadable one is listed with
        no version."""
        if directory is None or not directory.is_dir():
            return []
        out: list[BackupInfo] = []
        for db_path in reversed(self._db_backups(directory)):
            version: str | None = None
            if schema_versions:
                try:
                    version = read_schema_version(db_path)
                except BackupError:
                    version = None
            export_path = self._export_for(db_path)
            stat = db_path.stat()
            out.append(
                BackupInfo(
                    name=db_path.name,
                    db_path=db_path,
                    export_path=export_path if export_path and export_path.exists() else None,
                    created=datetime.fromtimestamp(stat.st_mtime),
                    size=stat.st_size,
                    schema_version=version,
                )
            )
        return out

    # -- restore -------------------------------------------------------------------------

    def restore_backup(
        self,
        backup_path: Path,
        database: Database,
        migrations: Migrations,
        *,
        now: datetime | None = None,
        before_dir: Path | None = None,
        after: Callable[[Session], None] | None = None,
    ) -> RestoreResult:
        """Replace the live database with a backup.

        Steps: verify the file, close every connection, copy the live database aside as
        `<prefix>before-restore-<stamp>.db`, copy the backup over it, reopen and migrate to
        head, then call `after` with a session (an app seeds its default settings there).
        The caller must not hold an open session.
        """
        now = now or datetime.now()
        if not backup_path.is_file():
            raise BackupError(f"Backup not found: {backup_path}")
        version = read_schema_version(backup_path)
        if version is not None and version not in migrations.known_revisions():
            raise BackupError(
                f"{backup_path.name} was made by a newer version of the app (schema {version}); "
                "update the app before restoring it"
            )

        live = database_file(database.get_engine())
        if backup_path.resolve() == live.resolve():
            raise BackupError("That file is the live database")
        database.reset_engine()

        before_dir = before_dir or live.parent
        before_dir.mkdir(parents=True, exist_ok=True)
        before_copy = unique_path(before_dir, self.before_restore_prefix, stamp(now), ".db", now)
        if live.exists():
            raw = sqlite3.connect(str(live), isolation_level=None)
            try:
                raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                raw.close()
            shutil.copy2(live, before_copy)
        # The sidecars belong to the database being replaced; left behind, SQLite would
        # replay them into the restored one.
        for suffix in ("-wal", "-shm"):
            side = Path(str(live) + suffix)
            if side.exists():
                side.unlink()
        shutil.copy2(backup_path, live)

        migrations.upgrade_to_head(database.get_engine())
        if after is not None:
            with database.get_session_factory()() as session:
                after(session)
        return RestoreResult(
            restored_from=backup_path, before_copy=before_copy, schema_version=version
        )
