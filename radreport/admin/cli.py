"""Command line for creating the first product admin and resetting passwords, since the admin panel itself needs a login to reach.

Order: create the first admin (create), reset a password (reset) or revoke sessions (revoke);
main dispatches between them. Every later account is added from the admin panel's Users page.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import select

from radreport.admin.auth import MIN_PASSWORD_LENGTH, revoke_all_sessions, set_password
from radreport.admin.users import UserChangeRefused, create_platform_user
from radreport.core.types import PlatformRole
from radreport.db.models.tenancy import PlatformUser
from radreport.db.session import system_session


def _prompt_password() -> str:
    first = getpass.getpass("Password: ")
    if len(first) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"too short: at least {MIN_PASSWORD_LENGTH} characters")
    if getpass.getpass("Repeat: ") != first:
        raise SystemExit("passwords did not match")
    return first


def create(email: str, display_name: str, role: str) -> int:
    with system_session() as session:
        try:
            create_platform_user(session, email=email, display_name=display_name, role=role, password=_prompt_password())
        except UserChangeRefused as exc:
            print(f"{exc.reason}{'; use set-password' if exc.code == 'duplicate' else ''}", file=sys.stderr)
            return 1
    print(f"created {role} {email}")
    return 0


def reset(email: str) -> int:
    with system_session() as session:
        try:
            set_password(session, email=email, password=_prompt_password())
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    print(f"password set for {email}; existing sessions revoked")
    return 0


def revoke(email: str) -> int:
    with system_session() as session:
        user = session.execute(select(PlatformUser).where(PlatformUser.email == email)).scalar_one_or_none()
        if user is None:
            print(f"no platform_user {email}", file=sys.stderr)
            return 1
        count = revoke_all_sessions(session, platform_user_id=user.id)
    print(f"revoked {count} session(s) for {email}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="radreport.admin.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    create_cmd = sub.add_parser("create", help="create a product admin")
    create_cmd.add_argument("--email", required=True)
    create_cmd.add_argument("--display-name", default="Product Admin")
    create_cmd.add_argument("--role", default=PlatformRole.PRODUCT_ADMIN, choices=PlatformRole.values())

    reset_cmd = sub.add_parser("set-password", help="set or replace a password")
    reset_cmd.add_argument("--email", required=True)

    revoke_cmd = sub.add_parser("revoke-sessions", help="sign an admin out everywhere")
    revoke_cmd.add_argument("--email", required=True)

    args = parser.parse_args(argv)
    if args.command == "create":
        return create(args.email, args.display_name, args.role)
    if args.command == "set-password":
        return reset(args.email)
    return revoke(args.email)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
