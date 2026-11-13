"""The .env reader and writer, password hashing, session tokens and the set-password command."""

from __future__ import annotations

import getpass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from localapps_core import auth as auth_module
from localapps_core.auth import Auth, PasswordError
from localapps_core.cli import set_password_main
from localapps_core.envfile import read_value, write_values
from tests.conftest import PASSWORD, Project


def test_env_values_are_added_replaced_and_removed(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    assert read_value("NOTES_PORT", env) is None  # no file yet
    env.write_text("# a comment\nNOTES_PORT=8050\nOTHER='quoted value'\n", encoding="utf-8")
    assert read_value("NOTES_PORT", env) == "8050"
    assert read_value("OTHER", env) == "quoted value"

    write_values({"NOTES_PORT": "8060", "NEW": "x"}, env)
    assert read_value("NOTES_PORT", env) == "8060" and read_value("NEW", env) == "x"
    text = env.read_text(encoding="utf-8")
    assert "# a comment" in text and "OTHER='quoted value'" in text and text.endswith("\n")
    assert text.count("NOTES_PORT=") == 1

    write_values({"NEW": None, "NOTES_PORT": "  "}, env)  # None and blank both remove
    assert read_value("NEW", env) is None and read_value("NOTES_PORT", env) is None
    write_values({"AGAIN": "1"}, env)  # the file was left read-only and is still writable
    assert read_value("AGAIN", env) == "1"


def test_env_falls_back_to_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    monkeypatch.setenv("NOTES_FROM_ENV", "outside")
    assert read_value("NOTES_FROM_ENV", env) == "outside"
    write_values({"NOTES_FROM_ENV": "inside"}, env)
    assert read_value("NOTES_FROM_ENV", env) == "inside"  # the file wins


def test_names_come_from_the_app_name(tmp_path: Path) -> None:
    auth = Auth("notes", tmp_path / ".env")
    assert auth.PASSWORD_KEY == "NOTES_PASSWORD_HASH"
    assert auth.SECRET_KEY == "NOTES_SESSION_SECRET"
    assert auth.COOKIE_NAME == "notes_session"
    assert auth.SESSION_DAYS == 90 and auth.MIN_LENGTH == 8
    for bad in ("Notes", "my-notes", "my notes", ""):
        with pytest.raises(ValueError):
            Auth(bad, tmp_path / ".env")


def test_password_hash_round_trip() -> None:
    stored = auth_module.hash_password(PASSWORD)
    assert stored.startswith("scrypt$") and PASSWORD not in stored
    assert stored != auth_module.hash_password(PASSWORD)  # a fresh salt each time
    assert auth_module.verify_password(PASSWORD, stored)
    assert not auth_module.verify_password("wrong", stored)
    assert not auth_module.verify_password(PASSWORD, None)
    assert not auth_module.verify_password(PASSWORD, "garbage")
    assert not auth_module.verify_password(PASSWORD, "md5$00$00")


def test_set_and_clear_password(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("NOTES_PORT=8050\n", encoding="utf-8")
    auth = Auth("notes", env)
    assert not auth.password_is_set()
    with pytest.raises(PasswordError):
        auth.set_password("short")
    with pytest.raises(auth.PasswordError):  # also reachable through the object
        auth.set_password(" padded password")
    auth.set_password(PASSWORD)
    assert auth.password_is_set() and auth.check_password(PASSWORD)
    assert not auth.check_password("nope")
    text = env.read_text(encoding="utf-8")
    assert "NOTES_PORT=8050" in text  # other lines untouched
    assert auth.PASSWORD_KEY in text and auth.SECRET_KEY in text and PASSWORD not in text

    token = auth.issue_token()
    assert token and auth.token_is_valid(token)
    auth.set_password(PASSWORD + " 2")
    assert not auth.token_is_valid(token)  # a new password rotates the secret

    auth.clear_password()
    assert not auth.password_is_set() and auth.issue_token() is None


def test_a_path_argument_or_env_file_redirects_the_file(tmp_path: Path) -> None:
    auth = Auth("notes", tmp_path / "one.env")
    other = tmp_path / "two.env"
    auth.set_password(PASSWORD, other)
    assert auth.password_is_set(other) and not auth.password_is_set()
    auth.env_file = other
    assert auth.password_is_set() and auth.check_password(PASSWORD)


def test_tokens_expire_and_refresh(tmp_path: Path) -> None:
    auth = Auth("notes", tmp_path / ".env")
    auth.set_password(PASSWORD)
    now = datetime(2026, 11, 5, 12, tzinfo=UTC)
    token = auth.issue_token(now)
    assert token is not None
    assert auth.token_is_valid(token, now)
    assert auth.token_is_valid(token, now + timedelta(days=89))
    assert not auth.token_is_valid(token, now + timedelta(days=90))
    assert not auth.token_is_valid(token + "x", now)
    assert not auth.token_is_valid(None, now)
    assert not auth.token_is_valid("not-a-token", now)
    assert not auth.token_is_valid("abc.def", now)
    assert not auth.token_needs_refresh(token, now + timedelta(hours=23))
    assert auth.token_needs_refresh(token, now + timedelta(days=1, minutes=1))
    assert not auth.token_needs_refresh("garbage", now)


def test_a_token_from_another_app_is_not_valid(tmp_path: Path) -> None:
    notes, other = Auth("notes", tmp_path / "a.env"), Auth("other", tmp_path / "b.env")
    notes.set_password(PASSWORD)
    other.set_password(PASSWORD)
    assert not other.token_is_valid(notes.issue_token())


def test_safe_next() -> None:
    assert auth_module.safe_next("/review?from=2026-11-01") == "/review?from=2026-11-01"
    assert auth_module.safe_next("https://evil.example") == "/"
    assert auth_module.safe_next("//evil.example") == "/"
    assert auth_module.safe_next("/" + chr(92) + "evil.example") == "/"
    assert auth_module.safe_next(None) == "/"
    assert auth_module.safe_next("") == "/"


def test_set_password_command(
    project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    auth = project.auth
    answers = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr(getpass, "getpass", lambda prompt: next(answers))
    assert set_password_main(auth, "Notes", []) == 0 and auth.check_password(PASSWORD)
    answers = iter(["one password", "another"])
    assert set_password_main(auth, "Notes", []) == 1 and "differ" in capsys.readouterr().err
    answers = iter(["short", "short"])
    assert set_password_main(auth, "Notes", []) == 1 and "at least 8" in capsys.readouterr().err
    assert auth.check_password(PASSWORD)  # a refused password changes nothing
    assert set_password_main(auth, "Notes", ["--clear"]) == 0 and not auth.password_is_set()
