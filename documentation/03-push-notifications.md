# 03 · Push notifications

`localapps_core.push` sends Web Push notifications to the browsers that asked for them, and `localapps_core.web.push` has the routes. The package delivers; it has no idea what an app is for. What to notify about, when, and in what words stays in the app, in one module of its own.

The first half of this page is the recipe for adding notifications to an app. The second half says how it works. The examples use an app called `notes`.

## The recipe

### 1. Install the extra

Sending needs `pywebpush`, which only an app that notifies should have to install, so it is an extra:

```toml
dependencies = [
    "localapps-core[push] @ git+https://github.com/jjdevine/localapps-core.git@v0.2.2",
]
```

### 2. The table: `app/models/push_subscription.py` and a migration

The package declares the columns; the app puts them on its own metadata and creates the table in its own migration, like any other table. There are two forms.

For an app whose declarative base gives every table a numeric key and a `created_at` date-time:

```python
from localapps_core.push import TABLE_NAME, SubscriptionColumns

from app.models.base import Base


class PushSubscription(SubscriptionColumns, Base):
    __tablename__ = TABLE_NAME
```

```python
op.create_table(
    "push_subscriptions",
    sa.Column("endpoint", sa.Text(), nullable=False),
    sa.Column("p256dh", sa.String(length=200), nullable=False),
    sa.Column("auth", sa.String(length=50), nullable=False),
    sa.Column("label", sa.String(length=100), nullable=False),
    sa.Column("last_sent_at", sa.DateTime(), nullable=True),
    sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
    sa.Column("created_at", sa.DateTime(), nullable=False),
    ...  # whatever else the base adds
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint("endpoint"),
)
```

For an app that keys its tables on a natural key and stores times as UTC ISO-8601 text, use `TextSubscriptionColumns` in the same way. It needs nothing from the base: `endpoint` is the primary key and `created_at` is declared with the rest.

```python
op.create_table(
    "push_subscriptions",
    sa.Column("endpoint", sa.Text(), nullable=False),
    sa.Column("p256dh", sa.Text(), nullable=False),
    sa.Column("auth", sa.Text(), nullable=False),
    sa.Column("label", sa.Text(), nullable=False, server_default=""),
    sa.Column("created_at", sa.Text(), nullable=False),
    sa.Column("last_sent_at", sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint("endpoint"),
)
```

An app that already has the table keeps it and its migration as they are, and picks the form that matches.

### 3. `app/services/push.py`

```python
from localapps_core.push import Push

from app.config import get_settings
from app.models.push_subscription import PushSubscription
from app.services.auth import auth

push = Push(
    auth,
    PushSubscription,
    title="Notes",
    contact=lambda: get_settings().vapid_contact,
)
```

`auth` is the login object from [01 Wiring an app](01-wiring-an-app.md): the key that signs messages is kept in the same `.env`, as `NOTES_VAPID_PRIVATE_KEY`. `title` is the app's name as it appears on the home screen. `contact` returns the VAPID contact claim, an `https:` or `mailto:` URL that tells a push service who is sending; the app's own site is the usual answer (`vapid_contact: str = "https://notes.example"` in `Settings`). It is called when a message is sent. `test_url` (default `/settings`) is where tapping the test message opens.

### 4. The module that decides

One module in `app/services/` decides what is worth a notification and what it says, and calls `push.send`:

```python
from localapps_core.push import Message

from app.services.push import push

EVENT_TTL = 12 * 60 * 60  # still worth having the next morning, not the next week


def record(session, title, detail):
    ...  # store the event first
    session.commit()
    result = push.send(
        session, Message(title="Notes", body=detail, url="/", tag="event", ttl=EVENT_TTL)
    )
```

A `Message` is `title`, `body`, `url` (a path in the app, opened when the notification is tapped; `/` unless given), `tag` and `ttl`. Messages with the same `tag` replace each other on the phone rather than piling up. `ttl` is how many seconds the push service holds the message for a phone that is off; it is the app's choice, so it has no default. A body appears on the lock screen, where anyone holding the phone can read it.

`send` returns a `SendResult` (`sent`, `failed`, `removed`, `errors`) and never raises because a device could not be reached. Whatever stops a message going twice (the record of an event, the marker that a reminder went) is stored before `send` is called, so a push service that is down never causes a message a minute.

### 5. `app/main.py`

```python
from localapps_core.web.push import push_router

from app.db import database
from app.services.push import push


def create_app() -> FastAPI:
    app = FastAPI(...)
    app.middleware("http")(login.require_login)
    app.include_router(push_router(push, database.get_db, service_worker=STATIC_DIR / "sw.js"))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    ...
```

**The push routes go in before the `/static` mount.** The package serves its two scripts under `/static/push/`, and the app's own mount would otherwise answer for that path and find nothing there. `settings_url` (default `/settings`) is the page the two form routes come back to.

### 6. `app/static/sw.js`

The app's own service worker loads the package's handlers and says what its notifications look like:

