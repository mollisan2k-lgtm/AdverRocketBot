"""Campaign task repository."""

from __future__ import annotations

from typing import Sequence

from sqlalchemy import select, update, func

from app.db.models import CampaignTask
from app.repositories.base import BaseRepository
from app.utils.time_utils import utc_now, minutes_from_now


class TaskRepository(BaseRepository[CampaignTask]):
    model = CampaignTask

    async def get_active_by_user(self, user_telegram_id: int) -> Sequence[CampaignTask]:
        """Get all active tasks for a Telegram user."""
        result = await self.session.execute(
            select(CampaignTask)
            .where(
                CampaignTask.user_telegram_id == user_telegram_id,
                CampaignTask.status.in_(["active", "pending_restriction"]),
            )
            .order_by(CampaignTask.created_at.desc())
        )
        return result.scalars().all()

    async def get_active_by_campaign(self, campaign_id: int) -> Sequence[CampaignTask]:
        """Get all active tasks for a campaign."""
        result = await self.session.execute(
            select(CampaignTask)
            .where(
                CampaignTask.campaign_id == campaign_id,
                CampaignTask.status.in_(["active", "pending_restriction"]),
            )
        )
        return result.scalars().all()

    async def get_active_by_group(self, group_id: int) -> Sequence[CampaignTask]:
        """Get all active tasks distributed to a seller group."""
        result = await self.session.execute(
            select(CampaignTask)
            .where(
                CampaignTask.group_id == group_id,
                CampaignTask.status.in_(["active", "pending_restriction"]),
            )
        )
        return result.scalars().all()

    async def has_active_task(
        self, user_telegram_id: int, campaign_id: int
    ) -> bool:
        """Check if user already has an active task for this campaign (v3 dedup)."""
        result = await self.session.execute(
            select(func.count(CampaignTask.id))
            .where(
                CampaignTask.user_telegram_id == user_telegram_id,
                CampaignTask.campaign_id == campaign_id,
                CampaignTask.status.in_(["active", "pending_restriction"]),
            )
        )
        return result.scalar_one() > 0

    async def has_active_task_for_target(
        self, user_telegram_id: int, target_chat_id: int
    ) -> bool:
        """Check if user already has an active task for this target (v4 dedup)."""
        result = await self.session.execute(
            select(func.count(CampaignTask.id))
            .where(
                CampaignTask.user_telegram_id == user_telegram_id,
                CampaignTask.target_chat_id == target_chat_id,
                CampaignTask.status.in_(["active", "pending_restriction"]),
            )
        )
        return result.scalar_one() > 0

    async def create_task(
        self,
        campaign_id: int,
        group_id: int,
        user_telegram_id: int,
        target_chat_id: int,
        interval_minutes_snapshot: int,
        ttl_minutes: int | None = None,
    ) -> CampaignTask:
        """Create a new task with optional TTL."""
        expires_at = minutes_from_now(ttl_minutes) if ttl_minutes else None
        return await self.create(
            campaign_id=campaign_id,
            group_id=group_id,
            user_telegram_id=user_telegram_id,
            target_chat_id=target_chat_id,
            interval_minutes_snapshot=interval_minutes_snapshot,
            expires_at=expires_at,
        )

    async def complete_task(self, task_id: int) -> int:
        """Mark task as completed. Returns rowcount."""
        result = await self.session.execute(
            update(CampaignTask)
            .where(
                CampaignTask.id == task_id,
                CampaignTask.status == "active",
            )
            .values(status="completed", completed_at=utc_now())
        )
        return result.rowcount

    async def expire_overdue_tasks(self) -> int:
        """Bulk-expire tasks past their TTL. Returns count of expired tasks."""
        now = utc_now()
        result = await self.session.execute(
            update(CampaignTask)
            .where(
                CampaignTask.status == "active",
                CampaignTask.expires_at.isnot(None),
                CampaignTask.expires_at <= now,
            )
            .values(status="expired")
        )
        return result.rowcount

    async def cancel_by_campaign(self, campaign_id: int) -> int:
        """Cancel all live tasks for a campaign. Returns count."""
        result = await self.session.execute(
            update(CampaignTask)
            .where(
                CampaignTask.campaign_id == campaign_id,
                CampaignTask.status.in_(["active", "pending_restriction"]),
            )
            .values(status="cancelled")
        )
        return result.rowcount

    async def count_active_by_campaign(self, campaign_id: int) -> int:
        """Count live tasks for a campaign."""
        result = await self.session.execute(
            select(func.count(CampaignTask.id))
            .where(
                CampaignTask.campaign_id == campaign_id,
                CampaignTask.status.in_(["active", "pending_restriction"]),
            )
        )
        return result.scalar_one()

    async def count_completed_by_campaign(self, campaign_id: int) -> int:
        """Count completed tasks for a campaign."""
        result = await self.session.execute(
            select(func.count(CampaignTask.id))
            .where(
                CampaignTask.campaign_id == campaign_id,
                CampaignTask.status == "completed",
            )
        )
        return result.scalar_one()

    # Aliases used by campaign_service
    cancel_active_tasks_for_campaign = cancel_by_campaign
    count_active_for_campaign = count_active_by_campaign

    async def get_affected_pairs_for_campaign(
        self, campaign_id: int
    ) -> list[tuple[int, int]]:
        """
        Get (group_id, user_telegram_id) pairs for active tasks.
        Used for unrestrict after campaign cancel/complete.
        """
        result = await self.session.execute(
            select(
                CampaignTask.group_id,
                CampaignTask.user_telegram_id,
            )
            .where(
                CampaignTask.campaign_id == campaign_id,
                CampaignTask.status.in_(["active", "pending_restriction"]),
            )
            .distinct()
        )
        return list(result.all())

    async def cancel_excess_active_tasks(
        self, campaign_id: int, excess: int
    ) -> int:
        """
        Cancel N oldest live tasks for a campaign (when target decreases).
        Returns actual cancelled count.
        """
        # Get IDs of oldest live tasks to cancel
        result = await self.session.execute(
            select(CampaignTask.id)
            .where(
                CampaignTask.campaign_id == campaign_id,
                CampaignTask.status.in_(["active", "pending_restriction"]),
            )
            .order_by(CampaignTask.created_at)
            .limit(excess)
        )
        ids_to_cancel = [row[0] for row in result.all()]
        if not ids_to_cancel:
            return 0

        result = await self.session.execute(
            update(CampaignTask)
            .where(CampaignTask.id.in_(ids_to_cancel))
            .values(status="cancelled")
        )
        return result.rowcount

    async def count_active_for_group_user(
        self, group_id: int, user_telegram_id: int
    ) -> int:
        """Count active tasks for a specific group+user pair."""
        result = await self.session.execute(
            select(func.count(CampaignTask.id))
            .where(
                CampaignTask.group_id == group_id,
                CampaignTask.user_telegram_id == user_telegram_id,
                CampaignTask.status.in_(["active", "pending_restriction"]),
            )
        )
        return result.scalar_one()

    async def get_by_campaign_and_status(
        self, campaign_id: int, status: str
    ) -> Sequence[CampaignTask]:
        """Get tasks for a campaign filtered by status."""
        result = await self.session.execute(
            select(CampaignTask)
            .where(
                CampaignTask.campaign_id == campaign_id,
                CampaignTask.status == status,
            )
            .order_by(CampaignTask.created_at.desc())
        )
        return result.scalars().all()
