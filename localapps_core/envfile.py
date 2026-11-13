"""Read and write single lines of a .env file.

Secrets (a password hash, a signing secret, an API key) live in .env, never in the database,
so they are in no backup and no export. These two functions are the only code that touches
the file.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

_QUOTES = "'" + '"'


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def read_value(name: str, path: Path) -> str | None:
    """The value of `name` from the file, else from the process environment, else None."""
    for line in _read_lines(path):
        stripped = line.strip()
        if stripped.startswith(f"{name}="):
            value = stripped.split("=", 1)[1].strip().strip(_QUOTES)
            if value:
                return value
    return os.environ.get(name) or None


def write_values(values: dict[str, str | None], path: Path) -> None:
    """Replace or add each named line, leaving every other line alone. A None or empty value
    removes the line. The file is left owner-read-only afterwards; it is made writable for the
    duration of the update."""
    kept = [
        ln
        for ln in _read_lines(path)
        if not any(ln.strip().startswith(f"{name}=") for name in values)
    ]
    for name, value in values.items():
        value = (value or "").strip()
        if value:
            kept.append(f"{name}={value}")
    if path.exists():
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    path.write_text("\n".join(kept).rstrip("\n") + "\n", encoding="utf-8")
    os.chmod(path, stat.S_IREAD)
