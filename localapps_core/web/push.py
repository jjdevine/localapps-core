"""The routes for push notifications: the two scripts the package gives a browser, the
service worker, and the calls that turn a device on or off and send the test message.

An app with server-rendered pages includes `push_router`:

    app.include_router(push_router(push, database.get_db, service_worker=STATIC_DIR / "sw.js"))
    app.mount("/static", ...)   # after it: the scripts are served under /static/push/

A single-page app includes `push_api_router` instead, which has the same calls as JSON under
`/api/notifications`.

Either must be included before the app's own `/static` mount, which would otherwise answer
for `/static/push/` and find nothing there. Everything except the scripts and `/sw.js` is
behind the app's login.
"""

# No `from __future__ import annotations` in this module: the routes are made inside functions
# and name the app's session dependency, which FastAPI can only see in an annotation that has
# already been evaluated.

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from localapps_core.push import (
    InvalidSubscription,
    Push,
    SubscriptionColumns,
    TextSubscriptionColumns,
    device_label,
)

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "static" / "push"
PAGE_SCRIPT = "/static/push/page.js"
WORKER_SCRIPT = "/static/push/worker.js"

type GetDb = Callable[[], Iterator[Session]]


class SubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class SubscriptionIn(BaseModel):
    """`PushSubscription.toJSON()` from the browser."""

    endpoint: str
    keys: SubscriptionKeys


class EndpointIn(BaseModel):
    endpoint: str


def _script(path: Path) -> FileResponse:
    """Never cached, so a new version is picked up on the next visit."""
    return FileResponse(
        path, media_type="application/javascript", headers={"Cache-Control": "no-cache"}
    )


def push_scripts_router(service_worker: Path | None = None) -> APIRouter:
    """The page script and the service-worker handlers, under `/static/` so they need no
    login; and, given the app's own service worker file, `/sw.js`, served from the root so
    its scope is the whole site. `push_router` and `push_api_router` include these."""
    router = APIRouter(include_in_schema=False)

    @router.get(PAGE_SCRIPT)
    def page_script() -> FileResponse:
        return _script(SCRIPTS_DIR / "page.js")

    @router.get(WORKER_SCRIPT)
    def worker_script() -> FileResponse:
        return _script(SCRIPTS_DIR / "worker.js")

    if service_worker is not None:
        worker_file = service_worker

        @router.get("/sw.js")
        def service_worker_script() -> FileResponse:
            return _script(worker_file)

    return router


def push_router[S: SubscriptionColumns | TextSubscriptionColumns](
    push: Push[S],
    get_db: GetDb,
    *,
    service_worker: Path | None = None,
    settings_url: str = "/settings",
) -> APIRouter:
    """The routes for an app with server-rendered pages. The page script posts JSON to
    subscribe and unsubscribe; the device list and the test button are plain forms, which
    come back to `settings_url` with what happened in the query string: `device=forgotten`,
    or `test=<sent>&failed=<n>&gone=<n>`."""
    router = APIRouter()
    router.include_router(push_scripts_router(service_worker))
    Db = Annotated[Session, Depends(get_db)]

    @router.post("/notifications/subscribe", status_code=204)
    def subscribe(body: SubscriptionIn, request: Request, db: Db) -> Response:
        label = device_label(request.headers.get("user-agent"))
        try:
            push.subscribe(db, body.endpoint, body.keys.p256dh, body.keys.auth, label)
        except InvalidSubscription as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return Response(status_code=204)

    @router.post("/notifications/unsubscribe", status_code=204)
    def unsubscribe(body: EndpointIn, db: Db) -> Response:
        push.unsubscribe(db, body.endpoint)
        return Response(status_code=204)

    @router.post("/notifications/forget")
    def forget(db: Db, endpoint: Annotated[str, Form()]) -> Response:
        """Remove a device from the list. Its browser still holds a subscription, which the
        page script notices (the endpoint is no longer in the list) and offers to turn on
        again."""
        push.unsubscribe(db, endpoint)
        return RedirectResponse(f"{settings_url}?device=forgotten", status_code=303)

    @router.post("/notifications/test")
    def send_test(db: Db) -> Response:
        result = push.send(db, push.test_message())
        query = f"test={result.sent}&failed={result.failed}&gone={result.removed}"
        return RedirectResponse(f"{settings_url}?{query}", status_code=303)

    return router


def push_api_router[S: SubscriptionColumns | TextSubscriptionColumns](
    push: Push[S],
    get_db: GetDb,
    *,
    view: Callable[[Session], dict[str, Any]] | None = None,
    invalid: Callable[[InvalidSubscription], Exception] | None = None,
    service_worker: Path | None = None,
    prefix: str = "/api/notifications",
) -> APIRouter:
    """The same calls for a single-page app, as JSON under `prefix`. Every route answers
    `{"notifications": <view>}`, and the test adds `{"result": {sent, failed, removed}}`.
    The view is `push.view` (the public key and the devices) unless the app gives its own
    `view`, to add what else its dialog shows. Unsubscribe is also how a device is removed
    from the list on another device.

    A subscription that cannot be kept is answered 422 with FastAPI's usual `detail`, unless
    `invalid` turns it into an exception the app already has a handler for."""
    router = APIRouter()
    router.include_router(push_scripts_router(service_worker))
    Db = Annotated[Session, Depends(get_db)]
    build_view = view if view is not None else push.view

    def answer(db: Session) -> dict[str, Any]:
        return {"notifications": build_view(db)}

    @router.get(prefix)
    def get_notifications(db: Db) -> dict[str, Any]:
        return answer(db)

    @router.post(f"{prefix}/subscribe")
    def subscribe(body: SubscriptionIn, request: Request, db: Db) -> dict[str, Any]:
        label = device_label(request.headers.get("user-agent"))
        try:
            push.subscribe(db, body.endpoint, body.keys.p256dh, body.keys.auth, label)
        except InvalidSubscription as exc:
            if invalid is not None:
                raise invalid(exc) from None
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return answer(db)

    @router.post(f"{prefix}/unsubscribe")
    def unsubscribe(body: EndpointIn, db: Db) -> dict[str, Any]:
        push.unsubscribe(db, body.endpoint)
        return answer(db)

    @router.post(f"{prefix}/test")
    def send_test(db: Db) -> dict[str, Any]:
        result = push.send(db, push.test_message())
        return {
            **answer(db),
            "result": {"sent": result.sent, "failed": result.failed, "removed": result.removed},
        }

    return router
