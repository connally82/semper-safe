"""Alembic environment.

Reads DATABASE_URL from the same place the app does (db.session._normalize_db_url),
so `alembic upgrade head` works against whatever DB the app is configured for.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make backend/ importable so `from db.session import ...` works.
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from db.session import Base, _resolve_database_url  # noqa: E402
from db import models as _models  # noqa: F401, E402  -- import for metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override the placeholder URL in alembic.ini with the real one.
config.set_main_option("sqlalchemy.url", _resolve_database_url())

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url, target_metadata=target_metadata, literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Don't try to autogenerate against PostGIS internals.
        include_schemas=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Skip PostGIS-managed tables/indexes during autogen comparisons.
            include_object=_skip_postgis_objects,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


def _skip_postgis_objects(obj, name, type_, reflected, compare_to):
    """PostGIS adds tables (spatial_ref_sys) and indexes that we don't manage."""
    if type_ == "table" and name in {"spatial_ref_sys"}:
        return False
    if type_ == "index" and name and name.startswith("idx_") and "_geom" in name:
        return False
    return True


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
