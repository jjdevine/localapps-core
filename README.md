# localapps-core

The parts every small self-hosted FastAPI + SQLite app repeats, written once: a one-password login with a long-lived cookie, secrets in `.env`, settings, the database engine, migrations at start-up, a `/health` route, backup and restore, and push notifications.

It is for apps of one particular shape: a single user or household, one SQLite file, server-rendered pages, run as a service on a home server behind a reverse proxy. It is not a framework; an app stays an ordinary FastAPI application and uses whichever parts it wants.

## Install

Pin an exact tag in the app's `pyproject.toml`:

```toml
dependencies = [
    "localapps-core @ git+https://github.com/jjdevine/localapps-core.git@v0.2.2",
]
```

An app that sends push notifications asks for the extra that installs `pywebpush`: `"localapps-core[push] @ git+https://github.com/jjdevine/localapps-core.git@v0.2.2"`.

A fix is made once here and released as a new tag; each app then moves to it in a one-line change, so each app's own tests run against the new version before it is deployed.

## What is in it

| Module | What it gives an app |
| --- | --- |
| `localapps_core.settings` | `AppSettings`: host, port, data folder and database URL, read from `<NAME>_...` in the environment and `.env` |
| `localapps_core.envfile` | `read_value`, `write_values`: single lines of `.env`, where secrets live |
| `localapps_core.auth` | `Auth`: scrypt password hash, signed session tokens, everything named from the app's one name |
| `localapps_core.db` | `Database`: a lazily created engine with WAL and foreign keys on, a session factory, a request dependency |
| `localapps_core.migrations` | `Migrations`: run Alembic to head on the app's engine, report the current and newest revision |
| `localapps_core.backup` | `Backups`: `VACUUM INTO` copies with an optional export beside each, pruning, listing, restore |
| `localapps_core.cli` | `set_password_main`: set or clear the password from a terminal |
| `localapps_core.push` | `Push`: Web Push to the browsers that asked for it; the VAPID key in `.env`, the subscription table on the app's own metadata, sending with stale subscriptions cleared, and a once-per-time check for reminders |
| `localapps_core.web.auth` | `Login`: the `/login` and `/logout` routes and the middleware that requires the login |
| `localapps_core.web.health` | `health_router`: `/health`, 200 at the newest migration and 503 otherwise |
| `localapps_core.web.push` | `push_router`, `push_api_router`: the routes that turn a device on or off and send a test, and the two scripts a browser needs (the page script and the service-worker handlers) |
| `localapps_core.web.launcher` | `run`: serve the app with uvicorn on the configured host and port |

Only `localapps_core.web` imports FastAPI, so an app's business logic can use the rest without the web framework.

[documentation/01-wiring-an-app.md](documentation/01-wiring-an-app.md) shows the few lines each part needs in an app. [documentation/02-how-it-works.md](documentation/02-how-it-works.md) explains the login, the backups and the health check. [documentation/03-push-notifications.md](documentation/03-push-notifications.md) is the recipe for adding push notifications to an app, and how they work.

## Development

```sh
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"    # .venv/bin/python on Linux and macOS
```

Before any push, all four clean:

```sh
pytest
ruff check .
ruff format --check .
mypy
```

The same four run on every push and pull request (`.github/workflows/check.yml`).

## Releasing

1. Change `version` in `pyproject.toml` and `__version__` in `localapps_core/__init__.py`.
2. Commit, then tag the commit `v<version>` and push the tag.
3. In each app, change the tag in `pyproject.toml`, run its tests, and deploy.

Versions follow the usual three numbers: the last for a fix, the middle for something added, the first for a change an app has to adapt to.
