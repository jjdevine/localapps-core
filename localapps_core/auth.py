"""Login: one password, a long-lived signed cookie per device.

A salted scrypt hash of the password and a cookie-signing secret live in the app's .env. A
session token is an expiry time plus an HMAC of it under that secret, so there is no session
table: a token is valid if its signature matches and it has not expired. Setting a new
password writes a fresh secret, which logs every device out at once. With no password set the
app is open, which suits a single PC bound to localhost and nothing else.

Everything is derived from the app's one name: an app called `notes` keeps
`NOTES_PASSWORD_HASH` and `NOTES_SESSION_SECRET` in .env and sets a cookie called
`notes_session`. The cookie has its own name because several apps may be reached on one host
name on different ports, and browsers do not separate cookies by port.

Nothing here imports FastAPI; `localapps_core.web.auth` turns these results into cookies and
redirects.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets as rand
from datetime import UTC, datetime, timedelta
from pathlib import Path

from localapps_core.envfile import read_value, write_values

SESSION_DAYS = 90
REFRESH_AFTER = timedelta(days=1)
MIN_LENGTH = 8

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_BACKSLASH = chr(92)


class PasswordError(ValueError):
    """A password that cannot be set, with a message fit for the screen."""


def hash_password(password: str) -> str:
    salt = rand.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
        )
    except ValueError:
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def safe_next(target: str | None) -> str:
    """Where to go after login: a path on this site only, never another host."""
    if (
        target
        and target.startswith("/")
        and not target.startswith("//")
        and _BACKSLASH not in target
    ):
        return target
    return "/"


def _signature(expires: int, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), str(expires).encode("ascii"), "sha256").hexdigest()


def _parse(token: str | None) -> tuple[int, str] | None:
    if not token or "." not in token:
        return None
    expires_text, signature = token.split(".", 1)
    if not expires_text.isdigit():
        return None
    return int(expires_text), signature


class Auth:
    """The login for one app. `name` is the app's short lowercase name; `env_file` is its .env.

    Every method that reads or writes the file takes an optional `path`, so a test can point
    one call at a scratch file; assigning `env_file` redirects them all.
    """

    PasswordError = PasswordError
    hash_password = staticmethod(hash_password)
    verify_password = staticmethod(verify_password)
    safe_next = staticmethod(safe_next)

    def __init__(
        self,
        name: str,
        env_file: Path,
        *,
        session_days: int = SESSION_DAYS,
        min_length: int = MIN_LENGTH,
    ) -> None:
        if not name.isidentifier() or name != name.lower():
            raise ValueError(f"an app name is lowercase letters and digits, not {name!r}")
        self.name = name
        self.env_file = env_file
        self.PASSWORD_KEY = f"{name.upper()}_PASSWORD_HASH"
        self.SECRET_KEY = f"{name.upper()}_SESSION_SECRET"
        self.COOKIE_NAME = f"{name}_session"
        self.SESSION_DAYS = session_days
        self.REFRESH_AFTER = REFRESH_AFTER
        self.MIN_LENGTH = min_length

    def _path(self, path: Path | None) -> Path:
        return path if path is not None else self.env_file

    def password_is_set(self, path: Path | None = None) -> bool:
        return read_value(self.PASSWORD_KEY, self._path(path)) is not None

    def check_password(self, password: str, path: Path | None = None) -> bool:
        """True when `password` matches the stored hash."""
        return verify_password(password, read_value(self.PASSWORD_KEY, self._path(path)))

    def set_password(self, password: str, path: Path | None = None) -> None:
        """Store a new password and rotate the session secret, logging every device out."""
        if len(password) < self.MIN_LENGTH:
            raise PasswordError(f"The password must be at least {self.MIN_LENGTH} characters")
        if password != password.strip():
            raise PasswordError("The password cannot start or end with a space")
        write_values(
            {self.PASSWORD_KEY: hash_password(password), self.SECRET_KEY: rand.token_hex(32)},
            self._path(path),
        )

    def clear_password(self, path: Path | None = None) -> None:
        """Remove the password; the app is open again."""
        write_values({self.PASSWORD_KEY: None, self.SECRET_KEY: None}, self._path(path))

    def issue_token(self, now: datetime | None = None, path: Path | None = None) -> str | None:
        """A new session token, or None when there is no secret to sign with."""
        secret = read_value(self.SECRET_KEY, self._path(path))
        if secret is None:
            return None
        now = now or datetime.now(UTC)
        expires = int((now + timedelta(days=self.SESSION_DAYS)).timestamp())
        return f"{expires}.{_signature(expires, secret)}"

    def token_is_valid(
        self, token: str | None, now: datetime | None = None, path: Path | None = None
    ) -> bool:
        parsed = _parse(token)
        secret = read_value(self.SECRET_KEY, self._path(path))
        if parsed is None or secret is None:
            return False
        expires, signature = parsed
        now = now or datetime.now(UTC)
        if expires <= int(now.timestamp()):
            return False
        return hmac.compare_digest(signature, _signature(expires, secret))

    def token_needs_refresh(self, token: str, now: datetime | None = None) -> bool:
        """True once a valid token is more than a day old, so an active device never expires."""
        parsed = _parse(token)
        if parsed is None:
            return False
        now = now or datetime.now(UTC)
        issued = datetime.fromtimestamp(parsed[0], UTC) - timedelta(days=self.SESSION_DAYS)
        return now - issued >= self.REFRESH_AFTER
