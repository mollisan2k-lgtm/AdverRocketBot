"""Campaign repository."""

from __future__ import annotations

from typing import Sequence

from sqlalchemy import select, update, func, text

from app.db.models import Campaign
from app.repositories.base import BaseRepository
from app.utils.time_utils import utc_now


# v4: Allowed state transitions
ALLOWED_TRANSITIONS = {
    "active": ["paused", "cancelled", "completed"],
    "paused": ["active", "cancelled", "completed"],
    "completed": [],  # terminal
    "cancelled": [],  # terminal
}


class CampaignRepository(BaseRepository[Campaign]):
    model = Campaign

    async def get_active_by_user(self, user_id: int) -> Sequence[Campaign]:
        """Get user's active campaigns."""
        result = await self.session.execute(
            select(Campaign)
            .where(Campaign.user_id == user_id, Campaign.status == "active")
            .order_by(Campaign.created_at.desc())
        )
        return result.scalars().all()

    async def get_by_user(
        self, user_id: int, status: str | None = None,
        offset: int = 0, limit: int = 20
    ) -> Sequence[Campaign]:
        """Get user campaigns, optionally filtered by status."""
        q = select(Campaign).where(Campaign.user_id == user_id)
        if status:
            q = q.where(Campaign.status == status)
        result = await self.session.execute(
            q.order_by(Campaign.created_at.desc())
            .offset(offset).limit(limit)
        )
        return result.scalars().all()

    async def count_active_by_user(self, user_id: int) -> int:
        """Count user's active campaigns."""
        result = await self.session.execute(
            select(func.count(Campaign.id))
            .where(Campaign.user_id == user_id, Campaign.status == "active")
        )
        return result.scalar_one()

    async def get_active_by_category(self, category_id: int) -> Sequence[Campaign]:
        """Get active campaigns for a category (for task distribution)."""
        result = await self.session.execute(
            select(Campaign)
            .where(
                Campaign.category_id == category_id,
                Campaign.status == "active",
                Campaign.completed < Campaign.target,
            )
            .order_by(Campaign.created_at)
        )
        return result.scalars().all()

    async def atomic_increment_completed(self, campaign_id: int) -> int:
        """
        v3: Atomically increment completed IF completed < target.
        Returns rowcount (1 if successful, 0 if campaign already full).
        This is the ONLY way to increment completed — ensures completed <= target.
        """
        result = await self.session.execute(
            update(Campaign)
            .where(
                Campaign.id == campaign_id,
                Campaign.completed < Campaign.target,
                Campaign.status.in_(["active", "paused"]),  # v4: paused tasks can complete
            )
            .values(completed=Campaign.completed + 1)
        )
        return result.rowcount

    async def transition_status(
        self, campaign_id: int, new_status: str, **extra_fields
    ) -> int:
        """
        v4: Conditional status transition.
        UPDATE WHERE status IN (allowed_from_states).
        Returns rowcount (0 if transition not allowed or race).
        """
        # Find which statuses can transition to new_status
        allowed_from = [
            s for s, targets in ALLOWED_TRANSITIONS.items()
            if new_status in targets
        ]
        if not allowed_from:
            return 0

        values = {"status": new_status, **extra_fields}
        if new_status == "completed":
            values["completed_at"] = utc_now()

        result = await self.session.execute(
            update(Campaign)
            .where(
                Campaign.id == campaign_id,
                Campaign.status.in_(allowed_from),
            )
            .values(**values)
        )
        return result.rowcount

    async def get_for_distribution(self, category_id: int) -> Sequence[Campaign]:
        """
        Get campaigns available for task distribution.
        Only active campaigns with remaining capacity.
        """
        result = await self.session.execute(
            select(Campaign)
            .where(
                Campaign.category_id == category_id,
                Campaign.status == "active",
                Campaign.completed < Campaign.target,
            )
            .order_by(Campaign.completed)  # Least completed first (fairness)
        )
        return result.scalars().all()

    # Aliases used by campaign_service
    get_distributable = get_for_distribution
    get_active_for_category = get_active_by_category

    async def conditional_update_status(
        self, campaign_id: int,
        allowed_from: tuple[str, ...],
        new_status: str,
        **extra_fields,
    ) -> bool:
        """
        Conditional status transition.
        Returns True if transition happened.
        """
        values = {"status": new_status, **extra_fields}
        if new_status == "completed":
            values["completed_at"] = utc_now()
        result = await self.session.execute(
            update(Campaign)
            .where(
                Campaign.id == campaign_id,
                Campaign.status.in_(allowed_from),
            )
            .values(**values)
        )
        return result.rowcount > 0
