"""Login and logout routes, and the middleware that turns every other request away until
the device has logged in. With no password set the middleware passes everything through.

An app wires it in when it builds its FastAPI application:

    login = Login(auth, templates)
    app.middleware("http")(login.require_login)
    app.include_router(login.router)

The login page itself is the app's own template (`auth/login.html` by default), because it
carries the app's name, icon and stylesheet. It is rendered with `next`, `error` and
`session_days`, and posts `password` and `next` to `/login`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Annotated

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from localapps_core.auth import Auth, safe_next

# Reachable without the login: the login page, the health check that deploys and monitoring
# poll, static files, and the service worker script, which a browser fetches on its own
# schedule and must never be answered with a redirect.
PUBLIC_PATHS: frozenset[str] = frozenset({"/login", "/health", "/sw.js"})
PUBLIC_PREFIXES: tuple[str, ...] = ("/static/",)


class Login:
    def __init__(
        self,
        auth: Auth,
        templates: Jinja2Templates,
        *,
        template: str = "auth/login.html",
        public_paths: Iterable[str] = PUBLIC_PATHS,
        public_prefixes: Iterable[str] = PUBLIC_PREFIXES,
    ) -> None:
        self.auth = auth
        self.templates = templates
        self.template = template
        self.public_paths = frozenset(public_paths)
        self.public_prefixes = tuple(public_prefixes)
        self.router = APIRouter()
        self._add_routes()

    def set_session_cookie(self, request: Request, response: Response, token: str) -> None:
        """Attach a session cookie, marked Secure when the request itself came over HTTPS.
        Behind a proxy on localhost uvicorn takes the scheme from `X-Forwarded-Proto`, so an
        HTTPS entry point gets a cookie the browser only sends back over HTTPS, while a
        plain-HTTP entry point on the LAN keeps a cookie it can actually send."""
        response.set_cookie(
            self.auth.COOKIE_NAME,
            token,
            max_age=self.auth.SESSION_DAYS * 24 * 3600,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="lax",
            path="/",
        )

    def is_public(self, path: str) -> bool:
        return path in self.public_paths or path.startswith(self.public_prefixes)

    async def require_login(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """HTTP middleware. Sets `request.state.logged_in` for templates to read."""
        auth = self.auth
        if not auth.password_is_set():
            request.state.logged_in = False
            return await call_next(request)
        token = request.cookies.get(auth.COOKIE_NAME)
        valid = auth.token_is_valid(token)
        request.state.logged_in = valid
        if valid or self.is_public(request.url.path):
            response = await call_next(request)
            if valid and token and auth.token_needs_refresh(token):
                fresh = auth.issue_token()
                if fresh:
                    self.set_session_cookie(request, response, fresh)
            return response
        # An HTMX request swaps whatever comes back into the page, so a redirect would put
        # the login form inside it; this header makes HTMX navigate the whole page instead.
        if request.headers.get("HX-Request") == "true":
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        target = "/login"
        if request.method == "GET" and request.url.path != "/":
            target += f"?next={request.url.path}"
        return RedirectResponse(target, status_code=303)

    def _page(self, request: Request, next: str, error: str | None, status: int) -> Response:
        return self.templates.TemplateResponse(
            request,
            self.template,
            {"next": safe_next(next), "error": error, "session_days": self.auth.SESSION_DAYS},
            status_code=status,
        )

    def _add_routes(self) -> None:
        auth = self.auth

        @self.router.get("/login", response_class=HTMLResponse)
        def login_page(request: Request, next: str = "/") -> Response:
            if not auth.password_is_set() or getattr(request.state, "logged_in", False):
                return RedirectResponse(safe_next(next), status_code=303)
            return self._page(request, next, None, 200)

        @self.router.post("/login", response_class=HTMLResponse)
        def login(
            request: Request,
            password: Annotated[str, Form()] = "",
            next: Annotated[str, Form()] = "/",
        ) -> Response:
            if not auth.check_password(password):
                return self._page(request, next, "That password is not right", 401)
            token = auth.issue_token()
            response = RedirectResponse(safe_next(next), status_code=303)
            if token:
                self.set_session_cookie(request, response, token)
            return response

        @self.router.post("/logout")
        def logout() -> Response:
            response = RedirectResponse("/login", status_code=303)
            response.delete_cookie(auth.COOKIE_NAME, path="/")
            return response
