"""Push notifications: the VAPID key, the subscription table in both its forms, sending, the
routes and the reminder check. Nothing here reaches a push service: `Push.deliver`, the one
function that POSTs to one, is replaced with a fake, and the test of `deliver` itself replaces
the library call inside it."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from pywebpush import WebPushException
from sqlalchemy import DateTime, Integer
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

import localapps_core.push as push_module
from localapps_core.push import (
    TABLE_NAME,
    TEST_TTL,
    TIMEOUT_SECONDS,
    InvalidSubscription,
    Message,
    Push,
    SubscriptionColumns,
    TextSubscriptionColumns,
    device_label,
    due_slot,
    in_window,
    utc_text,
)
from localapps_core.web.auth import Login
from localapps_core.web.push import (
    PAGE_SCRIPT,
    SCRIPTS_DIR,
    WORKER_SCRIPT,
    push_api_router,
    push_router,
)
from tests.conftest import PASSWORD, Project

ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Mobile Safari/537.36"
)
CONTACT = "https://notes.example"
UTC_TEXT = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
Delivery = tuple[str, str, int]  # endpoint, payload, ttl


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class DatedBase(DeclarativeBase):
    """A base that gives every table a numeric key and a creation time."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class DatedSubscription(SubscriptionColumns, DatedBase):
    __tablename__ = TABLE_NAME


class TextBase(DeclarativeBase):
    """A base that adds nothing: the table is keyed on the endpoint, with times as text."""


class TextSubscription(TextSubscriptionColumns, TextBase):
    __tablename__ = TABLE_NAME


@dataclass
class Kit:
    """One app's push set-up over a scratch database, in one of the two table forms."""

    project: Project
    push: Push[Any]
    text: bool

    def session(self) -> Session:
        return self.project.database.get_session_factory()()


@pytest.fixture(params=["dated", "text"])
def kit(request: pytest.FixtureRequest, project: Project) -> Kit:
    text = request.param == "text"
    model: type[Any] = TextSubscription if text else DatedSubscription
    model.metadata.create_all(project.database.get_engine())
    push: Push[Any] = Push(project.auth, model, title="Notes", contact=lambda: CONTACT)
    return Kit(project=project, push=push, text=text)


@pytest.fixture
def session(kit: Kit) -> Iterator[Session]:
    with kit.session() as s:
        yield s


@pytest.fixture
def deliveries(kit: Kit, monkeypatch: pytest.MonkeyPatch) -> list[Delivery]:
    """Record every would-be push. An endpoint containing "gone" answers 410 and "missing"
    404; "broken" answers 500; "offline" cannot be reached at all; "garbled" has keys nothing
    can be encrypted to."""
    sent: list[Delivery] = []

    def fake(subscription: Any, payload: str, private_key: str, ttl: int) -> None:
        assert private_key == kit.push.vapid_private_key()
        endpoint: str = subscription.endpoint
        if "gone" in endpoint:
            raise WebPushException("gone", SimpleNamespace(status_code=410, text=""))
        if "missing" in endpoint:
            raise WebPushException("missing", SimpleNamespace(status_code=404, text=""))
        if "broken" in endpoint:
            raise WebPushException("broken", SimpleNamespace(status_code=500, text=""))
        if "offline" in endpoint:
            raise ConnectionError("no network")
        if "garbled" in endpoint:
            raise ValueError("not a key")
        sent.append((endpoint, payload, ttl))

    monkeypatch.setattr(kit.push, "deliver", fake)
    return sent


def _sub(endpoint: str = "https://push.example/one") -> dict[str, object]:
    return {"endpoint": endpoint, "keys": {"p256dh": "BPUBLIC", "auth": "AUTH"}}


# --- the key ----------------------------------------------------------------------------------


