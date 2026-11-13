"""The login routes and middleware, and /health, through a real FastAPI application."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

import localapps_core
from tests.conftest import PASSWORD, Project, build_app


def test_open_when_no_password(client: TestClient) -> None:
    assert client.get("/").json() == {"logged_in": False}
    assert client.post("/notes").status_code == 200
    r = client.get("/login?next=/notes", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/notes"


def test_login_required_once_a_password_is_set(client: TestClient, migrated: Project) -> None:
    auth = migrated.auth
    auth.set_password(PASSWORD)
    r = client.get("/notes", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/notes"
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"  # no next for the home page
    r = client.post("/notes", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert migrated.notes() == []  # the post never reached the app

    page = client.get("/login?next=/notes")
    assert page.status_code == 200 and 'value="/notes"' in page.text and "90 days" in page.text
    r = client.post("/login", data={"password": "wrong", "next": "/notes"})
    assert r.status_code == 401 and "not right" in r.text and 'value="/notes"' in r.text

    r = client.post("/login", data={"password": PASSWORD, "next": "/notes"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/notes"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("notes_session=") and "HttpOnly" in cookie
    assert "SameSite=lax" in cookie and "Max-Age=7776000" in cookie and "Path=/" in cookie
    assert "Secure" not in cookie  # plain HTTP
    assert client.get("/").json() == {"logged_in": True}
    r = client.get("/login", follow_redirects=False)  # already in: straight through
    assert r.status_code == 303 and r.headers["location"] == "/"

    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/", follow_redirects=False).status_code == 303


def test_public_paths_need_no_login(client: TestClient, migrated: Project) -> None:
    migrated.auth.set_password(PASSWORD)
    assert client.get("/health").status_code == 200
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/sw.js", follow_redirects=False).status_code == 404  # open, just absent
    assert client.get("/staticky", follow_redirects=False).status_code == 303


def test_htmx_requests_get_a_401_that_navigates_the_whole_page(
    client: TestClient, migrated: Project
) -> None:
    migrated.auth.set_password(PASSWORD)
    r = client.get("/notes", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login"


def test_login_never_redirects_to_another_site(client: TestClient, migrated: Project) -> None:
    migrated.auth.set_password(PASSWORD)
    r = client.post(
        "/login",
        data={"password": PASSWORD, "next": "https://evil.example/"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/"
    client.post("/logout")
    assert 'value="/"' in client.get("/login?next=//evil.example").text


def test_session_cookie_is_secure_over_https(migrated: Project) -> None:
    migrated.auth.set_password(PASSWORD)
    with TestClient(build_app(migrated), base_url="https://testserver") as https:
        r = https.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        assert r.status_code == 303 and "Secure" in r.headers["set-cookie"]
        assert https.get("/").json() == {"logged_in": True}


def test_a_day_old_cookie_is_refreshed_and_a_fresh_one_is_not(
    client: TestClient, migrated: Project
) -> None:
    auth = migrated.auth
    auth.set_password(PASSWORD)
    fresh = auth.issue_token()
    assert fresh is not None
    client.cookies.set(auth.COOKIE_NAME, fresh)
    assert "set-cookie" not in client.get("/").headers

    old = auth.issue_token(datetime.now(UTC) - timedelta(days=2))
    assert old is not None
    client.cookies.set(auth.COOKIE_NAME, old)
    r = client.get("/")
    assert r.json() == {"logged_in": True}
    assert r.headers["set-cookie"].startswith("notes_session=")
    assert old not in r.headers["set-cookie"]


def test_changing_the_password_logs_other_devices_out(
    client: TestClient, migrated: Project
) -> None:
    migrated.auth.set_password(PASSWORD)
    client.post("/login", data={"password": PASSWORD})
    assert client.get("/").json() == {"logged_in": True}
    migrated.auth.set_password("a different password")
    assert client.get("/", follow_redirects=False).status_code == 303


def test_health_reports_ok_at_the_newest_migration(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "schema_version": "0002", "head_version": "0002"}


def test_health_is_503_when_a_migration_is_pending(project: Project) -> None:
    with TestClient(build_app(project)) as fresh:  # a database that was never migrated
        r = fresh.get("/health")
        assert r.status_code == 503
        assert r.json() == {
            "status": "migration-pending",
            "schema_version": None,
            "head_version": "0002",
        }
        project.migrations.upgrade(project.database.get_engine(), "0001")
        r = fresh.get("/health")
        assert r.status_code == 503 and r.json()["schema_version"] == "0001"
        project.migrations.upgrade_to_head(project.database.get_engine())
        assert fresh.get("/health").status_code == 200


def test_only_the_web_package_imports_fastapi() -> None:
    root = Path(localapps_core.__file__).parent
    web_imports = ("import fastapi", "from fastapi", "import starlette", "from starlette")
    offenders = [
        path.name
        for path in root.glob("*.py")  # the top level only; web/ is the exception
        if any(
            line.strip().startswith(web_imports)
            for line in path.read_text(encoding="utf-8").splitlines()
        )
    ]
    assert offenders == []
    assert (root / "web" / "auth.py").exists()
