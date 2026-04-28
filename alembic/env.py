"""Alembic env — auto-discovers all models from shared/database/models."""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

# Cargar <repo>/.env para que DATABASE_URL esté disponible (igual que deploy/sls scripts).
_repo_root = Path(__file__).resolve().parent.parent
_scripts = _repo_root / "scripts"
if str(_scripts) not in sys.path:
    sys.path.insert(0, str(_scripts))
from dotenv_loader import load_root_dotenv_defaults  # noqa: E402

load_root_dotenv_defaults(_repo_root)

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make sure src/shared is importable so ``database.*`` resolves.
shared_path = str(Path(__file__).resolve().parent.parent / "src" / "shared")
if shared_path not in sys.path:
    sys.path.insert(0, shared_path)

from database.base import Base  # noqa: E402
import database.models  # noqa: E402, F401  — registers all tables on Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Allow DATABASE_URL env-var to override alembic.ini value.
db_url = os.getenv("DATABASE_URL")
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
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
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
