# Working rules for this project

- **This repository is public.** Nothing specific to any one app, person or server goes in it: no host names, addresses, app names, real paths or data. Examples use an app called `notes`.
- **Keep `documentation/` and `README.md` in step with the code.** Any change to behaviour or to what an app has to write updates the page that describes it in the same piece of work.
- **Write Markdown one paragraph per line.** No hard wrapping.
- Only `localapps_core/web/` imports FastAPI or Starlette. Everything else must be usable from an app's business logic. A test enforces this.
- Every app that uses the package depends on its behaviour staying put. A change an app would have to adapt to is a new first version number, and is described in the commit that makes it.
- A module belongs here when more than one app needs it. Anything only one app needs stays in that app.
- Before finishing: `pytest`, `ruff check .`, `ruff format --check .`, `mypy` all clean.
- Use `.venv\Scripts\python`; do not install `uv`.
- Never commit, tag or push unless asked.
