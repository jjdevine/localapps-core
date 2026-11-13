"""SQLAlchemy engine and session management for one SQLite database."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker


def _apply_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def make_engine(url: str) -> Engine:
    """An engine with write-ahead logging and foreign keys switched on for every connection.
    The folder for a SQLite file is created if it is missing."""
    if url.startswith("sqlite:///") and not url.endswith(":memory:"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, future=True)
    event.listen(engine, "connect", _apply_pragmas)
    return engine


class Database:
    """One lazily created engine and session factory.

    `url` is called when the engine is first needed, not when the object is made, so an app
    can build this at import time and tests can still point the settings somewhere else
    before the first query.
    """

    def __init__(self, url: Callable[[], str]) -> None:
        self._url = url
        self._engine: Engine | None = None
        self._session_factory: sessionmaker[Session] | None = None

    def get_engine(self) -> Engine:
        if self._engine is None:
            self._engine = make_engine(self._url())
            self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False)
        return self._engine

    def get_session_factory(self) -> sessionmaker[Session]:
        self.get_engine()
        assert self._session_factory is not None
        return self._session_factory

    def reset_engine(self) -> None:
        """Dispose of the cached engine and every connection it holds (tests and restore)."""
        if self._engine is not None:
            self._engine.dispose()
        self._engine = None
        self._session_factory = None

    def get_db(self) -> Iterator[Session]:
        """One session per request; usable directly as a FastAPI dependency."""
        session = self.get_session_factory()()
        try:
            yield session
        finally:
            session.close()
