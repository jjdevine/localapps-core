"""Set or remove an app's login password from a terminal.

An app exposes it as `python -m app.set_password [--clear]`:

    import sys
    from localapps_core.cli import set_password_main
    from app.services.auth import auth

    if __name__ == "__main__":
        sys.exit(set_password_main(auth, "Notes"))

On a server this is how the password is set before the service first starts, so the app is
never reachable without one.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from localapps_core.auth import Auth, PasswordError


def set_password_main(auth: Auth, title: str, argv: list[str] | None = None) -> int:
    """Ask for a new password twice without echo and store it; `--clear` removes it.
    Returns the process exit code."""
    parser = argparse.ArgumentParser(description=f"Set or remove the {title} login password.")
    parser.add_argument("--clear", action="store_true", help="remove the password")
    args = parser.parse_args(argv)
    if args.clear:
        auth.clear_password()
        print("Password removed; the app is open to anyone who can reach it.")
        return 0
    first = getpass.getpass("New password: ")
    second = getpass.getpass("Again: ")
    if first != second:
        print("The two copies differ.", file=sys.stderr)
        return 1
    try:
        auth.set_password(first)
    except PasswordError as exc:
        print(exc, file=sys.stderr)
        return 1
    print("Password set. Restart the app if it is running; every device must log in once.")
    return 0
