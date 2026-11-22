"""The Claude API: an app's key, the client made with it, and the words for what goes wrong.
No test reaches the API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest

from localapps_core import claude as claude_module
from localapps_core.auth import Auth
from localapps_core.claude import Claude, ClaudeError, error_text, masked, valid_model


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("NOTES_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / ".env"


def test_the_key_is_kept_under_the_apps_own_name(env: Path) -> None:
    claude = Claude(Auth("notes", env))
    assert claude.KEY_NAME == "NOTES_ANTHROPIC_API_KEY"
    assert claude.api_key() is None and not claude.key_is_set() and claude.key_hint() is None

    claude.set_api_key(" sk-ant-test-1234 ")
    assert claude.api_key() == "sk-ant-test-1234" and claude.key_is_set()
    assert env.read_text(encoding="utf-8") == "NOTES_ANTHROPIC_API_KEY=sk-ant-test-1234\n"
    # Only the last four characters are ever shown.
    assert claude.key_hint() == "••••••••1234" and masked(None) is None and masked("") is None

    claude.set_api_key("sk-ant-new-9999")  # the file was left read-only and is still writable
    assert claude.api_key() == "sk-ant-new-9999"
    assert env.read_text(encoding="utf-8").count("ANTHROPIC_API_KEY") == 1
    claude.set_api_key(None)
    assert claude.api_key() is None

    # One call can be pointed at another file.
    other = env.with_name("other.env")
    claude.set_api_key("sk-ant-other-0000", other)
    assert claude.api_key(other) == "sk-ant-other-0000" and claude.api_key() is None


def test_the_key_can_come_from_the_process_environment(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = Claude(Auth("notes", env))
    monkeypatch.setenv("NOTES_ANTHROPIC_API_KEY", "from-outside")
    assert claude.api_key() == "from-outside"
    claude.set_api_key("from-the-file")
    assert claude.api_key() == "from-the-file"  # the file wins


def test_a_key_under_an_older_name_is_read_until_one_is_saved(env: Path) -> None:
    env.write_text("OTHER=1\nANTHROPIC_API_KEY=sk-ant-old-1111\n", encoding="utf-8")
    claude = Claude(Auth("notes", env), legacy_key_names=["ANTHROPIC_API_KEY"])
    assert claude.api_key() == "sk-ant-old-1111" and claude.key_hint() == "••••••••1111"
    assert Claude(Auth("notes", env)).api_key() is None  # only when the app names it

    # Saving a key moves it to the app's own name; the old line goes, others stay.
    claude.set_api_key("sk-ant-new-2222")
    assert env.read_text(encoding="utf-8") == "OTHER=1\nNOTES_ANTHROPIC_API_KEY=sk-ant-new-2222\n"
    # Removing the key removes it under both names.
    other = env.with_name("other.env")
    other.write_text("ANTHROPIC_API_KEY=sk-ant-old-1111\n", encoding="utf-8")
    claude.set_api_key(None, other)
    assert claude.api_key(other) is None and other.read_text(encoding="utf-8") == "\n"


def test_the_settings_form_replaces_removes_or_leaves_the_key(env: Path) -> None:
    claude = Claude(Auth("notes", env))
    claude.save_from_form("  ", remove=False)
    assert claude.api_key() is None
    claude.save_from_form(" sk-ant-test-1234 ", remove=False)
    assert claude.api_key() == "sk-ant-test-1234"
    claude.save_from_form("", remove=False)  # the box is empty because the key is never shown
    assert claude.api_key() == "sk-ant-test-1234"
    claude.save_from_form("sk-ant-ignored-0000", remove=True)  # the tick wins
    assert claude.api_key() is None


class Made:
    """Stands in for the SDK's client: it keeps what it was made with."""

    def __init__(self, **options: Any) -> None:
        self.options = options


def test_the_client_is_made_with_the_apps_key(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(anthropic, "Anthropic", Made)
    claude = Claude(Auth("notes", env))
    with pytest.raises(ClaudeError, match="No Claude API key is set"):
        claude.client()
    claude.set_api_key("sk-ant-test-1234")
    made: Any = claude.client()
    assert made.options == {"api_key": "sk-ant-test-1234"}  # the SDK's own timeout and retries

    patient: Any = Claude(Auth("notes", env), timeout=60.0, max_retries=1).client()
    assert patient.options == {"api_key": "sk-ant-test-1234", "timeout": 60.0, "max_retries": 1}


def test_a_test_can_be_kept_from_the_api(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    claude = Claude(Auth("notes", env))
    claude.set_api_key("sk-ant-test-1234")
    claude_module.forbid_requests(monkeypatch)
    with pytest.raises(AssertionError, match="reached for the Claude API"):
        claude.client()


def test_only_a_model_on_offer_is_used() -> None:
    ids = [model for model, _ in claude_module.MODELS]
    assert len(set(ids)) == len(ids) and all(name for _, name in claude_module.MODELS)
    assert valid_model(ids[-1], ids[0]) == ids[-1]
    for unknown in (None, "", "claude-of-old"):
        assert valid_model(unknown, ids[0]) == ids[0]


def status_error(kind: type[anthropic.APIStatusError], status: int) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.example/v1/messages")
    return kind("what the API said", response=httpx2.Response(status, request=request), body=None)


def test_what_went_wrong_is_said_in_words_for_the_page() -> None:
    cases: list[tuple[Exception, str]] = [
        (ClaudeError("No Claude API key is set."), "No Claude API key is set."),
        (status_error(anthropic.AuthenticationError, 401), "key was refused"),
        (status_error(anthropic.PermissionDeniedError, 403), "cannot use the model"),
        (status_error(anthropic.NotFoundError, 404), "cannot use the model"),
        (status_error(anthropic.RateLimitError, 429), "turning requests away"),
        (status_error(anthropic.BadRequestError, 400), "would not take the request: what the"),
        (status_error(anthropic.InternalServerError, 500), "an error (500): what the API said"),
        (
            anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.example")),
            "could not be reached",
        ),
        (RuntimeError("something else"), "failed: something else"),
    ]
    for exc, said in cases:
        assert said in error_text(exc), exc
    assert Claude.error_text is error_text and Claude.ClaudeError is ClaudeError
