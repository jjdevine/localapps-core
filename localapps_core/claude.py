"""The Claude API: an app's key, the client made with it, and the words for what goes wrong.

An app that asks Claude for something does it with Anthropic's own SDK, and what it asks, when,
and what it does with the answer are the app's business, in a module of its own. What is the
same in every app is here:

- **The key.** One for each app, kept in the app's .env like its other secrets, as
  `<NAME>_ANTHROPIC_API_KEY`: never in the database, a backup or git. It is typed into the
  app's Settings page, which shows only its last four characters. A key per app means each
  app's spending shows on its own in the Anthropic console, and one key can be withdrawn
  without stopping the others.
- **The client.** `Claude.client()` makes the SDK's client with that key, and says so in plain
  words when there is none.
- **The models on offer**, for the app's Settings page to choose from (`MODELS`), so a new
  model reaches every app from one place.
- **What went wrong, in words for the page** (`error_text`): a key that is refused, a model the
  key cannot use, a rate or spending limit, an error from the API, no connection.
- **Tests that cannot reach the API** (`forbid_requests`).

Nothing here imports FastAPI. It needs `anthropic`, which the package's `claude` extra
installs.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import anthropic

from localapps_core.auth import Auth
from localapps_core.envfile import read_value, write_values

# The models an app offers on its Settings page, the most capable first: the id the API takes
# and a name to show. The app chooses which is its default.
MODELS: tuple[tuple[str, str], ...] = (
    ("claude-opus-5-5", "Claude Opus 5.5 (most capable)"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5 (mid-tier)"),
    ("claude-haiku-5-5", "Claude Haiku 5.5 (fastest, cheapest)"),
)

# Sent as a beta with `fallbacks="default"`, it has a request the model declines run on that
# model's stand-in within the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

NO_KEY = "No Claude API key is set. Add one on the Settings page."


class ClaudeError(Exception):
    """Claude could not be asked, or its answer could not be used. The message is written for
    the page."""


def masked(key: str | None) -> str | None:
    """A key as the Settings page shows it: only its last four characters."""
    if not key:
        return None
    return f"{'•' * 8}{key[-4:]}"


def valid_model(chosen: str | None, default: str) -> str:
    """The model to use: the one chosen if it is on offer, else the app's default."""
    return chosen if chosen in {model for model, _ in MODELS} else default


def error_text(exc: Exception) -> str:
    """What went wrong with a request, in words for the page. It takes whatever the SDK
    raised; a `ClaudeError` already has its words."""
    if isinstance(exc, ClaudeError):
        return str(exc)
    if isinstance(exc, anthropic.AuthenticationError):
        return "The Claude API key was refused. Check it on the Settings page."
    if isinstance(exc, anthropic.PermissionDeniedError | anthropic.NotFoundError):
        return f"The Claude API key cannot use the model that was asked for: {exc.message}"
    if isinstance(exc, anthropic.RateLimitError):
        return (
            "The Claude API is turning requests away for now: a rate limit or the account's "
            "spending limit has been reached. Try again later."
        )
    if isinstance(exc, anthropic.BadRequestError):
        return f"The Claude API would not take the request: {exc.message}"
    if isinstance(exc, anthropic.APIStatusError):
        return f"The Claude API answered with an error ({exc.status_code}): {exc.message}"
    if isinstance(exc, anthropic.APIConnectionError):
        return "The Claude API could not be reached. Check the connection and try again."
    return f"The request to the Claude API failed: {exc}"


def _refuse(*args: object, **kwargs: object) -> None:
    raise AssertionError("A test reached for the Claude API; give it a stand-in instead")


def forbid_requests(monkeypatch: Any) -> None:
    """For an app's tests: make the SDK's client impossible to build, whatever key the machine
    holds, so no test can reach the API. `monkeypatch` is pytest's. A test that needs an
    answer stands in for the app's own functions, or for `Claude.client`."""
    monkeypatch.setattr(anthropic, "Anthropic", _refuse)


class Claude:
    """The Claude API for one app.

    `auth` is the app's login object: the key is kept in the same .env, under a name made from
    the same app name. `legacy_key_names` are names the key was kept under before, read when
    the app's own name has nothing and removed the next time a key is saved. `timeout`, in
    seconds, and `max_retries` are passed to the SDK's client when given.

    Every method that reads or writes the key takes an optional `path`, so a test can point one
    call at a scratch file; the app's tests already point `auth.env_file` at one.
    """

    ClaudeError = ClaudeError
    masked = staticmethod(masked)
    error_text = staticmethod(error_text)

    def __init__(
        self,
        auth: Auth,
        *,
        legacy_key_names: Sequence[str] = (),
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        self.auth = auth
        self.KEY_NAME = f"{auth.name.upper()}_ANTHROPIC_API_KEY"
        self.legacy_key_names = tuple(legacy_key_names)
        self.timeout = timeout
        self.max_retries = max_retries

    def _path(self, path: Path | None) -> Path:
        return path if path is not None else self.auth.env_file  # the same .env as the login

    def api_key(self, path: Path | None = None) -> str | None:
        """The app's key, from .env or else the process environment, or None."""
        env = self._path(path)
        for name in (self.KEY_NAME, *self.legacy_key_names):
            key = read_value(name, env)
            if key is not None:
                return key
        return None

    def key_is_set(self, path: Path | None = None) -> bool:
        return self.api_key(path) is not None

    def key_hint(self, path: Path | None = None) -> str | None:
        """The key as the Settings page shows it, or None when there is none."""
        return masked(self.api_key(path))

    def set_api_key(self, key: str | None, path: Path | None = None) -> None:
        """Keep the key under the app's own name; None or an empty one removes it. A line
        under an older name goes either way."""
        values: dict[str, str | None] = dict.fromkeys(self.legacy_key_names)
        values[self.KEY_NAME] = key
        write_values(values, self._path(path))

    def save_from_form(self, typed: str, remove: bool, path: Path | None = None) -> None:
        """What the Settings form sends: a new key to keep, a tick to remove the one there, or
        neither, which leaves it as it is. The page never shows the key, so an empty box
        means no change."""
        if remove:
            self.set_api_key(None, path)
        elif typed.strip():
            self.set_api_key(typed, path)

    def client(self, path: Path | None = None) -> anthropic.Anthropic:
        """The SDK's client, with the app's key. A `ClaudeError` when there is no key."""
        key = self.api_key(path)
        if key is None:
            raise ClaudeError(NO_KEY)
        options: dict[str, Any] = {}
        if self.timeout is not None:
            options["timeout"] = self.timeout
        if self.max_retries is not None:
            options["max_retries"] = self.max_retries
        return anthropic.Anthropic(api_key=key, **options)
