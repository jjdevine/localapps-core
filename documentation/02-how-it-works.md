# 02 · How it works

## Secrets (`envfile.py`)

Secrets live in the app's `.env`, never in its database, so they are in no backup and no export. `read_value` returns a line's value from the file, falling back to the process environment; `write_values` replaces or adds the named lines and leaves every other line alone, and a `None` or empty value removes the line. The file is left owner-read-only afterwards and is made writable only for the duration of an update.

## The login (`auth.py`, `web/auth.py`)

### What is stored

Two lines in `.env`, for an app called `notes`:

| Line | What it is |
| --- | --- |
| `NOTES_PASSWORD_HASH` | `scrypt$<salt>$<hash>`: a salted scrypt hash (`hashlib.scrypt`, n=2¹⁴, r=8, p=1). The password itself is never written anywhere |
| `NOTES_SESSION_SECRET` | 32 random bytes in hex, used to sign session cookies |

With no `NOTES_PASSWORD_HASH` line the app is open: no login page, every route reachable. That suits a single PC bound to localhost and nothing else, so on a server the password is set before the service first starts.

A password is at least eight characters and cannot start or end with a space. Every `set_password` writes a new secret as well as the new hash, so a token signed under the old secret no longer verifies: changing the password logs every device out.

### The session cookie

After a correct password, `POST /login` sets a cookie called `notes_session` whose value is `<expiry unix time>.<HMAC-SHA256 of that number under the secret>`. It is `HttpOnly`, `SameSite=Lax`, `Path=/`, and lasts 90 days. There is no session table: a cookie is valid if its signature matches and its expiry has not passed, which is why rotating the secret is enough to end every session.

A device in regular use never logs in again. Once a valid cookie is more than a day old the middleware issues a new one with the response, pushing the expiry another 90 days out. A device left unused for 90 days is asked for the password again.

The cookie is marked `Secure` only when the request itself arrived over HTTPS. Behind a proxy on localhost, uvicorn takes the scheme from `X-Forwarded-Proto`, so an HTTPS entry point gets a cookie the browser only sends over HTTPS, while a plain-HTTP entry point on the LAN keeps a cookie it can send back.

The cookie is named after the app because several apps may share one host name on different ports, and browsers do not separate cookies by port.

### The middleware

`Login.require_login` sees every request before any route:

1. No password set: pass through, `request.state.logged_in = False`.
2. Otherwise verify the cookie; `request.state.logged_in` tells templates whether to show a Log out button.
3. A valid cookie, or a public path: run the request, and refresh the cookie if it is a day old. The public paths are `/login`, `/health`, `/sw.js` and anything under `/static/`.
4. Anything else: a request from HTMX (header `HX-Request: true`) gets `401` with `HX-Redirect: /login`, which HTMX follows as a full-page navigation rather than swapping a login form into the page; a normal `GET` is redirected to `/login?next=<path>`; any other method is redirected to `/login`.

`/login` passes `next` through `safe_next`, which accepts only a path on the same site, so a crafted link cannot send someone to another host after they log in. `POST /logout` deletes the cookie. A logged-in browser that visits `/login` is sent on to where it was going.

## The database and migrations (`db.py`, `migrations.py`)

`Database` makes one engine the first time it is needed, with `journal_mode=WAL` and `foreign_keys=ON` applied to every connection, and creates the folder for the SQLite file if it is missing. `reset_engine` disposes of it and every connection it holds; tests and restore use that.

`Migrations.upgrade_to_head` runs Alembic on the app's own engine, inside one transaction. An app calls it when it starts, so deploying new code and bringing an older database up to date are the same step. `current_revision` is what the database is at, `head_revision` is the newest revision in the code, and `is_current` compares them.

## The health check (`web/health.py`)

`GET /health` needs no login and returns `{"status": ..., "schema_version": ..., "head_version": ...}`. It answers 200 with status `ok` when the database is at the newest migration, and 503 with status `migration-pending` when it is not, including a database that has never been migrated. A deploy script that requires a 200 therefore treats a deploy that left the schema behind as failed. The response gives away only the schema revision.

## Backups (`backup.py`)

A backup is a copy of the database made with `VACUUM INTO`: SQLite reads through its own locking, so the app keeps running, and the result is one consistent file with no `-wal` or `-shm` sidecar. `create_backup` commits the session first, because the copy holds committed data only.

Files are named `<prefix>YYYY-MM-DD-HHMM.db` in local time; a second backup in the same minute gets `-SS` added. An app can give an `Export` to have a second file written beside each copy, named with the same stamp, so the data is readable without the app. After each backup the newest `keep` (24 unless told otherwise) are kept and older ones are deleted, each with its export. Ordering is by the time in the name, not the name itself, because `-SS` names would otherwise sort before the bare stamp.

`list_backups` returns the copies newest first. With `schema_versions=True` it opens each one read-only, runs SQLite's `quick_check` and reads its Alembic revision; a file that fails is listed with no version rather than left out.

`restore_backup` replaces the live database with a backup:

1. check the file opens, passes `quick_check` and is not from a newer version of the app than the code knows;
2. refuse if it is the live database itself;
3. close every connection, checkpoint the live database and copy it aside as `<prefix>before-restore-<stamp>.db`;
4. delete the live `-wal` and `-shm` files, which belong to the database being replaced and would otherwise be replayed into the restored one;
5. copy the backup into place, reopen, migrate to head, and call the app's `after` function with a session.

The copy set aside is not a backup: it is not listed and never pruned. A refused restore changes nothing. The caller must not hold an open session.

Every failure is a `BackupError` whose message can be shown on screen.
