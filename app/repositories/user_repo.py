"""User repository."""

from __future__ import annotations

from sqlalchemy import select, update

from app.db.models import User
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        """Find user by Telegram ID."""
        result = await self.session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        return result.scalar_one_or_none()

    async def get_or_create(
        self,
        telegram_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> tuple[User, bool]:
        """Get existing user or create new one. Returns (user, created)."""
        from sqlalchemy.dialects.sqlite import insert
        from app.utils.time_utils import utc_now
        
        now = utc_now()
        stmt = insert(User).values(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            available="0.00",
            reserved="0.00",
            created_at=now,
            updated_at=now,
        )
        
        # On conflict update profile data
        update_dict = {
            "username": stmt.excluded.username,
            "first_name": stmt.excluded.first_name,
            "last_name": stmt.excluded.last_name,
            "updated_at": now,
        }
        
        stmt = stmt.on_conflict_do_update(
            index_elements=['telegram_id'],
            set_=update_dict
        ).returning(User)
        
        result = await self.session.execute(stmt)
        await self.session.flush()
        
        user = result.scalar_one()
        # We can't perfectly tell if it was created or updated with this approach 
        # unless we compare created_at and updated_at, but we can assume False for 'created' 
        # in most callers since they just need the user object.
        # Let's check created_at == updated_at as a proxy for created.
        created = user.created_at == user.updated_at
        return user, created

    async def search_by_username(self, query: str) -> list[User]:
        """Search users by username (with or without @)."""
        clean = query.lstrip("@").lower()
        result = await self.session.execute(
            select(User).where(
                User.username.ilike(f"%{clean}%")
            ).limit(50)
        )
        return list(result.scalars().all())

    async def block(self, user_id: int, reason: str) -> int:
        """Block a user."""
        from app.utils.time_utils import utc_now
        return await self.update_by_id(
            user_id,
            is_blocked=True,
            block_reason=reason,
            blocked_at=utc_now(),
        )

    async def unblock(self, user_id: int) -> int:
        """Unblock a user."""
        return await self.update_by_id(
            user_id,
            is_blocked=False,
            block_reason=None,
            blocked_at=None,
        )

    async def get_balance_for_update(self, user_id: int) -> User | None:
        """Get user with row lock for financial operations."""
        return await self.get_by_id_for_update(user_id)

    async def atomic_update_balance(
        self,
        user_id: int,
        available: str,
        reserved: str,
        held_for_withdrawal: str | None = None,
    ) -> int:
        """
        Atomically update user balance fields.
        Called within a transaction after validation.
        """
        kwargs = {
            "available": available,
            "reserved": reserved,
        }
        if held_for_withdrawal is not None:
            kwargs["held_for_withdrawal"] = held_for_withdrawal
            
        return await self.update_by_id(
            user_id,
            **kwargs
        )
