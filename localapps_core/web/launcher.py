"""Run an app with uvicorn: what `python -m app` calls.

An app's `app/__main__.py` is:

    from localapps_core.web.launcher import run
    from app.config import get_settings

    if __name__ == "__main__":
        run(get_settings())
"""

from __future__ import annotations

import uvicorn

from localapps_core.settings import AppSettings


def run(settings: AppSettings, app: str = "app.main:app") -> None:
    """Serve `app` on the host and port in the settings, `127.0.0.1` unless told otherwise."""
    uvicorn.run(app, host=settings.host, port=settings.port, reload=False)
