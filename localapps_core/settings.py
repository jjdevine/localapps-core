"""Application settings, read from the environment and a .env file.

An app subclasses `AppSettings`, gives the three things that differ between apps (its name,
its default port and where its data lives) and adds any fields of its own:

    class Settings(AppSettings):
        model_config = settings_config("notes", PROJECT_ROOT)
        db_filename = "notes.db"
        data_dir: Path = default_data_dir("NotesApp")
        port: int = 8050

Every field is then read from `NOTES_<FIELD>`, in the process environment first and the
project's .env second.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar

from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir(folder: str) -> Path:
    """Where an app keeps its database when nothing says otherwise: `%LOCALAPPDATA%\\<folder>`
    on Windows, `~/.local/share/<folder>` elsewhere. A server sets `<NAME>_DATA_DIR` instead."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / folder


def settings_config(name: str, project_root: Path) -> SettingsConfigDict:
    """The pydantic-settings configuration for an app called `name`: the `<NAME>_` prefix and
    the .env file in the project root. Unknown lines in the file are ignored, because secrets
    and other apps' settings may share it."""
    return SettingsConfigDict(
        env_file=project_root / ".env",
        env_file_encoding="utf-8",
        env_prefix=f"{name.upper()}_",
        extra="ignore",
    )


class AppSettings(BaseSettings):
    """What every app needs to know to start. A subclass supplies `model_config`, a default
    `data_dir` and `port`, and `db_filename`."""

    db_filename: ClassVar[str] = "app.db"

    data_dir: Path
    database_url: str | None = None
    host: str = "127.0.0.1"
    port: int

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename

    @property
    def sqlalchemy_url(self) -> str:
        """`<NAME>_DATABASE_URL` when set (tests use it), else the SQLite file in `data_dir`."""
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.db_path.as_posix()}"
