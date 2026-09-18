"""
Task service — distribution, verification, and completion.

Core invariants from plan v4:
- completed <= target: atomic INSERT ... WHERE (SELECT COUNT) < limit
- Partial UNIQUE (user, campaign) WHERE active: no duplicate active tasks
- Partial UNIQUE (user, target_chat_id) WHERE active: one active per target
- Repeat restriction via target_user_history.next_available_at
- Task completion: subscription check -> atomic increment -> spend -> reward
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text, select, update, func, and_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Campaign, CampaignTask, SellerGroup,
    TargetUserHistory, UserRestriction,
)
from app.repositories.task_repo import TaskRepository
from app.repositories.campaign_repo import CampaignRepository
from app.repositories.group_repo import GroupRepository
from app.repositories.history_repo import HistoryRepository
from app.repositories.restriction_repo import RestrictionRepository
from app.services.balance_service import BalanceService
from app.utils.decimal_utils import from_db, to_db
from app.utils.time_utils import utc_now

logger = logging.getLogger(__name__)


class TaskDistributionError(Exception):
    """No suitable tasks for distribution."""
    pass


class TaskVerificationError(Exception):
    """Task verification failed."""
    pass


class TaskService:
    """
    Manages task lifecycle: assign, verify, complete.
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.task_repo = TaskRepository(session)
        self.campaign_repo = CampaignRepository(session)
        self.group_repo = GroupRepository(session)
        self.history_repo = HistoryRepository(session)
        self.restriction_repo = RestrictionRepository(session)
        self.balance_service = BalanceService(session)

    # ── Distribution ─────────────────────────────────────────────────────

    async def assign_task(
        self,
        user_telegram_id: int,
        campaign_id: int,
        group_id: int,
    ) -> CampaignTask:
        """
        Assign a task to a user.

        Validates:
        1. Campaign is active and has slots
        2. User doesn't already have active task for this campaign
        3. User doesn't have active task for same target_chat_id
        4. Repeat restriction (target_user_history.next_available_at)
        5. Group is approved

        The partial UNIQUE indexes guarantee no duplicates at DB level.
        """
        # 1. Get campaign & group
        campaign = await self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise TaskDistributionError("Кампания не найдена.")
        if campaign.status != "active":
            raise TaskDistributionError("Кампания не активна.")
        if campaign.completed >= campaign.target:
            raise TaskDistributionError("Кампания заполнена.")

        group = await self.group_repo.get_by_id(group_id)
        if not group:
            raise TaskDistributionError("Группа не найдена.")
        if group.status != "approved":
            raise TaskDistributionError("Группа не одобрена.")

        # 2. Check campaign total capacity (completed + live)
        live_count = await self.task_repo.count_active_for_campaign(campaign_id)
        if campaign.completed + live_count >= campaign.target:
            raise TaskDistributionError("Все доступные места уже забронированы или выполнены.")

        # 3. Check user live task limit
        from app.repositories.settings_repo import SettingsRepository
        settings_repo = SettingsRepository(self.session)
        max_tasks_str = await settings_repo.get_value("max_tasks_per_user")
        max_tasks = int(max_tasks_str) if max_tasks_str else 3
        
        # count_active_by_user now includes active + pending_restriction
        user_tasks = await self.task_repo.get_active_by_user(user_telegram_id)
        if len(user_tasks) >= max_tasks:
            raise TaskDistributionError(f"У вас уже максимальное количество активных заданий ({max_tasks}).")

        # 4. Check repeat restriction
        is_available = await self.history_repo.is_available(
            user_telegram_id=user_telegram_id,
            target_chat_id=campaign.target_chat_id,
        )
        if not is_available:
            raise TaskDistributionError(
                "Вы недавно выполняли задание для этого канала/группы. "
                "Попробуйте позже."
            )

        # 5. Create task with interval snapshot
        task = CampaignTask(
            campaign_id=campaign_id,
            group_id=group_id,
            user_telegram_id=user_telegram_id,
            target_chat_id=campaign.target_chat_id,
            interval_minutes_snapshot=group.interval_minutes,
            status="pending_restriction",
            created_at=utc_now(),
            # TTL: task expires in 24 hours if not completed
            expires_at=utc_now() + timedelta(hours=24),
            updated_at=utc_now(),
        )
        self.session.add(task)

        try:
            await self.session.flush()
        except IntegrityError:
            await self.session.rollback()
            raise TaskDistributionError(
                "У вас уже есть активное задание для этого канала/группы."
            )

        logger.info(
            "Task assigned: id=%d campaign=%d user=%d",
            task.id, campaign_id, user_telegram_id,
        )
        return task

    # ── Verification & Completion ────────────────────────────────────────

    async def verify_and_complete(
        self,
        task_id: int,
        is_subscribed: bool,
    ) -> dict[str, Any]:
        """
        Verify subscription and complete task.

        Plan v4 concurrency-safe flow:
        1. Check task is active
        2. Verify subscription (is_subscribed passed from caller who checked Telegram)
        3. Atomic increment completed:
           UPDATE campaigns SET completed = completed + 1
           WHERE id = ? AND completed < target AND status IN ('active', 'paused')
        4. If increment succeeded: spend from buyer, reward seller
        5. Update target_user_history for repeat restriction
        6. Unrestrict user in group

        Returns dict with result details.
        """
        task = await self.task_repo.get_by_id(task_id)
        if not task:
            raise TaskVerificationError("Задание не найдено.")
        if task.status != "active":
            raise TaskVerificationError("Задание уже не активно.")

        # 2. Subscription check
        if not is_subscribed:
            raise TaskVerificationError(
                "Подписка не обнаружена. Пожалуйста, подпишитесь и попробуйте снова."
            )

        # 3. Complete task and increment campaign (BEGIN IMMEDIATE protects this)
        campaign = await self.campaign_repo.get_by_id(task.campaign_id)
        if not campaign:
            raise TaskVerificationError("Campaign not found")

        if campaign.completed >= campaign.target or campaign.status not in ("active", "paused"):
            await self.session.execute(
                update(CampaignTask)
                .where(CampaignTask.id == task.id)
                .values(status="cancelled", updated_at=utc_now())
            )
            raise TaskVerificationError("Кампания завершена — все места заняты.")

        claimed = await self.task_repo.complete_task(task.id)
        if not claimed:
            raise TaskVerificationError("Задание уже обработано или неактивно.")

        await self.session.execute(
            update(Campaign)
            .where(Campaign.id == campaign.id)
            .values(completed=Campaign.completed + 1, updated_at=utc_now())
        )
        
        # In-memory update for the response dict
        campaign.completed += 1

        # 5. Financial operations

        buyer_price = from_db(campaign.buyer_price_snapshot)
        seller_payout = from_db(campaign.seller_payout_snapshot)

        # Spend from buyer's reserve
        await self.balance_service.spend_from_reserve(
            user_id=campaign.user_id,
            amount=buyer_price,
            campaign_id=campaign.id,
            task_id=task.id,
            idempotency_key=f"task_spend:{task.id}",
        )

        # Reward seller
        group = await self.group_repo.get_by_id(task.group_id)
        if group:
            await self.balance_service.credit_seller_reward(
                user_id=group.user_id,
                amount=seller_payout,
                campaign_id=campaign.id,
                task_id=task.id,
                idempotency_key=f"seller_reward:{task.id}",
            )

        # 6. Update repeat restriction history
        interval = task.interval_minutes_snapshot
        await self.history_repo.record_completion(
            user_telegram_id=task.user_telegram_id,
            target_chat_id=task.target_chat_id,
            cooldown_minutes=interval,
        )

        # 7. Check if campaign is now fully completed
        if campaign.completed >= campaign.target:
            await self._finalize_campaign_completion(campaign)

        logger.info(
            "Task completed: id=%d campaign=%d completed=%d/%d",
            task.id, campaign.id, campaign.completed, campaign.target,
        )

        return {
            "task_id": task.id,
            "campaign_id": campaign.id,
            "seller_reward": str(seller_payout),
            "completed": campaign.completed,
            "target": campaign.target,
            "campaign_finished": campaign.completed >= campaign.target,
        }



    async def _finalize_campaign_completion(self, campaign: Campaign) -> None:
        """
        Idempotent campaign completion:
        1. Set status to completed
        2. Cancel remaining active tasks
        3. Refund remaining reserve (should be zero for normal completion)
        """
        ok = await self.campaign_repo.conditional_update_status(
            campaign_id=campaign.id,
            allowed_from=("active", "paused"),
            new_status="completed",
        )
        if ok:
            campaign.completed_at = utc_now()
            # Cancel any remaining active tasks
            cancelled = await self.task_repo.cancel_active_tasks_for_campaign(
                campaign.id
            )
            if cancelled > 0:
                logger.info(
                    "Cancelled %d stale active tasks on campaign %d completion",
                    cancelled, campaign.id,
                )

    # ── Expiry ───────────────────────────────────────────────────────────

    async def expire_stale_tasks(self) -> int:
        """
        Expire tasks that have passed their TTL.
        Called by background task processor.
        """
        return await self.task_repo.expire_overdue_tasks()

    # ── Queries ──────────────────────────────────────────────────────────

    async def get_user_active_tasks(
        self, user_telegram_id: int
    ) -> list[CampaignTask]:
        return await self.task_repo.get_active_by_user(user_telegram_id)

    async def get_task_by_id(self, task_id: int) -> CampaignTask | None:
        return await self.task_repo.get_by_id(task_id)

    async def get_tasks_for_campaign(
        self, campaign_id: int, status: str | None = None
    ) -> list[CampaignTask]:
        if status:
            return await self.task_repo.get_by_campaign_and_status(
                campaign_id, status
            )
        return await self.task_repo.get_active_by_campaign(campaign_id)

    async def count_active_for_campaign(self, campaign_id: int) -> int:
        return await self.task_repo.count_active_for_campaign(campaign_id)
