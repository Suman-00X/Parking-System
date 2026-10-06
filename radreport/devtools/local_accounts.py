"""Creates a working sign-in for every role on a developer machine, and writes them to a local file.

Order: refuse outside local/test/development (require_local) -> give each platform role an
account (_platform_accounts) -> register a lab with one person per lab role (_lab_accounts) ->
write every credential to a gitignored Markdown file (write_credentials). seed_local_accounts
runs all of it; rerunning gives every account a fresh password.
"""

from __future__ import annotations

import datetime as dt
import secrets
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from radreport.admin import auth as admin_auth
from radreport.auth import lab as lab_auth
from radreport.core.config import get_settings
from radreport.core.types import PlatformRole, UserRole
from radreport.db.models.identity import AppUser
from radreport.db.models.tenancy import PlatformUser, Tenant
from radreport.db.session import system_session, tenant_session
from radreport.onboarding import roster
from radreport.onboarding.registration import LabRegistration, register_lab

LOCAL_ENVIRONMENTS = frozenset({"local", "test", "development"})
LAB_NAME, LAB_SLUG = "Sunrise Imaging", "sunrise"

#: (role, email, display name) for the admin panel.
PLATFORM_ACCOUNTS = ((PlatformRole.PRODUCT_ADMIN, "admin@radreport.local", "Product Admin"), (PlatformRole.SUPPORT, "support@radreport.local", "Support Desk"))

#: (employee code, display name, email, roles) for the lab; the first is its lab admin.
LAB_PEOPLE = (("SUN-ADM", "Priya Lab Admin", "labadmin@sunrise.local", (UserRole.LAB_ADMIN,)), ("SUN-R01", "Dr Arjun Rao", "radiologist@sunrise.local", (UserRole.RADIOLOGIST,)), ("SUN-R02", "Dr Meera Iyer", "radiologist2@sunrise.local", (UserRole.RADIOLOGIST,)), ("SUN-T01", "Kavya Transcriptionist", "transcriptionist@sunrise.local", (UserRole.TRANSCRIPTIONIST,)), ("SUN-A01", "Rohan Auditor", "auditor@sunrise.local", (UserRole.AUDITOR,)))


@dataclass(frozen=True, slots=True)
class Credential:
    realm: str
    role: str
    name: str
    email: str
    password: str


def require_local() -> None:
    """These passwords are written to disk in clear text; never on a real deployment."""
    environment = get_settings().environment
    if environment not in LOCAL_ENVIRONMENTS:
        raise SystemExit(f"refusing to seed local accounts in environment {environment!r}; this is for developer machines only")


def _password() -> str:
    return secrets.token_urlsafe(12)


def _platform_accounts() -> list[Credential]:
    created: list[Credential] = []
    with system_session() as session:
        for role, email, name in PLATFORM_ACCOUNTS:
            user = session.execute(select(PlatformUser).where(PlatformUser.email == email)).scalar_one_or_none()
            if user is None:
                user = PlatformUser(email=email, display_name=name, role=role)
                session.add(user)
                session.flush()
            user.role, user.is_active = role, True
            password = _password()
            admin_auth.set_password(session, email=email, password=password)
            created.append(Credential("admin panel", role, user.display_name, email, password))
    return created


def _lab_accounts() -> list[Credential]:
    lead_code, lead_name, lead_email, _roles = LAB_PEOPLE[0]
    with system_session() as session:
        tenant = session.execute(select(Tenant).where(Tenant.slug == LAB_SLUG)).scalar_one_or_none()
        if tenant is None:
            tenant = register_lab(session, LabRegistration(name=LAB_NAME, slug=LAB_SLUG, admin_email=lead_email, admin_display_name=lead_name, admin_employee_code=lead_code)).tenant
        tenant_id = tenant.id

    created: list[Credential] = []
    with tenant_session(tenant_id) as session:
        rows = [roster.RosterRow(employee_code=code, display_name=name, email=email, roles=roles) for code, name, email, roles in LAB_PEOPLE[1:]]
        roster.import_roster(session, tenant_id=tenant_id, rows=rows)
        for _code, name, email, roles in LAB_PEOPLE:
            user = session.execute(select(AppUser).where(AppUser.tenant_id == tenant_id, AppUser.email == email)).scalar_one()
            user.is_active = True
            password = _password()
            lab_auth.set_password(session, user_id=user.id, password=password, actor_id=None)
            created.append(Credential("lab", ", ".join(roles), name, email, password))
    return created


def write_credentials(path: Path, platform: list[Credential], lab: list[Credential], *, base_url: str) -> None:
    """A Markdown sheet of every sign-in; gitignored, and regenerated on every run."""

    def rows(items: list[Credential]) -> str:
        return "\n".join(f"| {c.role} | {c.name} | `{c.email}` | `{c.password}` |" for c in items)

    first_lab = lab[1] if len(lab) > 1 else lab[0]
    path.write_text(
        f"""# Local credentials

Generated {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M} UTC by `python -m radreport.devtools.seed --local-accounts`.
**Local development only.** This file is gitignored; never commit it or reuse these
passwords anywhere. Rerunning the command gives every account a new password.

## Admin panel

Sign in at {base_url}/admin/login

| Role | Name | Email | Password |
|---|---|---|---|
{rows(platform)}

`support` can see everything and change nothing except its own password.

## Lab: {LAB_NAME}

Lab slug **`{LAB_SLUG}`**. In a browser, sign in at {base_url}/ui/login with the slug,
email and password. The review queue is at {base_url}/ui/queue.

| Role | Name | Email | Password |
|---|---|---|---|
{rows(lab)}

From a script or API client:

```bash
curl -s -X POST {base_url}/auth/login \\
  -H 'content-type: application/json' \\
  -d '{{"lab": "{LAB_SLUG}", "email": "{first_lab.email}", "password": "{first_lab.password}"}}'
# then send: Authorization: Bearer <access_token>
```

Which role may call which route is set in `radreport/api/access_policy.xml`.
""",
        encoding="utf-8",
    )


def seed_local_accounts(path: Path, *, base_url: str = "http://127.0.0.1:8000") -> tuple[list[Credential], list[Credential]]:
    """Create or re-password every local account and write the credentials sheet."""
    require_local()
    platform = _platform_accounts()
    lab = _lab_accounts()
    write_credentials(path, platform, lab, base_url=base_url)
    return platform, lab
