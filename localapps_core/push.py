"""Push notifications: the devices that asked for them, the VAPID key that signs what is sent,
and the one outbound request an app makes to deliver a message.

How Web Push works here. A browser that turns notifications on asks its push service (the
browser maker's own) for a subscription: a one-off URL plus two keys. The page posts that to
the app and it is kept in the app's `push_subscriptions` table. To notify the device the app
encrypts a small JSON message to those keys (RFC 8291) and POSTs it to that URL, signed with
the app's VAPID key (RFC 8292) so the push service knows the message came from the site the
browser subscribed to. The push service cannot read the message; it only wakes the browser,
whose service worker shows it. A 404 or 410 from the push service means the browser has let
the subscription go, and the row is deleted.

The VAPID private key lives in the app's .env like the other secrets, as
`<NAME>_VAPID_PRIVATE_KEY`. Every device's subscription is bound to it, so it is made once,
on first use, and never changed: a new key silently cuts off every device.

This module only delivers. What an app notifies about, when, and in what words is the app's
own business, in a module of its own. Nothing here imports FastAPI; the routes are in
`localapps_core.web.push`. It needs `pywebpush`, which the package's `push` extra installs.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid02
from py_vapid.utils import b64urlencode
from pywebpush import WebPushException, webpush
from sqlalchemy import DateTime, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from localapps_core.auth import Auth
from localapps_core.envfile import read_value, write_values

log = logging.getLogger(__name__)

TABLE_NAME = "push_subscriptions"
GONE_STATUSES = frozenset({404, 410})  # the push service says the subscription no longer exists
TIMEOUT_SECONDS = 10.0  # for each POST to a push service
TEST_TTL = 60  # a test message is not worth holding for a phone that is off
MAX_FIELD_LENGTH = 2000  # endpoints run to a few hundred characters; keys to under a hundred
REMINDER_WINDOW = timedelta(hours=3)  # a reminder not sent within this long of its time is skipped


class InvalidSubscription(ValueError):
    """A subscription that cannot be kept, with a message fit for the screen."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Message:
    """What the service worker shows. `title` is the app's name as it appears on the home
    screen; `url` is a path in the app, opened when the notification is tapped; messages with
    the same `tag` replace each other on the phone rather than piling up. `ttl` is how many
    seconds the push service holds the message for a phone that is off or out of signal: it
    is the app's choice, so there is no default."""

    title: str
    body: str
    url: str = "/"
    tag: str
    ttl: int

    def payload(self) -> str:
        """The JSON the device receives. The time to live goes to the push service, not in
        here."""
        return json.dumps(
            {"title": self.title, "body": self.body, "url": self.url, "tag": self.tag}
        )


@dataclass(slots=True)
class SendResult:
    sent: int = 0
    failed: int = 0
    removed: int = 0
    errors: list[str] = field(default_factory=list)


# --- the table --------------------------------------------------------------------------------
#
# The table is declared on the app's own metadata, by a class in the app that takes one of the
# two sets of columns below, and created by the app's own migration.


