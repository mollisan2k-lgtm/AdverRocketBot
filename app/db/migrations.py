"""
Database migration helpers.

Runs pending Alembic migrations at startup.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig

from app.config import config

logger = logging.getLogger(__name__)


def get_alembic_config() -> AlembicConfig:
    """Create Alembic config pointing to our migration scripts."""
    # Find project root (where alembic.ini lives)
    project_root = Path(__file__).parent.parent.parent
    alembic_ini = project_root / "alembic.ini"

    cfg = AlembicConfig(str(alembic_ini))
    cfg.set_main_option("sqlalchemy.url", config.sync_database_url)
    cfg.set_main_option("script_location", str(project_root / "alembic"))
    return cfg


def run_migrations() -> None:
    """Apply all pending migrations. Called at startup BEFORE bot polling."""
    logger.info("Checking for pending database migrations...")
    cfg = get_alembic_config()
    command.upgrade(cfg, "head")
    logger.info("Database migrations completed.")
