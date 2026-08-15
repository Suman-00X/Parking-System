"""Alembic's entry point, taking the database URL from settings so one env file serves local, test and deployed runs.

Order: run_migrations_offline emits SQL; run_migrations_online applies it to a live database.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from radreport.core.config import get_settings
from radreport.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

_settings = get_settings()
config.set_main_option("sqlalchemy.url", context.get_x_argument(as_dictionary=True).get("url") or _settings.database_url)


def _include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001, ANN202
    """Keep partition children out of autogenerate."""
    if type_ == "table" and "_y" in name and name.rsplit("_y", 1)[-1][:4].isdigit():
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"}, include_object=_include_object)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, include_object=_include_object, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
