"""
Application configuration.

All settings loaded from environment variables.
Business settings stored in DB and changed via admin panel without restart.
"""

from __future__ import annotations

import os
import logging
from pathlib import Path
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _require_env(name: str) -> str:
    """Get required environment variable or raise."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is required but not set.")
    return value


@dataclass(frozen=True)
class Config:
    """Immutable application configuration from environment."""

    # === Required ===
    bot_token: str = field(default_factory=lambda: _require_env("BOT_TOKEN"))
    admin_telegram_id: int = field(
        default_factory=lambda: int(_require_env("ADMIN_TELEGRAM_ID"))
    )
    crypto_pay_api_token: str = field(
        default_factory=lambda: _require_env("CRYPTO_PAY_API_TOKEN")
    )

    # === Optional with defaults ===
    support_username: str = field(
        default_factory=lambda: os.getenv("SUPPORT_USERNAME", "")
    )
    database_path: str = field(
        default_factory=lambda: os.getenv("DATABASE_PATH", "/app/data/bot.db")
    )
    crypto_pay_api_url: str = field(
        default_factory=lambda: os.getenv(
            "CRYPTO_PAY_API_URL", "https://pay.crypt.bot/api"
        )
    )
    log_level: str = field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper()
    )

    # === Derived ===
    @property
    def database_url(self) -> str:
        """SQLAlchemy async URL for aiosqlite."""
        return f"sqlite+aiosqlite:///{self.database_path}"

    @property
    def sync_database_url(self) -> str:
        """SQLAlchemy sync URL for alembic."""
        return f"sqlite:///{self.database_path}"

    @property
    def data_dir(self) -> Path:
        """Directory containing the database file."""
        return Path(self.database_path).parent

    @property
    def backup_dir(self) -> Path:
        """Directory for database backups."""
        return self.data_dir / "backups"

    def ensure_dirs(self) -> None:
        """Create data and backup directories if they don't exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)


def setup_logging(level: str = "INFO") -> None:
    """Configure structured logging."""
    import structlog

    log_level = getattr(logging, level, logging.INFO)

    logging.basicConfig(
        level=log_level,
        format="%(message)s",
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


# Singleton config — created once at import
config = Config()