def test_the_key_is_made_on_first_use_and_named_after_the_app(kit: Kit, tmp_path: Path) -> None:
    push = kit.push
    assert push.PRIVATE_KEY == "NOTES_VAPID_PRIVATE_KEY"
    env = tmp_path / ".env"
    env.write_text("NOTES_PORT=8050\n", encoding="utf-8")
    private = push.vapid_private_key(env)
    text = env.read_text(encoding="utf-8")
    assert f"NOTES_VAPID_PRIVATE_KEY={private}" in text and "NOTES_PORT=8050" in text
    public = push.vapid_public_key(env)
    assert len(public) == 87 and public.startswith("B")  # 65 bytes, an uncompressed point


def test_a_key_that_exists_is_never_made_again(
    kit: Kit, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    push = kit.push
    env = tmp_path / ".env"
    private = push.vapid_private_key(env)
    public = push.vapid_public_key(env)
    before = env.read_text(encoding="utf-8")

    def never() -> None:
        raise AssertionError("a second key was generated")

    monkeypatch.setattr(push_module.Vapid02, "generate_keys", lambda self: never())
    for _ in range(3):
        assert push.vapid_private_key(env) == private
        assert push.vapid_public_key(env) == public
    assert env.read_text(encoding="utf-8") == before

    # A key given in the process environment counts as existing: nothing is made or written.
    elsewhere = tmp_path / "other.env"
    monkeypatch.setenv("NOTES_VAPID_PRIVATE_KEY", private)
    assert push.vapid_private_key(elsewhere) == private
    assert not elsewhere.exists()


def test_the_key_is_kept_in_the_same_env_file_as_the_login(kit: Kit) -> None:
    kit.project.auth.set_password(PASSWORD)
    private = kit.push.vapid_private_key()
    text = kit.project.auth.env_file.read_text(encoding="utf-8")
    assert f"NOTES_VAPID_PRIVATE_KEY={private}" in text and "NOTES_PASSWORD_HASH=" in text
    assert kit.project.auth.check_password(PASSWORD)  # the login's lines were left alone


# --- devices ----------------------------------------------------------------------------------


def test_device_labels() -> None:
    assert device_label(ANDROID_UA) == "Android · Chrome"
    edge = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0 Safari/537.36 Edg/130.0"
    assert device_label(edge) == "Windows · Edge"
    assert device_label(None) == "Unknown device · browser"
    assert Push.device_label(ANDROID_UA) == "Android · Chrome"


def test_subscribing_twice_keeps_one_row(kit: Kit, session: Session) -> None:
    push = kit.push
    first = push.subscribe(session, " https://push.example/a ", "K1", "A1", "phone")
    assert first.endpoint == "https://push.example/a"
    push.subscribe(session, "https://push.example/a", "K2", "A2", "phone again")
    [row] = push.subscriptions(session)
    assert (row.p256dh, row.auth, row.label) == ("K2", "A2", "phone again")
    assert row.last_sent_at is None
    if kit.text:
        assert UTC_TEXT.match(row.created_at)
    else:
        assert isinstance(row.created_at, datetime)
    found = push.find_subscription(session, "https://push.example/a")
    assert found is not None and found.label == "phone again"
    assert push.unsubscribe(session, "https://push.example/a") is True
    assert push.unsubscribe(session, "https://push.example/a") is False
    assert push.subscriptions(session) == []


def test_devices_are_listed_oldest_first(kit: Kit, session: Session) -> None:
    push = kit.push
    for name in ("c", "a", "b"):
        row = push.subscribe(session, f"https://push.example/{name}", "K", "A", name)
        stamp = datetime(2026, 1, {"c": 1, "a": 2, "b": 3}[name], 9, 0)
        row.created_at = utc_text(stamp) if kit.text else stamp
    session.commit()
    assert [row.label for row in push.subscriptions(session)] == ["c", "a", "b"]


@pytest.mark.parametrize(
    ("endpoint", "p256dh", "auth", "words"),
    [
        ("http://push.example/a", "K", "A", "must be an https address"),
        ("ftp://push.example/a", "K", "A", "must be an https address"),
        ("", "K", "A", "endpoint is missing"),
        ("https://push.example/" + "a" * 2000, "K", "A", "endpoint is missing or too long"),
        ("https://push.example/a", "  ", "A", "p256dh key is missing"),
        ("https://push.example/a", "K", "", "auth key is missing"),
    ],
)
def test_a_subscription_the_server_should_not_post_to_is_refused(
    kit: Kit, session: Session, endpoint: str, p256dh: str, auth: str, words: str
) -> None:
    with pytest.raises(InvalidSubscription, match=words):
        kit.push.subscribe(session, endpoint, p256dh, auth, "phone")
    assert kit.push.subscriptions(session) == []
    assert issubclass(kit.push.InvalidSubscription, ValueError)


# --- sending ----------------------------------------------------------------------------------


def test_send_reaches_every_device_and_drops_the_gone(
    kit: Kit, session: Session, deliveries: list[Delivery]
) -> None:
    push = kit.push
    names = ("one", "gone", "missing", "broken", "offline", "garbled", "two")
    for name in names:
        push.subscribe(session, f"https://push.example/{name}", "K", "A", name)
    now = datetime(2026, 1, 31, 7, 0, 5)
    message = Message(title="Notes", body="Something happened.", tag="event", ttl=3600)
    result = push.send(session, message, now=now)

    assert (result.sent, result.failed, result.removed) == (2, 3, 2)
    assert sorted(e.split(":")[0] for e in result.errors) == ["broken", "garbled", "offline"]
    assert "broken: 500" in result.errors and "garbled: ValueError: not a key" in result.errors
    assert [(endpoint.rsplit("/", 1)[1], ttl) for endpoint, _, ttl in deliveries] == [
        ("one", 3600),
        ("two", 3600),
    ]
    # What the device receives is the four fields and nothing else; the time to live went to
    # the push service.
    assert json.loads(deliveries[0][1]) == {
        "title": "Notes",
        "body": "Something happened.",
        "url": "/",
        "tag": "event",
    }
    session.expire_all()
    stamps = {row.label: row.last_sent_at for row in push.subscriptions(session)}
    assert set(stamps) == {"one", "two", "broken", "offline", "garbled"}  # the gone are deleted
    assert stamps["broken"] is None and stamps["offline"] is None and stamps["garbled"] is None
    expected = utc_text(now) if kit.text else now
    assert stamps["one"] == expected and stamps["two"] == expected


def test_send_with_no_devices_does_nothing_and_makes_no_key(
    kit: Kit, session: Session, deliveries: list[Delivery]
) -> None:
    result = kit.push.send(session, kit.push.test_message())
    assert (result.sent, result.failed, result.removed, result.errors) == (0, 0, 0, [])
    assert deliveries == [] and not kit.project.auth.env_file.exists()


def test_last_sent_defaults_to_the_present(
    kit: Kit, session: Session, deliveries: list[Delivery]
) -> None:
    kit.push.subscribe(session, "https://push.example/one", "K", "A", "phone")
    assert kit.push.send(session, kit.push.test_message()).sent == 1
    [row] = kit.push.subscriptions(session)
    if kit.text:
        assert UTC_TEXT.match(row.last_sent_at)
    else:
        assert abs(datetime.now() - row.last_sent_at) < timedelta(minutes=1)


def test_the_test_message(kit: Kit) -> None:
    assert kit.push.test_message() == Message(
        title="Notes",
        body="Notifications are working on this device.",
        url="/settings",
        tag="test",
        ttl=TEST_TTL,
    )
    elsewhere: Push[Any] = Push(
        kit.project.auth, kit.push.model, title="Notes", contact=lambda: CONTACT, test_url="/"
    )
    assert elsewhere.test_message().url == "/"


def test_deliver_posts_once_with_the_key_the_claim_and_a_timeout(
    kit: Kit, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(push_module, "webpush", lambda **kwargs: calls.append(kwargs))
    row = kit.push.subscribe(session, "https://push.example/one", "BPUBLIC", "AUTH", "phone")
    result = kit.push.send(session, Message(title="Notes", body="Hello.", tag="t", ttl=90))
    assert result.sent == 1
    [call] = calls
    assert call == {
        "subscription_info": {
            "endpoint": row.endpoint,
            "keys": {"p256dh": "BPUBLIC", "auth": "AUTH"},
        },
        "data": '{"title": "Notes", "body": "Hello.", "url": "/", "tag": "t"}',
        "vapid_private_key": kit.push.vapid_private_key(),
        "vapid_claims": {"sub": CONTACT},
        "ttl": 90,
        "headers": {"Urgency": "high"},  # or an idle phone holds the message until it wakes
        "timeout": TIMEOUT_SECONDS,
    }
    assert TIMEOUT_SECONDS == 10.0


def test_utc_text() -> None:
    exact = datetime(2026, 1, 31, 7, 0, 5, 123456, tzinfo=UTC)
    assert utc_text(exact) == "2026-01-31T07:00:05.123Z"
    local = datetime(2026, 1, 31, 7, 0, 5)  # no time zone: the server's local time
    assert utc_text(local) == utc_text(local.astimezone())
    assert UTC_TEXT.match(utc_text())


def test_view_is_plain_data(kit: Kit, session: Session) -> None:
    kit.push.subscribe(session, "https://push.example/one", "K", "A", "phone")
    view = kit.push.view(session)
    assert view["public_key"] == kit.push.vapid_public_key()
    [device] = view["devices"]
    assert set(device) == {"endpoint", "label", "created_at", "last_sent_at"}
    assert (device["endpoint"], device["label"]) == ("https://push.example/one", "phone")
    assert device["last_sent_at"] is None


# --- the routes -------------------------------------------------------------------------------

SW_JS = (
    'importScripts("/static/push/worker.js");\nlocalappsPush({ title: "Notes", tag: "notes" });\n'
)


def _app(kit: Kit, *, api: bool = False, **options: Any) -> FastAPI:
    """An app wired the way the recipe says: the push routes first, the static mount after."""
    project = kit.project
    (project.root / "static" / "sw.js").write_bytes(SW_JS.encode("utf-8"))
    login = Login(project.auth, Jinja2Templates(directory=str(project.root / "templates")))
    app = FastAPI(docs_url=None, redoc_url=None)
    app.middleware("http")(login.require_login)
    app.include_router(login.router)
    make = push_api_router if api else push_router
    app.include_router(
        make(
            kit.push,
            project.database.get_db,
            service_worker=project.root / "static" / "sw.js",
            **options,
        )
    )
    app.mount("/static", StaticFiles(directory=str(project.root / "static")), name="static")
    return app


def test_page_routes(kit: Kit, session: Session, deliveries: list[Delivery]) -> None:
    push = kit.push
    with TestClient(_app(kit)) as client:
        r = client.post("/notifications/subscribe", json=_sub(), headers={"User-Agent": ANDROID_UA})
        assert r.status_code == 204 and r.content == b""
        [row] = push.subscriptions(session)
        assert (row.endpoint, row.label) == ("https://push.example/one", "Android · Chrome")

        r = client.post("/notifications/subscribe", json=_sub("http://push.example/plain"))
        assert r.status_code == 422
        assert r.json() == {"detail": "The subscription's endpoint must be an https address"}
        assert client.post("/notifications/subscribe", json={"endpoint": "x"}).status_code == 422

        r = client.post("/notifications/test", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/settings?test=1&failed=0&gone=0"
        [(endpoint, payload, ttl)] = deliveries
        assert endpoint == "https://push.example/one" and ttl == TEST_TTL
        assert json.loads(payload)["body"] == "Notifications are working on this device."

        client.post("/notifications/subscribe", json=_sub("https://push.example/gone"))
        r = client.post("/notifications/test", follow_redirects=False)
        assert r.headers["location"] == "/settings?test=1&failed=0&gone=1"

        r = client.post("/notifications/unsubscribe", json={"endpoint": "https://push.example/one"})
        assert r.status_code == 204
        session.expire_all()
        assert push.subscriptions(session) == []
        r = client.post("/notifications/unsubscribe", json={"endpoint": "https://push.example/one"})
        assert r.status_code == 204  # already off: not an error

        client.post("/notifications/subscribe", json=_sub("https://push.example/two"))
        r = client.post(
            "/notifications/forget",
            data={"endpoint": "https://push.example/two"},
            follow_redirects=False,
        )
        assert r.status_code == 303 and r.headers["location"] == "/settings?device=forgotten"
        session.expire_all()
        assert push.subscriptions(session) == []


def test_page_routes_can_come_back_to_another_page(kit: Kit, deliveries: list[Delivery]) -> None:
    with TestClient(_app(kit, settings_url="/preferences")) as client:
        r = client.post("/notifications/test", follow_redirects=False)
        assert r.headers["location"] == "/preferences?test=0&failed=0&gone=0"
        r = client.post("/notifications/forget", data={"endpoint": "x"}, follow_redirects=False)
        assert r.headers["location"] == "/preferences?device=forgotten"


@pytest.mark.parametrize("api", [False, True])
def test_only_the_scripts_are_served_without_a_login(kit: Kit, api: bool) -> None:
    kit.project.auth.set_password(PASSWORD)
    with TestClient(_app(kit, api=api)) as client:
        r = client.get("/sw.js")
        assert r.status_code == 200 and r.text == SW_JS
        assert r.headers["cache-control"] == "no-cache"
        assert "javascript" in r.headers["content-type"]
        for path, words in ((PAGE_SCRIPT, "LocalappsPush"), (WORKER_SCRIPT, "localappsPush")):
            r = client.get(path)  # declared before the static mount, so they win over it
            assert r.status_code == 200 and words in r.text
            assert r.headers["cache-control"] == "no-cache"
            assert "javascript" in r.headers["content-type"]
        assert client.get("/static/app.css").status_code == 200  # the app's own files still are

        prefix = "/api/notifications" if api else "/notifications"
        for path, body in (
            (f"{prefix}/subscribe", _sub()),
            (f"{prefix}/unsubscribe", {"endpoint": "https://push.example/one"}),
            (f"{prefix}/test", None),
        ):
            assert client.post(path, json=body, follow_redirects=False).status_code == 303
        if api:
            assert client.get(prefix, follow_redirects=False).status_code == 303
        else:
            r = client.post(f"{prefix}/forget", data={"endpoint": "x"}, follow_redirects=False)
            assert r.headers["location"] == "/login"


def test_without_a_service_worker_file_the_app_serves_its_own(kit: Kit) -> None:
    app = FastAPI()
    app.include_router(push_router(kit.push, kit.project.database.get_db))
    with TestClient(app) as client:
        assert client.get("/sw.js").status_code == 404
        assert client.get(WORKER_SCRIPT).status_code == 200


def test_api_routes(kit: Kit, session: Session, deliveries: list[Delivery]) -> None:
    push = kit.push
    with TestClient(_app(kit, api=True)) as client:
        r = client.get("/api/notifications")
        assert r.status_code == 200
        assert r.json() == {"notifications": {"public_key": push.vapid_public_key(), "devices": []}}

        r = client.post(
            "/api/notifications/subscribe", json=_sub(), headers={"User-Agent": ANDROID_UA}
        )
        assert r.status_code == 200
        [device] = r.json()["notifications"]["devices"]
        assert (device["endpoint"], device["label"]) == (
            "https://push.example/one",
            "Android · Chrome",
        )
        assert device["last_sent_at"] is None and device["created_at"]

        r = client.post("/api/notifications/subscribe", json=_sub("http://push.example/plain"))
        assert r.status_code == 422 and "https address" in r.json()["detail"]

        client.post("/api/notifications/subscribe", json=_sub("https://push.example/gone"))
        r = client.post("/api/notifications/test")
        assert r.json()["result"] == {"sent": 1, "failed": 0, "removed": 1}
        [device] = r.json()["notifications"]["devices"]  # the view after the gone one went
        assert device["last_sent_at"]
        assert [ttl for _, _, ttl in deliveries] == [TEST_TTL]

        r = client.post(
            "/api/notifications/unsubscribe", json={"endpoint": "https://push.example/one"}
        )
        assert r.json() == {"notifications": {"public_key": push.vapid_public_key(), "devices": []}}
        session.expire_all()
        assert push.subscriptions(session) == []


def test_api_routes_take_the_apps_own_view_and_error(kit: Kit) -> None:
    class AppError(Exception):
        pass

    def view(db: Session) -> dict[str, Any]:
        return {**kit.push.view(db), "times": ["08:00"]}

    app = _app(kit, api=True, view=view, invalid=lambda exc: AppError(str(exc)), prefix="/api/push")

    @app.exception_handler(AppError)
    async def handle(_request: Any, exc: AppError) -> Any:
        from fastapi.responses import JSONResponse

        return JSONResponse({"error": "validation", "message": str(exc)}, status_code=400)

    with TestClient(app) as client:
        assert client.get("/api/push").json()["notifications"]["times"] == ["08:00"]
        r = client.post("/api/push/test")
        assert r.json()["notifications"]["times"] == ["08:00"]
        assert r.json()["result"] == {"sent": 0, "failed": 0, "removed": 0}
        r = client.post("/api/push/subscribe", json=_sub("http://push.example/plain"))
        assert r.status_code == 400
        assert r.json() == {
            "error": "validation",
            "message": "The subscription's endpoint must be an https address",
        }
        assert client.get("/api/notifications").status_code == 404


def test_the_scripts_are_part_of_the_package() -> None:
    page = (SCRIPTS_DIR / "page.js").read_text(encoding="utf-8")
    worker = (SCRIPTS_DIR / "worker.js").read_text(encoding="utf-8")
    assert "root.LocalappsPush = {" in page and '"/notifications/subscribe"' in page
    assert "self.localappsPush = function" in worker and "showNotification" in worker
    pyproject = (Path(push_module.__file__).parent.parent / "pyproject.toml").read_text("utf-8")
    assert '"static/push/*.js"' in pyproject  # or an installed copy would have no scripts


# --- reminders --------------------------------------------------------------------------------


def test_in_window_is_from_the_time_until_three_hours_later() -> None:
    at = time(7, 0)
    day = datetime(2026, 1, 31)
    assert not in_window(day.replace(hour=6, minute=59), at)
    assert in_window(day.replace(hour=7, minute=0), at)
    assert in_window(day.replace(hour=9, minute=59), at)
    assert not in_window(day.replace(hour=10, minute=0), at)  # too late: skipped, not sent
    assert in_window(day.replace(hour=7, minute=30), at, timedelta(hours=1))
    assert not in_window(day.replace(hour=8, minute=0), at, timedelta(hours=1))
    # A time late in the evening is not carried over midnight.
    assert in_window(day.replace(hour=23, minute=59), time(23, 0))
    assert not in_window(datetime(2026, 2, 1, 0, 30), time(23, 0))


def test_due_slot_handles_each_time_once_and_skips_the_late() -> None:
    times = [time(18, 0), time(8, 0), time(13, 0)]  # in any order
    day = datetime(2026, 1, 31)
    assert due_slot(day.replace(hour=7, minute=59), times, None) is None
    assert due_slot(day.replace(hour=8, minute=0), times, None) == "2026-01-31T08:00"
    assert due_slot(day.replace(hour=8, minute=1), times, "2026-01-31T08:00") is None  # once
    assert due_slot(day.replace(hour=8, minute=1), times, "2026-01-30T18:00") == "2026-01-31T08:00"
    assert due_slot(day.replace(hour=11, minute=30), times, None) is None  # down all morning
    assert due_slot(day.replace(hour=13, minute=5), times, "2026-01-31T08:00") == "2026-01-31T13:00"
    # Two times inside one window: only the later one is handled, and then neither.
    close = [time(8, 0), time(9, 0)]
    assert due_slot(day.replace(hour=9, minute=30), close, None) == "2026-01-31T09:00"
    assert due_slot(day.replace(hour=9, minute=31), close, "2026-01-31T09:00") is None
    assert due_slot(day.replace(hour=9, minute=30), [], None) is None
    assert due_slot(day.replace(hour=8, minute=30), times, None, timedelta(minutes=10)) is None
