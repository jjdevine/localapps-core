# 01 · Wiring an app

Everything in the package is derived from the app's one short lowercase name. The examples use an app called `notes`: its settings are read from `NOTES_...`, its password hash is `NOTES_PASSWORD_HASH`, its cookie is `notes_session` and its backups start `notes-`.

The app keeps its own small modules, each a few lines that build one object from the package. The rest of the app imports those objects and never needs to know the package is there.

## `app/config.py`

```python
from functools import lru_cache
from pathlib import Path

from localapps_core.settings import AppSettings, default_data_dir, settings_config

NAME = "notes"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(AppSettings):
    model_config = settings_config(NAME, PROJECT_ROOT)
    db_filename = "notes.db"
    data_dir: Path = default_data_dir("NotesApp")
    port: int = 8050
    # the app's own settings go here, each read from NOTES_<FIELD>


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

`AppSettings` already has `host` (default `127.0.0.1`), `database_url` (unset; tests use it), and the derived `db_path` and `sqlalchemy_url`.

## `app/db.py`

```python
from localapps_core.db import Database

from app.config import get_settings

database = Database(lambda: get_settings().sqlalchemy_url)
get_engine = database.get_engine
get_session_factory = database.get_session_factory
reset_engine = database.reset_engine
get_db = database.get_db
```

The URL is asked for when the engine is first needed, not at import, so a test can change the settings first. `get_db` is the FastAPI dependency: `db: Annotated[Session, Depends(get_db)]`.

## `app/services/migrations.py`

```python
from localapps_core.migrations import Migrations

from app.config import PROJECT_ROOT

migrations = Migrations(PROJECT_ROOT)
```

The project keeps `alembic.ini` and `alembic/` in its root. Its `alembic/env.py` must use the connection in `config.attributes["connection"]` when there is one, which is how the upgrade runs on the app's own engine:

```python
def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    ...  # the usual engine_from_config path, for the alembic command line
```

## `app/services/auth.py`

```python
from localapps_core.auth import Auth

from app.config import NAME, PROJECT_ROOT

auth = Auth(NAME, PROJECT_ROOT / ".env")
```

Business logic and routers use `auth.password_is_set()`, `auth.check_password(...)`, `auth.set_password(...)`, `auth.clear_password()`, `auth.issue_token()` and the constants `auth.COOKIE_NAME`, `auth.SESSION_DAYS`, `auth.MIN_LENGTH`. A password that cannot be set raises `auth.PasswordError` with a message fit for the screen.

## `app/services/backup.py`

```python
from localapps_core.backup import BackupError, Backups, Export

backups = Backups("notes-", Export("export-", ".csv", write_export))
```

`write_export(session, path, now)` is the app's own function that writes the second file; leave `Export` out for a database copy alone. Then `backups.create_backup(session, directory)`, `backups.list_backups(directory)` and `backups.restore_backup(path, database, migrations, after=ensure_defaults)`.

## `app/main.py`

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI
from localapps_core.web.auth import Login
from localapps_core.web.health import health_router

from app.db import database
from app.services.auth import auth
from app.services.migrations import migrations
from app.templating import templates

login = Login(auth, templates)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    migrations.upgrade_to_head(database.get_engine())
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Notes", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", ...)
    app.middleware("http")(login.require_login)
    app.include_router(login.router)
    app.include_router(health_router(database, migrations))
    ...  # the app's own routers
    return app
```

The login page is the app's own template, `templates/auth/login.html`, because it carries the app's name, icon and stylesheet. It is rendered with `next`, `error` and `session_days`, and must post `password` and `next` to `/login`. A Log out button posts to `/logout`. Templates can read `request.state.logged_in`.

A route that changes the password itself (a Settings page) sets a fresh cookie on its response with `login.set_session_cookie(request, response, auth.issue_token())`, so the device that changed it stays logged in.

## `app/__main__.py` and `app/set_password.py`

```python
from localapps_core.web.launcher import run

from app.config import get_settings

if __name__ == "__main__":
    run(get_settings())
```

```python
import sys

from localapps_core.cli import set_password_main

from app.services.auth import auth

if __name__ == "__main__":
    sys.exit(set_password_main(auth, "Notes"))
```

`python -m app` serves the app; `python -m app.set_password` asks for a password twice without echo, and `--clear` removes it.

## The app's tests

Point the settings at a scratch database before the app is imported, and give each test an empty `.env`, so the real password is never read:

```python
os.environ["NOTES_DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"


@pytest.fixture(autouse=True)
def fresh_database(monkeypatch):
    monkeypatch.setattr(auth, "env_file", scratch_env)
    get_settings.cache_clear()
    database.reset_engine()
    ...  # delete the scratch database and its -wal and -shm files
    migrations.upgrade_to_head(database.get_engine())
    yield
    database.reset_engine()
```

`Auth` methods also take a `path` argument for a single call against another file.
