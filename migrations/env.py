"""Alembic environment for the RDS system-of-record.

The DB URL comes from app settings (env vars / config) so no credentials live in
the repo. Offline mode (`alembic upgrade head --sql`) works without a live DB —
useful for reviewing the generated SQL before provisioning RDS.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No SQLAlchemy model metadata: the baseline migration uses explicit DDL, so
# autogenerate is not used for it.
target_metadata = None


def _database_url() -> str:
    """Resolve the DB URL from app settings, with an offline-safe fallback."""
    try:
        from app.config import settings

        if settings.rds_host:
            return settings.rds_sqlalchemy_url
    except Exception:
        pass
    # Offline / unconfigured: a placeholder lets `--sql` render without a DB.
    return "postgresql+psycopg://user:pass@localhost:5432/vibesense"


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
