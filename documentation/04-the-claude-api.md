# 04 · The Claude API

An app that asks Claude for something does it with Anthropic's own SDK. What it asks, when, and what it does with the answer are the app's own business. `localapps_core.claude` holds what is the same in every app: where the key lives and how it is set, the client made with it, the models on offer, the words for what goes wrong, and a way to keep tests off the network.

The first half of this page is the recipe for an app. The second half says how it works.

## Adding it to an app

### Install

Ask for the extra that installs the SDK, and pin `anthropic` with the app's other dependencies:

```toml
dependencies = [
    "localapps-core[claude] @ git+https://github.com/jjdevine/localapps-core.git@v0.3.0",
]
```

An app that also sends push notifications asks for both: `localapps-core[push,claude]`.

### `app/services/claude_key.py`

One object, made from the app's login object so the key is kept in the same `.env` under the same app name:

```python
from localapps_core.claude import Claude

from app.services.auth import auth

claude = Claude(auth)
```

`Claude(auth, timeout=60.0, max_retries=1)` passes those two to the SDK's client; left out, the SDK's own apply. `legacy_key_names=["ANTHROPIC_API_KEY"]` is for an app that kept its key under another name before: that name is read while the app's own has nothing, and its line is removed the next time a key is saved, so nobody has to edit `.env` by hand.

### Asking

The app's own module makes the client and uses the SDK as its documentation describes:

```python
from localapps_core.claude import FALLBACK_BETA, ClaudeError, error_text

from app.services.claude_key import claude


def ask(model: str, question: str) -> str:
    try:
        with claude.client() as client:  # ClaudeError when no key is set
            message = client.beta.messages.create(
                model=model,
                max_tokens=16000,
                messages=[{"role": "user", "content": question}],
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
    except Exception as exc:
        raise ClaudeError(error_text(exc)) from exc
    return "".join(block.text for block in message.content if block.type == "text")
```

`error_text` turns whatever was raised into a sentence for the page. `FALLBACK_BETA` with `fallbacks="default"` has a request the model declines run on that model's stand-in within the same call; an app that does not want that leaves both out.

### The Settings page

The key is typed into the app's Settings page and never shown again: the page shows `claude.key_hint()`, its last four characters, and the form has a password box for a new key and a tick to remove the one there.

```html
<label>Claude API key
  <input name="api_key" type="password" autocomplete="off">
  <small>{% if key_hint %}A key is set ({{ key_hint }}). Enter a new one to replace it.{% else %}No key set.{% endif %}</small>
</label>
{% if key_hint %}<label><input type="checkbox" name="clear_api_key"> Remove the stored key</label>{% endif %}
```

```python
claude.save_from_form(api_key, remove=clear_api_key == "on")
```

An empty box with no tick leaves the key as it is, because the page never puts the key in the box.

The model is not a secret, so it is one of the app's ordinary settings, chosen on the same page from `MODELS`, a tuple of `(id, name)` pairs; `valid_model(chosen, default)` gives the one to use, falling back to the app's default when what is stored is no longer on offer.

### The app's tests

No test may reach the API, whatever key the machine holds. In the app's `conftest.py`:

```python
from localapps_core.claude import forbid_requests


@pytest.fixture(autouse=True)
def claude_is_out_of_reach(monkeypatch):
    forbid_requests(monkeypatch)
```

After that, making the SDK's client fails the test. A test that needs an answer stands in for the app's own functions that ask, or replaces `claude.client`.

### What else the app writes down

The app's `CLAUDE.md` names the Claude API as an outbound request and says what is sent. Its documentation says what each request is for, and that requests are paid for on the account the key belongs to.

## How it works

**The key.** One for each app, as `<NAME>_ANTHROPIC_API_KEY` in the app's `.env`, read and written with `read_value` and `write_values` like every other secret, so it is never in the database, a backup or git, and the file is left owner-read-only. `api_key()` reads the file first and the process environment second. A key per app, not one shared between apps, means each app's spending shows on its own in the Anthropic console and one key can be withdrawn without stopping the others.

**The client.** `client()` reads the key each time it is called, so a key saved on the Settings page is used by the next request without a restart. With no key it raises `ClaudeError` with words for the page, before anything is sent.

**The models.** `MODELS` is the one list of what an app may offer, the most capable first. When Anthropic releases or retires a model the list changes here, and each app picks it up with the tag.

**What goes wrong.** `error_text` knows the SDK's exceptions: a key that is refused, a model the key cannot use, a rate or spending limit, a request the API would not take, any other error from the API with its status, and no connection. Anything else is reported as it is. A `ClaudeError` already has its words and comes back unchanged.

**What stays in the app.** Every prompt, what is sent and when, how answers are read and stored, the choice of default model and effort, and the page that shows the result.