```js
"use strict";
importScripts("/static/push/worker.js");

self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (event) { event.waitUntil(self.clients.claim()); });

localappsPush({ title: "Notes", tag: "notes", icon: "/static/icon-192.png", badge: "/static/badge-96.png" });
```

`title`, `tag` and `url` are what a message is shown with when it does not carry its own. `icon` is the colour picture in the expanded notification. `badge` is the status-bar glyph, which Android draws as a silhouette from the image's transparency, so it is a shape on a transparent background, not the square app icon. `navigate: false` leaves an open copy of the app where it is when a notification is tapped; without it the open copy is taken to the message's `url`. A service worker that also caches keeps doing so: the call adds the `push` and `notificationclick` handlers and nothing else. `localappsPush` must be called while the script is first run, not later.

### 7. The Notifications section of Settings

The route that renders the page passes three things:

```python
devices = push.subscriptions(db)
context = {
    "vapid_public_key": push.vapid_public_key(),
    "devices": devices,
    "push_endpoints": [d.endpoint for d in devices],
}
```

The section is in the app's own template and style. The package's page script runs it, and needs only the three hooks: `id="push"` with the two `data-` attributes, `data-push-status` and `data-push-toggle`.

```html
<section id="push" data-public-key="{{ vapid_public_key }}" data-endpoints='{{ push_endpoints | tojson }}'>
  <h2>Notifications</h2>
  <p data-push-status>Notifications need JavaScript, which is off or still loading.</p>
  <p><button type="button" data-push-toggle hidden>Turn on on this device</button></p>
  {% for d in devices %}
    {{ d.label }}, added {{ d.created_at }}, last sent {{ d.last_sent_at or 'never' }}
    <form method="post" action="/notifications/forget">
      <input type="hidden" name="endpoint" value="{{ d.endpoint }}">
      <button type="submit">Remove</button>
    </form>
  {% endfor %}
  <form method="post" action="/notifications/test">
    <button type="submit" {{ 'disabled' if not devices }}>Send a test notification</button>
  </form>
</section>
...
<script src="/static/push/page.js"></script>
```

The script writes the status line (this device gets notifications, or does not yet, or why it cannot: an unsupported browser, a page that is not HTTPS, notifications blocked) and shows the button when it can work. After a change the page is reloaded with something in the query string for the template to turn into a message:

| Query | Meaning |
| --- | --- |
| `device=added` | notifications were turned on for this device |
| `device=removed` | they were turned off for this device |
| `device=forgotten` | a device was removed from the list |
| `test=<n>&failed=<n>&gone=<n>` | a test went to `test` devices, `failed` could not be reached, `gone` stale subscriptions were removed |

Browsers allow push only on a secure page, so notifications are turned on at the app's HTTPS address; at a plain-HTTP address the section says so instead of offering a button that cannot work.

### 8. The app's rules and documentation

Delivery is an outbound request: the app POSTs to the push-service URLs that subscribed browsers handed over. An app that otherwise makes none should say so wherever it lists its exceptions. Its documentation should say what it notifies about, its tags and times to live, and that the key in `.env` must never be changed.

### 9. The app's tests

Tests replace `push.deliver`, the one method that reaches a push service, so no test touches the network:

```python
from types import SimpleNamespace

from pywebpush import WebPushException

from app.services.push import push


@pytest.fixture
def deliveries(monkeypatch):
    """Record every would-be push; an endpoint containing "gone" answers 410."""
    sent = []

    def fake(subscription, payload, private_key, ttl):
        if "gone" in subscription.endpoint:
            raise WebPushException("gone", SimpleNamespace(status_code=410, text=""))
        sent.append((subscription.endpoint, payload, ttl))

    monkeypatch.setattr(push, "deliver", fake)
    return sent
```

The key is written to `auth.env_file`, which the app's tests already point at a scratch file ([01 Wiring an app](01-wiring-an-app.md)), so no test writes a key into the real `.env`.

### A single-page app

An app whose front end is one page over a JSON API includes `push_api_router` instead of `push_router`:

```python
app.include_router(push_api_router(push, database.get_db, view=view, invalid=invalid))
```

