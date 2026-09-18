"""
Base repository — common CRUD operations via SQLAlchemy.

All repositories use AsyncSession passed from middleware/service layer.
No direct DB engine access — keeps transactions short and manageable.
"""

from __future__ import annotations

from typing import TypeVar, Generic, Type, Sequence, Any

from sqlalchemy import select, update, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Base

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    """Generic async repository with common CRUD."""

    model: Type[ModelT]

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, id_: int) -> ModelT | None:
        """Get single record by primary key."""
        return await self.session.get(self.model, id_)

    async def get_by_id_for_update(self, id_: int) -> ModelT | None:
        """Get single record by primary key with FOR UPDATE lock (row-level)."""
        result = await self.session.execute(
            select(self.model)
            .where(self.model.id == id_)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def get_all(
        self,
        offset: int = 0,
        limit: int = 100,
    ) -> Sequence[ModelT]:
        """Get paginated records."""
        result = await self.session.execute(
            select(self.model)
            .offset(offset)
            .limit(limit)
        )
        return result.scalars().all()

    async def count(self) -> int:
        """Count all records."""
        result = await self.session.execute(
            select(func.count(self.model.id))
        )
        return result.scalar_one()

    async def create(self, **kwargs: Any) -> ModelT:
        """Create a new record."""
        instance = self.model(**kwargs)
        self.session.add(instance)
        await self.session.flush()
        return instance

    async def update_by_id(self, id_: int, **kwargs: Any) -> int:
        """Update a record by ID. Returns rowcount."""
        result = await self.session.execute(
            update(self.model)
            .where(self.model.id == id_)
            .values(**kwargs)
        )
        return result.rowcount

    async def delete_by_id(self, id_: int) -> int:
        """Delete a record by ID. Returns rowcount."""
        result = await self.session.execute(
            delete(self.model)
            .where(self.model.id == id_)
        )
        return result.rowcount

    async def exists(self, id_: int) -> bool:
        """Check if record exists."""
        result = await self.session.execute(
            select(func.count(self.model.id))
            .where(self.model.id == id_)
        )
        return result.scalar_one() > 0
