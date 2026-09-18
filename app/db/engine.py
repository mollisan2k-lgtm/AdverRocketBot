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


import asyncio
from sqlalchemy.exc import OperationalError

class TransactionTimeoutError(Exception):
    """Raised when atomic_session max retries are exhausted due to SQLite locks."""
    pass

from typing import TypeVar, Callable, Awaitable

T = TypeVar('T')

async def run_atomic(operation: Callable[[AsyncSession], Awaitable[T]], max_retries: int = 5, base_delay: float = 0.1) -> T:
    """
    Run an async business operation inside an exclusive write transaction.
    Handles SQLITE_BUSY by cleanly rolling back, waiting, and retrying the ENTIRE operation from scratch.
    """
    for attempt in range(max_retries):
        async with session_factory() as session:
            try:
                # Force exclusive lock to prevent read-upgrade deadlocks
                await session.execute(text("BEGIN IMMEDIATE"))
                result = await operation(session)
                await session.commit()
                return result
            except OperationalError as e:
                await session.rollback()
                if "database is locked" in str(e).lower() and attempt < max_retries - 1:
                    await asyncio.sleep(base_delay * (2 ** attempt))
                    continue
                raise
            except Exception:
                await session.rollback()
                raise
    raise TransactionTimeoutError(f"Failed to acquire database lock after {max_retries} attempts.")
