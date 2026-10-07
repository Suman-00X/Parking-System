"""Creates the database login users and grants them the privilege roles.

Order: create or update one login user (ensure_login_user); main does it for the application
and audit roles together.
"""

from __future__ import annotations

import argparse
import re
import sys

from sqlalchemy import text

from radreport.db.session import get_engine

APP_ROLE = "radreport_app"
AUDIT_ROLE = "radreport_audit"


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def ensure_login_user(conn, username: str, password: str, member_of: str) -> None:  # noqa: ANN001
    """Create or update a login user and grant it a privilege role."""
    from psycopg import sql

    if not _IDENTIFIER.match(username):
        raise ValueError(f"invalid role name {username!r}")

    exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :n"), {"n": username}).scalar()

    role = sql.Identifier(username)
    secret = sql.Literal(password)
    verb = sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}") if exists else sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}")

    raw = conn.connection.dbapi_connection
    with raw.cursor() as cursor:
        cursor.execute(verb.format(role, secret))
        cursor.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(member_of), role))
        # Inherit the role's privileges without an explicit SET ROLE on every connection.
        cursor.execute(sql.SQL("ALTER ROLE {} INHERIT").format(role))
        # A transaction left open by a crashed or abandoned request is ended by the server, so it cannot pin a connection forever.
        cursor.execute(sql.SQL("ALTER ROLE {} SET idle_in_transaction_session_timeout = '60s'").format(role))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-user", default="radreport_app_login")
    parser.add_argument("--app-password", required=True)
    parser.add_argument("--audit-user", default="radreport_audit_login")
    parser.add_argument("--audit-password")
    parser.add_argument("--url", default=None, help="admin connection URL")
    args = parser.parse_args(argv)

    engine = get_engine(args.url)
    with engine.begin() as conn:
        ensure_login_user(conn, args.app_user, args.app_password, APP_ROLE)
        if args.audit_password:
            ensure_login_user(conn, args.audit_user, args.audit_password, AUDIT_ROLE)

    print(f"bootstrapped {args.app_user} (member of {APP_ROLE})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
