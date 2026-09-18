"""
Async SQLAlchemy engine & session factory for SQLite.

Key design:
- WAL mode for concurrent read/write
- Foreign keys enforced
- busy_timeout for lock handling
- Short transactions only — no HTTP calls inside transactions
"""

from __future__ import annotations

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import config


def _set_sqlite_pragmas(dbapi_conn, connection_record):
    """Set SQLite PRAGMA on every new raw connection."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA cache_size=-64000")  # 64MB
    cursor.execute("PRAGMA temp_store=MEMORY")
    cursor.close()


def create_engine() -> AsyncEngine:
    """Create async SQLAlchemy engine with SQLite PRAGMAs."""
    engine = create_async_engine(
        config.database_url,
        echo=False,
        pool_pre_ping=True,
        # SQLite needs single connection for write serialization
        # but WAL allows concurrent reads
        connect_args={"check_same_thread": False},
    )

    # Register PRAGMA handler on the sync engine underneath
    event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create async session factory bound to the engine."""
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


# Module-level singletons (initialized at import time)
engine = create_engine()
session_factory = create_session_factory(engine)


async def get_session() -> AsyncSession:
    """Get a new async session. Use as async context manager or close manually."""
    return session_factory()