It has `GET /api/notifications`, `POST /api/notifications/subscribe` (the browser's `PushSubscription.toJSON()`), `POST /api/notifications/unsubscribe` (`{endpoint}`, which is also how a device is removed from the list on another device) and `POST /api/notifications/test`. Each answers `{"notifications": <view>}`, and the test adds `"result": {"sent", "failed", "removed"}`. The view is `push.view(session)`, the public key and the devices, unless the app passes its own `view` function to add what else its dialog shows. A subscription that cannot be kept is answered 422 with FastAPI's usual `detail`; `invalid` turns it into an exception the app already has a handler for. `prefix` changes the path, and `service_worker` adds `/sw.js` as in `push_router`; leave it out when the app serves its own.

The page draws its own controls and uses the functions `page.js` defines on `window.LocalappsPush`: `problem()` (why this browser cannot be turned on, in words for the screen, or `""`), `subscription()` (this browser's `PushSubscription` or null), `subscribe(publicKey)` (ask for permission and subscribe, dropping a subscription the browser already held) and `registration()`. `/sw.js` is registered by the script only when the page has not registered a service worker itself.

### Reminders

A reminder goes at a time of day the owner chose, at most once for each time, and is skipped rather than sent late when the app could not send it near its time. An app that checks once a minute can use two functions; where the times and the marker are kept is the app's business.

`in_window(now, at)` is true from `at` today until three hours later (`REMINDER_WINDOW`; pass another `window` to change it). `due_slot(now, times, last)` returns the reminder to handle now as `"YYYY-MM-DDTHH:MM"`, or `None`: the latest of today's `times` that has passed by less than the window, unless it or a later one is the `last` the app recorded. Both take the server's local time.

```python
slot = due_slot(now, times, stored_last_slot)
if slot is not None:
    store(slot)  # before sending
    push.send(session, message)
```

## How it works

### Web Push

A browser that turns notifications on asks its push service (the browser maker's own) for a subscription: a one-off URL plus two keys. The page posts that to the app, which keeps it. To notify the device the app encrypts a small JSON message to those keys (RFC 8291) and POSTs it to that URL, signed with the app's VAPID key (RFC 8292) so the push service knows the message came from the site the browser subscribed to. The push service cannot read the message; it only wakes the browser, whose service worker shows it. `pywebpush` does the encryption and the POST.

### The key

`push.vapid_private_key()` reads `NOTES_VAPID_PRIVATE_KEY` from `.env` with `read_value`. If there is none it makes a P-256 key pair, writes the private key with `write_values` and returns it; that is the only time a key is written. A key that exists, in the file or in the process environment, is only ever read. **It must never be changed or deleted**: every device's subscription is bound to it, so a new key silently cuts off every device, and each would have to be turned on again. Like every secret it is in `.env` and so in no backup; restoring a database on a new machine without the old `.env` leaves subscriptions that no longer work, which are cleared as the push services report them gone. `push.vapid_public_key()` is the matching public key in the form `PushManager.subscribe` wants.

### Devices

`push.subscribe(session, endpoint, p256dh, auth, label)` keeps a subscription; the same endpoint again updates the keys and the label, so one browser is one row. The endpoint is where the server will POST, so one that is not an `https://` address is refused, as is a missing or over-long field, with an `InvalidSubscription` whose message can be shown on screen. `push.unsubscribe(session, endpoint)` forgets a device and is not an error when the endpoint is unknown. `push.subscriptions(session)` lists them oldest first. `device_label(user_agent)` works out a short name such as "Android · Chrome" for the list. There is no choice of what each device gets: every subscribed device gets every message.

### Sending

`push.send(session, message)` POSTs to each device in turn through `push.deliver`, with a 10-second timeout on each. The device receives the JSON `{title, body, url, tag}`; the time to live goes to the push service. Every message is sent with `Urgency: high` (`URGENCY`), which asks the push service to wake the device for it. At the default urgency a phone that is idle holds a message until it next wakes, which can be after the time to live has run out, so the message is late or never shown.

| What the push service says | What happens |
| --- | --- |
| success | `last_sent_at` is set; counted in `sent` |
| 404 or 410 | the browser has let the subscription go: the row is deleted; counted in `removed` |
| any other answer, no network, or a subscription that cannot be encrypted to | the row is kept for next time; counted in `failed`, described in `errors`, and logged as a warning |

With no device subscribed nothing happens, and no key is made. `last_sent_at` is the server's local time in the date-time form of the table, and UTC text in the text form.

### The routes

| Route | What it does |
| --- | --- |
| `GET /static/push/page.js`, `GET /static/push/worker.js` | The package's two scripts. Under `/static/`, so they need no login |
| `GET /sw.js` | The app's own service worker file, from the root so its scope is the whole site. No login: a browser fetches it on its own schedule and must never be answered with a redirect |
| `POST /notifications/subscribe`, `POST /notifications/unsubscribe` | JSON from the page script; 204. A refused subscription is 422 with the reason in `detail` |
| `POST /notifications/forget`, `POST /notifications/test` | Forms; each redirects to the Settings page with the outcome in the query string |

The three scripts are served with `Cache-Control: no-cache`, so a new version of the app or the package reaches a browser on its next visit. Everything else is behind the app's login.

### In the browser

`worker.js` adds two handlers to the app's service worker. On `push` it shows the message with the app's icon and badge; a message that is not JSON is shown as text under the app's default title. On `notificationclick` it closes the notification and brings an open copy of the app to the front, taking it to the message's `url` unless the app said `navigate: false`, or opens one.

`page.js` treats a device as on only when the browser holds a subscription and the server still lists its endpoint. A device removed from the list elsewhere, or subscribed under an older key, shows as off, and turning it on drops the old subscription and makes a fresh one.
