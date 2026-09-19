"""Seeds the shared model catalog and a demo lab so a fresh database is usable.

Order: load the model catalog (seed_model_catalog) -> create the first product admin, with a
password taken from the environment (seed_platform_admin) -> create the logins shown on the login
page's test-credentials tab (seed_demo_accounts) -> create a demo lab with sample data
(seed_demo_tenant); main runs all four. Every later account is added from the admin panel.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.adapters.llm.pricing import SEED_PRICES, derive_cache_prices
from radreport.admin.auth import set_password
from radreport.core.config import get_settings
from radreport.core.logging import configure_logging, get_logger
from radreport.core.types import AuthMethod, PlatformRole, ProviderKind, TaskBucket, TaskKey
from radreport.db.models.modelconfig import ModelDefinition, ModelProvider
from radreport.db.models.tenancy import PlatformUser
from radreport.db.session import system_session
from radreport.onboarding.registration import LabRegistration, register_lab

log = get_logger(__name__)

#: The environment variable the first product admin's password is read from.
ADMIN_PASSWORD_ENV = "RADREPORT_SEED_ADMIN_PASSWORD"

#: the per-task split, at corrected pricing.
DEFAULT_TASK_MODELS: dict[str, tuple[str, str]] = {
    # Consequential — stay on a frontier model regardless of local hardware.
    TaskKey.EXTRACTION: ("claude-sonnet-5", TaskBucket.CONSEQUENTIAL),
    TaskKey.SELF_CORRECTION: ("claude-sonnet-5", TaskBucket.CONSEQUENTIAL),
    TaskKey.VERIFICATION: ("claude-sonnet-5", TaskBucket.CONSEQUENTIAL),
    TaskKey.COMPOSE: ("claude-sonnet-5", TaskBucket.CONSEQUENTIAL),
    # Bounded — the set. Local candidates in Beta ( Scope A).
    TaskKey.UTTERANCE_CLASSIFICATION: ("claude-haiku-4-5", TaskBucket.BOUNDED),
    TaskKey.ROUTING_SHORTLIST: ("claude-haiku-4-5", TaskBucket.BOUNDED),
    TaskKey.ROUTING_PICK: ("claude-haiku-4-5", TaskBucket.BOUNDED),
    # Kept on cloud Haiku even if compose ever goes local: an 8B model checking
    # an 8B model's work removes the independence the check depends on.
    TaskKey.ROUNDTRIP_CHECK: ("claude-haiku-4-5", TaskBucket.BOUNDED),
}


def seed_model_catalog(session: Session) -> dict[str, ModelDefinition]:
    anthropic = session.execute(select(ModelProvider).where(ModelProvider.name == "anthropic", ModelProvider.tenant_id.is_(None))).scalar_one_or_none()

    if anthropic is None:
        anthropic = ModelProvider(tenant_id=None, name="anthropic", kind=ProviderKind.CLOUD_API, default_endpoint="https://api.anthropic.com", auth_method=AuthMethod.API_KEY)
        session.add(anthropic)
        session.flush()

    # A row for local models with no endpoint: each lab's box has its own
    # address, set per-tenant on `model_definition.endpoint_override`.
    local = session.execute(select(ModelProvider).where(ModelProvider.name == "local_openai_compatible", ModelProvider.tenant_id.is_(None))).scalar_one_or_none()
    if local is None:
        local = ModelProvider(tenant_id=None, name="local_openai_compatible", kind=ProviderKind.LOCAL_OPENAI_COMPATIBLE, default_endpoint=None, auth_method=AuthMethod.NONE)
        session.add(local)
        session.flush()

    definitions: dict[str, ModelDefinition] = {}
    for identifier, price in SEED_PRICES.items():
        existing = session.execute(select(ModelDefinition).where(ModelDefinition.provider_id == anthropic.id, ModelDefinition.model_identifier == identifier)).scalar_one_or_none()
        if existing is not None:
            definitions[identifier] = existing
            continue

        cache_read, cache_write = derive_cache_prices(price.input_per_1k)
        definition = ModelDefinition(tenant_id=None, provider_id=anthropic.id, model_identifier=identifier, display_name=identifier.replace("-", " ").title(), input_price_per_1k=price.input_per_1k, output_price_per_1k=price.output_per_1k, cache_read_price_per_1k=cache_read, cache_write_price_per_1k=cache_write, batch_discount_factor=0.5, context_window=price.context_window)
        session.add(definition)
        definitions[identifier] = definition

    session.flush()
    log.info("model_catalog_seeded", providers=2, definitions=len(definitions))
    return definitions


def seed_platform_admin(session: Session, email: str = "admin@radreport.local", password: str | None = None) -> PlatformUser:
    """Create the first product admin, and set its password if one is given and none is set yet."""
    user = session.execute(select(PlatformUser).where(PlatformUser.email == email)).scalar_one_or_none()
    if user is None:
        user = PlatformUser(email=email, display_name="Product Admin", role=PlatformRole.PRODUCT_ADMIN)
        session.add(user)
        session.flush()
    if password and not user.password_hash:
        set_password(session, email=email, password=password)
    return user


def seed_demo_accounts(session: Session) -> list[PlatformUser]:
    """Create each configured demo login, keeping its role and password in step with the settings."""
    accounts = []
    for account in get_settings().demo_accounts:
        user = session.execute(select(PlatformUser).where(PlatformUser.email == account.email)).scalar_one_or_none()
        if user is None:
            user = PlatformUser(email=account.email, display_name=account.label, role=PlatformRole(account.role))
            session.add(user)
            session.flush()
        user.role = PlatformRole(account.role)
        set_password(session, email=account.email, password=account.password)
        accounts.append(user)
    return accounts


def seed_demo_tenant(session: Session, actor_id: uuid.UUID) -> uuid.UUID:
    result = register_lab(
        session,
        LabRegistration(
            name="Demo Diagnostics",
            slug="demo",
            admin_email="labadmin@demo.local",
            admin_display_name="Demo Lab Admin",
            admin_employee_code="DEMO-001",
            # False on purpose.
            training_pooling_consent=False,
        ),
        actor_id=actor_id,
    )
    return result.tenant.id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo-tenant", action="store_true")
    parser.add_argument("--admin-email", default="admin@radreport.local")
    args = parser.parse_args(argv)

    configure_logging()
    with system_session() as session:
        seed_model_catalog(session)
        admin = seed_platform_admin(session, email=args.admin_email, password=os.environ.get(ADMIN_PASSWORD_ENV))
        for account in seed_demo_accounts(session):
            print(f"demo login: {account.email} ({account.role})")
        if args.demo_tenant:
            tenant_id = seed_demo_tenant(session, admin.id)
            print(f"demo tenant: {tenant_id}")
        print(f"platform admin: {admin.id} <{admin.email}>")
        if not admin.password_hash:
            print(f"  no password set: export {ADMIN_PASSWORD_ENV} and re-run, or run `make admin-password EMAIL={admin.email}`")

    print("\nNote: no task_model_assignment rows were created. An assignment cannot go active without a gold-set eval_run, and seeding one would bypass the gate the registry exists to enforce.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
