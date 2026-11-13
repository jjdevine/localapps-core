"""The /health route: is the app up, and is its database at the newest migration?

It needs no login, so a deploy script, a service manager or a monitor can poll it, and it
gives away only the schema revision.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from localapps_core.db import Database
from localapps_core.migrations import Migrations


def health_router(database: Database, migrations: Migrations) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    def health(response: Response) -> dict[str, str | None]:
        """200 when the database is at the newest migration, 503 when it is not, so a deploy
        that left the schema behind fails its health check instead of passing it."""
        schema = migrations.current_revision(database.get_engine())
        head = migrations.head_revision()
        ok = schema is not None and schema == head
        if not ok:
            response.status_code = 503
        return {
            "status": "ok" if ok else "migration-pending",
            "schema_version": schema,
            "head_version": head,
        }

    return router
