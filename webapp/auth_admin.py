from __future__ import annotations

import argparse
import getpass
import uuid

from sqlalchemy import select, text

from persistence import AppUser, LocalCredential, utc_now
from webapp.auth import hash_password
from webapp.database import session_factory
from webapp.settings import Settings


def provision_local_user(login: str, password: str, settings: Settings) -> None:
    if not settings.database_url.startswith("postgresql"):
        raise RuntimeError("Local account provisioning requires PostgreSQL.")
    login_name = login.strip().casefold()
    if len(login_name) < 3 or len(login_name) > 320:
        raise ValueError("Login must be between 3 and 320 characters.")
    password_hash = hash_password(password)
    factory = session_factory(settings)
    with factory.begin() as session:
        database = session.execute(text("select current_database()")).scalar_one()
        if database != session.get_bind().url.database:
            raise RuntimeError("Connected database does not match configured database.")
        revision = session.execute(text("select version_num from alembic_version")).scalar_one_or_none()
        if revision != "9e41c6a7b2d3":
            raise RuntimeError("Apply the current Alembic migrations before provisioning a user.")
        existing = session.scalar(
            select(LocalCredential).where(LocalCredential.login_name == login_name)
        )
        if existing is not None:
            raise ValueError("A local account with this login already exists.")
        user_id = str(uuid.uuid4())
        now = utc_now()
        session.add(
            AppUser(
                id=user_id,
                identity_provider="local",
                external_subject=login_name,
                email=login_name if "@" in login_name else None,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            LocalCredential(
                user_id=user_id,
                login_name=login_name,
                password_hash=password_hash,
                created_at=now,
                updated_at=now,
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage local Cold Outreach web accounts.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser("create-user", help="Create a local web login.")
    create_parser.add_argument("--login", required=True, help="Login name, usually an email address.")
    args = parser.parse_args()

    password = getpass.getpass("New password (minimum 12 characters): ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords did not match.")
    try:
        provision_local_user(args.login, password, Settings.from_environment())
    except (RuntimeError, ValueError) as error:
        raise SystemExit(str(error)) from error
    print("Local web account created.")


if __name__ == "__main__":
    main()
