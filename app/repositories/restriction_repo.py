"""User restriction repository (v4 bot-applied restrictions)."""

from __future__ import annotations

import json

from sqlalchemy import select, delete

from app.db.models import UserRestriction
from app.repositories.base import BaseRepository
from app.utils.time_utils import utc_now
from datetime import timedelta


class RestrictionRepository(BaseRepository[UserRestriction]):
    model = UserRestriction

    async def get_record(
        self, group_id: int, user_telegram_id: int
    ) -> UserRestriction | None:
        """Get restriction record for (group, user) pair."""
        result = await self.session.execute(
            select(UserRestriction)
            .where(
                UserRestriction.group_id == group_id,
                UserRestriction.user_telegram_id == user_telegram_id,
            )
        )
        return result.scalar_one_or_none()

    async def is_restricted(
        self, group_id: int, user_telegram_id: int
    ) -> bool:
        """Check if user is restricted by bot in this group."""
        record = await self.get_record(group_id, user_telegram_id)
        return record is not None and record.restricted_by_bot

    async def set_desired_state(
        self,
        group_id: int,
        user_telegram_id: int,
        desired_state: str,
        original_permissions: dict | None = None,
    ) -> None:
        """Update the desired state of a restriction and increment operation_token."""
        record = await self.get_record(group_id, user_telegram_id)
        if not record:
            if desired_state == "OFF":
                return
            perms_json = (
                json.dumps(original_permissions, ensure_ascii=False)
                if original_permissions
                else None
            )
            await self.create(
                group_id=group_id,
                user_telegram_id=user_telegram_id,
                desired_state=desired_state,
                actual_state="OFF", # Since we are just creating it
                operation_token=1,
                original_permissions_json=perms_json,
            )
            return

        record.desired_state = desired_state
        record.operation_token += 1
        if original_permissions and not record.original_permissions_json:
            record.original_permissions_json = json.dumps(original_permissions, ensure_ascii=False)

    async def add_restriction(
        self,
        group_id: int,
        user_telegram_id: int,
        original_permissions: dict | None = None,
    ) -> None:
        """v4 wrapper for setting desired state to ON."""
        await self.set_desired_state(group_id, user_telegram_id, "ON", original_permissions)

    async def remove_restriction(
        self, group_id: int, user_telegram_id: int
    ) -> None:
        """v4 wrapper for setting desired state to OFF."""
        await self.set_desired_state(group_id, user_telegram_id, "OFF")

    async def get_by_group(self, group_id: int) -> list[UserRestriction]:
        """Get all restrictions for a group."""
        result = await self.session.execute(
            select(UserRestriction)
            .where(UserRestriction.group_id == group_id)
        )
        return list(result.scalars().all())

    async def get_active_by_group(self, group_id: int) -> list[UserRestriction]:
        """Get active (bot-applied) restrictions for a group."""
        result = await self.session.execute(
            select(UserRestriction)
            .where(
                UserRestriction.group_id == group_id,
                UserRestriction.restricted_by_bot == True,
            )
        )
        return list(result.scalars().all())

    async def claim_for_reconciliation(self, worker_token: str, lease_minutes: int = 5) -> Sequence[UserRestriction]:
        """
        Claim records where desired_state != actual_state for background processing.
        MUST be called inside atomic_session.
        """
        now = utc_now()
        lease_expiry = now + timedelta(minutes=lease_minutes)
        
        result = await self.session.execute(
            select(UserRestriction)
            .where(
                UserRestriction.desired_state != UserRestriction.actual_state,
                (UserRestriction.lease_expires_at == None) | (UserRestriction.lease_expires_at <= now)
            )
            .limit(100) # Process in batches
        )
        restrictions = result.scalars().all()
        for r in restrictions:
            r.worker_token = worker_token
            r.lease_expires_at = lease_expiry
            
        return restrictions
