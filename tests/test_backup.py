"""Backup, prune, list and restore."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from localapps_core.backup import (
    BackupError,
    Backups,
    database_file,
    read_schema_version,
    vacuum_into,
)
from tests.conftest import Project

NOW = datetime(2026, 11, 13, 9, 30, 15)


def backup(project: Project, directory: Path, now: datetime = NOW, **kw: int) -> Path:
    with project.database.get_session_factory()() as session:
        return project.backups.create_backup(session, directory, now=now, **kw).db_path


def test_backup_writes_a_consistent_copy_and_the_export(migrated: Project, tmp_path: Path) -> None:
    migrated.add_note("first")
    target = tmp_path / "backups" / "nested"  # created on demand
    with migrated.database.get_session_factory()() as session:
        result = migrated.backups.create_backup(session, target, now=NOW)
    assert result.db_path == target / "notes-2026-11-13-0930.db"
    assert result.export_path == target / "export-2026-11-13-0930.csv"
    assert result.removed == []
    assert not list(target.glob("*-wal")) and not list(target.glob("*-shm"))

    copy = sqlite3.connect(result.db_path)
    assert copy.execute("select body from notes").fetchall() == [("first",)]
    copy.close()
    assert result.export_path.read_text(encoding="utf-8").splitlines() == ["id,body", "1,first"]
    assert read_schema_version(result.db_path) == "0002"


def test_two_backups_in_one_minute_get_distinct_names(migrated: Project, tmp_path: Path) -> None:
    first = backup(migrated, tmp_path)
    second = backup(migrated, tmp_path)
    assert first.name == "notes-2026-11-13-0930.db" and second.name == "notes-2026-11-13-0930-15.db"
    assert (tmp_path / "export-2026-11-13-0930-15.csv").exists()


def test_prune_keeps_the_newest_with_their_exports(migrated: Project, tmp_path: Path) -> None:
    for minutes in range(3):
        backup(migrated, tmp_path, NOW + timedelta(minutes=minutes))
    same_minute = backup(migrated, tmp_path, NOW + timedelta(minutes=2))  # the -SS name, newest
    assert same_minute.name == "notes-2026-11-13-0932-15.db"
    names = sorted(p.name for p in tmp_path.iterdir() if p.is_file())
    assert names == [
        "export-2026-11-13-0931.csv",
        "export-2026-11-13-0932-15.csv",
        "export-2026-11-13-0932.csv",
        "notes-2026-11-13-0931.db",
        "notes-2026-11-13-0932-15.db",
        "notes-2026-11-13-0932.db",
    ]  # keep=3: the 09:30 pair went, both files of it

    listed = migrated.backups.list_backups(tmp_path)
    assert [b.name for b in listed] == [
        "notes-2026-11-13-0932-15.db",  # by time, not by name
        "notes-2026-11-13-0932.db",
        "notes-2026-11-13-0931.db",
    ]
    assert all(b.export_path is not None and b.schema_version is None for b in listed)
    assert migrated.backups.prune(tmp_path, keep=0) == []  # zero keeps everything
    removed = migrated.backups.prune(tmp_path, keep=1)
    assert len(removed) == 4 and [b.name for b in migrated.backups.list_backups(tmp_path)] == [
        "notes-2026-11-13-0932-15.db"
    ]


def test_list_reads_schema_versions_and_survives_a_bad_file(
    migrated: Project, tmp_path: Path
) -> None:
    good = backup(migrated, tmp_path)
    (tmp_path / "notes-2026-11-14-0200.db").write_bytes(b"not a database at all")
    (tmp_path / "export-2026-11-13-0930.csv").unlink()
    listed = migrated.backups.list_backups(tmp_path, schema_versions=True)
    assert [(b.name, b.schema_version) for b in listed] == [
        ("notes-2026-11-14-0200.db", None),
        (good.name, "0002"),
    ]
    assert listed[1].export_path is None and listed[1].size == good.stat().st_size
    assert migrated.backups.list_backups(None) == []
    assert migrated.backups.list_backups(tmp_path / "missing") == []


def test_backup_errors_are_fit_for_the_screen(migrated: Project, tmp_path: Path) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    with migrated.database.get_session_factory()() as session:
        with pytest.raises(BackupError, match="backup folder"):
            migrated.backups.create_backup(session, blocker / "inside", now=NOW)
    memory = create_engine("sqlite://")
    with pytest.raises(BackupError, match="not a file"):
        database_file(memory)
    with pytest.raises(BackupError, match="Backup failed"):
        vacuum_into(migrated.db_file, backup(migrated, tmp_path))  # the target exists
    with pytest.raises(BackupError, match="not one of this app's databases"):
        junk = tmp_path / "junk.db"
        junk.write_bytes(b"x" * 100)
        read_schema_version(junk)


def test_restore_puts_the_old_data_back_and_keeps_a_copy_of_what_it_replaced(
    migrated: Project, tmp_path: Path
) -> None:
    migrated.add_note("before the backup")
    saved = backup(migrated, tmp_path)
    migrated.add_note("after the backup")
    seen: list[int] = []

    result = migrated.backups.restore_backup(
        saved,
        migrated.database,
        migrated.migrations,
        now=NOW + timedelta(hours=1),
        before_dir=tmp_path,
        after=lambda session: seen.append(1),
    )
    assert migrated.notes() == ["before the backup"]
    assert result.schema_version == "0002" and seen == [1]
    assert result.before_copy == tmp_path / "notes-before-restore-2026-11-13-1030.db"
    aside = sqlite3.connect(result.before_copy)
    assert aside.execute("select count(*) from notes").fetchone() == (2,)
    aside.close()
    # The copy set aside is not a backup: it is not listed and not pruned.
    assert [b.name for b in migrated.backups.list_backups(tmp_path)] == [saved.name]
    migrated.add_note("the app carries on")
    assert migrated.notes() == ["before the backup", "the app carries on"]


def test_restore_migrates_an_older_backup(project: Project, tmp_path: Path) -> None:
    project.migrations.upgrade(project.database.get_engine(), "0001")
    project.add_note("from the old schema")
    old = backup(project, tmp_path)
    assert read_schema_version(old) == "0001"
    project.migrations.upgrade_to_head(project.database.get_engine())

    result = project.backups.restore_backup(old, project.database, project.migrations, now=NOW)
    assert result.schema_version == "0001"
    assert project.migrations.is_current(project.database.get_engine())
    assert project.notes() == ["from the old schema"]


def test_restore_refuses_what_it_should(migrated: Project, tmp_path: Path) -> None:
    def restore(path: Path, backups: Backups | None = None) -> None:
        (backups or migrated.backups).restore_backup(path, migrated.database, migrated.migrations)

    with pytest.raises(BackupError, match="not found"):
        restore(tmp_path / "nothing.db")
    with pytest.raises(BackupError, match="live database"):
        restore(migrated.db_file)

    newer = backup(migrated, tmp_path)
    raw = sqlite3.connect(newer)
    raw.execute("update alembic_version set version_num = '9999'")
    raw.commit()
    raw.close()
    migrated.add_note("untouched")
    with pytest.raises(BackupError, match="newer version of the app"):
        restore(newer)
    assert migrated.notes() == ["untouched"]  # a refused restore changes nothing
