"""Local administration and the single-worker web entry point."""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sqlite3
from pathlib import Path

from .store import Store, password_hash


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="shui", description="Shui! Shared research sessions"
    )
    parser.add_argument(
        "--data-dir", default=os.environ.get("SHUI_DATA_DIR", "~/.local/share/shui")
    )
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-owner", help="Create the first platform owner")
    init.add_argument("--username", default="owner")
    init.add_argument("--display-name", default="Owner")
    init.add_argument(
        "--generate-password",
        action="store_true",
        help="Save a generated password in the private data directory",
    )
    reset = commands.add_parser(
        "reset-password", help="Reset an account password and revoke its web sessions"
    )
    reset.add_argument("username")
    serve = commands.add_parser("serve", help="Start the web app and project workers")
    serve.add_argument("--host", default=os.environ.get("SHUI_HOST", "127.0.0.1"))
    serve.add_argument(
        "--port", type=int, default=int(os.environ.get("SHUI_PORT", "18940"))
    )
    args = parser.parse_args(argv)
    root = Path(args.data_dir).expanduser().resolve()
    os.umask(0o077)
    store = Store(root)

    if args.command == "serve":
        with store.connect() as c:
            if not c.execute("SELECT 1 FROM users WHERE role='owner'").fetchone():
                parser.error("Create your account with 'shui init-owner' first.")
        os.environ["SHUI_DATA_DIR"] = str(root)
        os.environ.setdefault("SHUI_PROXY_URL", "http://127.0.0.1:7890")
        os.environ.setdefault("SHUI_PUBLIC_ORIGIN", f"http://127.0.0.1:{args.port}")
        from .agents import _environment

        try:
            _environment()
        except RuntimeError as exc:
            parser.error(str(exc))
        import uvicorn

        from .app import create_app

        uvicorn.run(
            create_app(root),
            host=args.host,
            port=args.port,
            workers=1,
            access_log=False,
            timeout_graceful_shutdown=15,
        )
        return

    if args.command == "init-owner":
        with store.connect() as c:
            if c.execute("SELECT 1 FROM users WHERE role='owner'").fetchone():
                parser.error(
                    "An owner already exists. Use reset-password to recover access."
                )

    generated = args.command == "init-owner" and args.generate_password
    password = (
        secrets.token_urlsafe(24)
        if generated
        else os.environ.pop("SHUI_OWNER_PASSWORD", "")
    )
    if not password:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Confirm password: "):
            parser.error("Passwords do not match.")
    if not 8 <= len(password) <= 256:
        parser.error("Use a password between 8 and 256 characters.")

    try:
        if args.command == "init-owner":
            credentials = root / "owner-login.txt"
            if generated:
                # Exclusive creation prevents overwriting an existing credential record.
                with credentials.open("x", encoding="utf-8") as stream:
                    os.chmod(credentials, 0o600)
                    stream.write(
                        f"Shui!\nUsername: {args.username}\nPassword: {password}\n"
                    )
            try:
                store.create_user(args.username, args.display_name, password, "owner")
            except Exception:
                if generated:
                    credentials.unlink(missing_ok=True)
                raise
            print(f"Created platform owner @{args.username}.")
            if generated:
                print(f"Private sign-in details: {credentials}")
        else:
            with store.connect() as c:
                user = c.execute(
                    "SELECT id FROM users WHERE username=?", (args.username,)
                ).fetchone()
                if not user:
                    parser.error("Account not found.")
                c.execute(
                    "UPDATE users SET password_hash=? WHERE id=?",
                    (password_hash(password), user["id"]),
                )
                c.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
            print(f"Updated @{args.username}; existing web sessions were revoked.")
    except (ValueError, sqlite3.IntegrityError, FileExistsError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
