"""The parts every small self-hosted FastAPI + SQLite app repeats.

Nothing outside `localapps_core.web` imports FastAPI, so an app's business logic can use the
login, database, migration, backup and push code without pulling the web framework in.
"""

__version__ = "0.2.2"
