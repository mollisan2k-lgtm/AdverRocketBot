import os
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from app.db.models import Base
from app.db.engine import _set_sqlite_pragmas
from sqlalchemy import event

# Make sure we use a unique db per worker if xdist is used, but for now a single isolated one is fine
DB_URL = "sqlite+aiosqlite:///file:test_db.sqlite?mode=rwc&uri=true"

# Setup isolated engine
test_engine = create_async_engine(DB_URL, echo=False, pool_pre_ping=True, connect_args={"check_same_thread": False})
event.listen(test_engine.sync_engine, "connect", _set_sqlite_pragmas)
test_session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

# Monkey-patch engine for run_atomic
import app.db.engine as engine_module
engine_module.engine = test_engine
engine_module.session_factory = test_session_factory
engine_module.DATABASE_URL = DB_URL

@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    # Cleanup after test
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

@pytest_asyncio.fixture
async def session() -> AsyncSession:
    async with test_session_factory() as session:
        yield session