def utc_text(now: datetime | None = None) -> str:
    """A moment as UTC ISO-8601 text, `2026-01-31T07:00:00.000Z`: the present when `now` is
    None. A `now` without a time zone is taken to be the server's local time."""
    moment = datetime.now(UTC) if now is None else now.astimezone(UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _utc_text_now() -> str:
    return utc_text()


class SubscriptionColumns:
    """One browser that asked for push notifications: the push-service URL it was given and
    the two keys that encrypt messages to it. The label is a readable device name worked out
    from the User-Agent when it subscribed, so a Settings page can list devices.

    For an app whose declarative base gives every table a primary key and a `created_at`
    date-time. `last_sent_at` is the server's local time.

        class PushSubscription(SubscriptionColumns, Base):
            __tablename__ = TABLE_NAME
    """

    endpoint: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    p256dh: Mapped[str] = mapped_column(String(200), nullable=False)
    auth: Mapped[str] = mapped_column(String(50), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    if TYPE_CHECKING:
        created_at: Mapped[datetime]  # from the app's base

        def __init__(self, **kw: Any) -> None: ...

    def mark_sent(self, now: datetime | None = None) -> None:
        self.last_sent_at = now or datetime.now()


class TextSubscriptionColumns:
    """The same row for an app that keys its tables on a natural key and stores times as UTC
    ISO-8601 text: `endpoint` is the primary key, and `created_at` is declared here.

        class PushSubscription(TextSubscriptionColumns, Base):
            __tablename__ = TABLE_NAME
    """

    endpoint: Mapped[str] = mapped_column(Text, primary_key=True)
    p256dh: Mapped[str] = mapped_column(Text, nullable=False)
    auth: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=_utc_text_now)
    last_sent_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    if TYPE_CHECKING:

        def __init__(self, **kw: Any) -> None: ...

    def mark_sent(self, now: datetime | None = None) -> None:
        self.last_sent_at = utc_text(now)


# --- devices ----------------------------------------------------------------------------------

_PLATFORMS = (
    ("Android", "Android"),
    ("iPhone", "iPhone"),
    ("iPad", "iPad"),
    ("Windows", "Windows"),
    ("Mac OS", "Mac"),
    ("CrOS", "ChromeOS"),
    ("Linux", "Linux"),
)
_BROWSERS = (
    ("Edg/", "Edge"),
    ("SamsungBrowser", "Samsung Internet"),
    ("OPR/", "Opera"),
    ("Firefox/", "Firefox"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
)


def device_label(user_agent: str | None) -> str:
    """A short device name from the User-Agent, e.g. "Android · Chrome". The tuples are in
    order of specificity because Edge's string also says Chrome, and Chrome's says Safari."""
    ua = user_agent or ""
    platform = next((name for needle, name in _PLATFORMS if needle in ua), "Unknown device")
    browser = next((name for needle, name in _BROWSERS if needle in ua), "browser")
    return f"{platform} · {browser}"


def _checked(value: str, what: str) -> str:
    value = value.strip()
    if not value or len(value) > MAX_FIELD_LENGTH:
        raise InvalidSubscription(f"The subscription's {what} is missing or too long")
    return value


def _raw_private(vapid: Vapid02) -> str:
    number: int = vapid.private_key.private_numbers().private_value
    return str(b64urlencode(number.to_bytes(32, "big")))


class Push[S: SubscriptionColumns | TextSubscriptionColumns]:
    """Push notifications for one app.

    `auth` is the app's login object: the VAPID key is kept in the same .env, under a name
    made from the same app name. `model` is the app's `push_subscriptions` class. `title` is
    the app's name as it appears on the home screen. `contact` returns the VAPID contact
    claim, an https: or mailto: URL that tells a push service who is sending; it is called
    when a message is sent, so it can read the app's settings. `test_url` is where tapping
    the test message opens.

    Every method that reads the key takes an optional `path`, so a test can point one call
    at a scratch file; the app's tests already point `auth.env_file` at one.
    """

    InvalidSubscription = InvalidSubscription
    device_label = staticmethod(device_label)

    def __init__(
        self,
        auth: Auth,
        model: type[S],
        *,
        title: str,
        contact: Callable[[], str],
        test_url: str = "/settings",
    ) -> None:
        self.auth = auth
        self.model = model
        self.title = title
        self.contact = contact
        self.test_url = test_url
        self.PRIVATE_KEY = f"{auth.name.upper()}_VAPID_PRIVATE_KEY"

    # --- keys ---------------------------------------------------------------------------------

    def vapid_private_key(self, path: Path | None = None) -> str:
        """The base64url private key from .env. It is generated and stored the first time it
        is asked for and there is none; a key that exists is only ever read."""
        env = path if path is not None else self.auth.env_file  # the same .env as the login
        key = read_value(self.PRIVATE_KEY, env)
        if key is None:
            vapid = Vapid02()
            vapid.generate_keys()
            key = _raw_private(vapid)
            write_values({self.PRIVATE_KEY: key}, env)
            log.info("Generated a VAPID key pair")
        return key

    def vapid_public_key(self, path: Path | None = None) -> str:
        """The matching public key in the form `PushManager.subscribe` wants (base64url of
        the uncompressed P-256 point)."""
        vapid = Vapid02.from_raw(self.vapid_private_key(path).encode("ascii"))
        point = vapid.public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        return str(b64urlencode(point))

    # --- devices ------------------------------------------------------------------------------

    def subscriptions(self, session: Session) -> list[S]:
        """Every subscribed device, oldest first."""
        model = self.model
        return list(session.scalars(select(model).order_by(model.created_at, model.endpoint)))

    def find_subscription(self, session: Session, endpoint: str) -> S | None:
        model = self.model
        return session.scalar(select(model).where(model.endpoint == endpoint))

    def subscribe(self, session: Session, endpoint: str, p256dh: str, auth: str, label: str) -> S:
        """Keep a device's subscription; the same endpoint again updates the keys and label.
        The endpoint is where the server will POST, so it must be an https URL."""
        endpoint = _checked(endpoint, "endpoint")
        if not endpoint.startswith("https://"):
            raise InvalidSubscription("The subscription's endpoint must be an https address")
        p256dh, auth = _checked(p256dh, "p256dh key"), _checked(auth, "auth key")
        row = self.find_subscription(session, endpoint)
        if row is None:
            row = self.model(endpoint=endpoint, p256dh=p256dh, auth=auth, label=label)
            session.add(row)
        else:
            row.p256dh, row.auth, row.label = p256dh, auth, label
        session.commit()
        return row

    def unsubscribe(self, session: Session, endpoint: str) -> bool:
        """Forget a device. An unknown endpoint is not an error: the device is off either
        way."""
        row = self.find_subscription(session, endpoint)
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True

    def view(self, session: Session) -> dict[str, Any]:
        """What a page needs in order to offer notifications, as plain data for a JSON
        answer: the public key, and the devices."""
        return {
            "public_key": self.vapid_public_key(),
            "devices": [
                {
                    "endpoint": row.endpoint,
                    "label": row.label,
                    "created_at": row.created_at,
                    "last_sent_at": row.last_sent_at,
                }
                for row in self.subscriptions(session)
            ],
        }

    # --- sending ------------------------------------------------------------------------------

    def deliver(self, subscription: S, payload: str, private_key: str, ttl: int) -> None:
        """One POST to the push service: the only code that reaches the network, and the one
        thing an app's tests replace (`monkeypatch.setattr(push, "deliver", fake)`). Raises
        WebPushException on any non-success response and OSError when the network is down."""
        webpush(
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
            },
            data=payload,
            vapid_private_key=private_key,
            vapid_claims={"sub": self.contact()},
            ttl=ttl,
            timeout=TIMEOUT_SECONDS,
        )

    def send(
        self,
        session: Session,
        message: Message,
        *,
        now: datetime | None = None,
        path: Path | None = None,
    ) -> SendResult:
        """Send `message` to every subscribed device and say what happened; it never raises
        because a device could not be reached. A subscription the push service reports gone
        is deleted; any other failure is counted and logged and the subscription kept for
        next time. `now` is the server's local time, recorded as when each device was last
        sent to.

        Store whatever stops the message going twice (the record of an event, the marker
        that a reminder went) before calling this, so a push service that is down never
        causes a message a minute."""
        result = SendResult()
        rows = self.subscriptions(session)
        if not rows:
            return result
        private_key = self.vapid_private_key(path)
        payload = message.payload()
        for row in rows:
            try:
                self.deliver(row, payload, private_key, message.ttl)
            except WebPushException as exc:
                if exc.status_code in GONE_STATUSES:
                    session.delete(row)
                    result.removed += 1
                else:
                    result.failed += 1
                    result.errors.append(f"{row.label}: {exc.status_code or exc.message}")
                continue
            except OSError as exc:
                result.failed += 1
                result.errors.append(f"{row.label}: {exc}")
                continue
            except Exception as exc:  # a subscription whose keys cannot be encrypted to
                result.failed += 1
                result.errors.append(f"{row.label}: {type(exc).__name__}: {exc}")
                continue
            row.mark_sent(now)
            result.sent += 1
        session.commit()
        if result.failed:
            log.warning("Push failures: %s", "; ".join(result.errors))
        return result

    def test_message(self) -> Message:
        return Message(
            title=self.title,
            body="Notifications are working on this device.",
            url=self.test_url,
            tag="test",
            ttl=TEST_TTL,
        )


# --- reminders --------------------------------------------------------------------------------
#
# A reminder goes at a time of day the owner chose, at most once for each time, and is skipped
# rather than sent late when the app could not send it near its time. An app that checks every
# minute can use these two; where the times and the marker are kept is the app's business.


def in_window(now: datetime, at: time, window: timedelta = REMINDER_WINDOW) -> bool:
    """True from `at` today until `window` later. `now` is the server's local time. A window
    that would run past midnight ends at midnight."""
    start = datetime.combine(now.date(), at)
    return start <= now < start + window


def due_slot(
    now: datetime,
    times: Iterable[time],
    last: str | None,
    window: timedelta = REMINDER_WINDOW,
) -> str | None:
    """The reminder to handle at `now`, as "YYYY-MM-DDTHH:MM", or None when there is none:
    the latest of today's `times` that has passed by less than `window`, unless it or a later
    one has been handled already. `last` is the slot handled most recently, which the app
    stores before it sends, so each time is handled at most once and a time that came and
    went while the app was down is skipped."""
    passed = sorted(at for at in times if in_window(now, at, window))
    if not passed:
        return None
    slot = f"{now.date().isoformat()}T{passed[-1].strftime('%H:%M')}"
    return slot if slot > (last or "") else None
