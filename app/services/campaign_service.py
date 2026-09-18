"""
Campaign service — lifecycle management.

Core invariants from plan v4:
- Campaign transitions via conditional UPDATE ... WHERE status IN (...)
- completed never decrements
- completed <= target always holds
- Active tasks cancelled on campaign cancel/complete
- Reserve-or-nothing for campaign creation and target increase
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Campaign, CampaignTask, Category, Refund
from app.repositories.campaign_repo import CampaignRepository
from app.repositories.task_repo import TaskRepository
from app.repositories.refund_repo import RefundRepository
from app.repositories.settings_repo import SettingsRepository
from app.services.balance_service import BalanceService
from app.utils.decimal_utils import to_db, from_db, round_down, ZERO
from app.utils.time_utils import utc_now

logger = logging.getLogger(__name__)


class CampaignService:
    """
    Campaign lifecycle management.

    All state transitions use conditional UPDATEs to prevent race conditions.
    Financial operations are performed atomically within transactions.
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.campaign_repo = CampaignRepository(session)
        self.task_repo = TaskRepository(session)
        self.refund_repo = RefundRepository(session)
        self.balance_service = BalanceService(session)

    # ── Creation ─────────────────────────────────────────────────────────

    async def create_campaign(
        self,
        user_id: int,
        category_id: int,
        target: int,
        target_type: str,
        target_chat_id: int,
        target_username: str | None,
        target_title_snapshot: str,
        target_link: str,
    ) -> Campaign:
        """
        Create a campaign with atomic reserve.

        Steps:
        1. Fetch category (snapshot buyer_price, seller_payout)
        2. Calculate total cost = target * buyer_price
        3. Reserve funds atomically (available -> reserved)
        4. Create campaign row

        Raises InsufficientFundsError if not enough balance.
        """
        # 1. Snapshot category
        category = await self.session.get(Category, category_id)
        if not category or category.status != "active":
            raise ValueError("Категория недоступна.")

        buyer_price = from_db(category.buyer_price)
        seller_payout = from_db(category.seller_payout)
        total_cost = buyer_price * target

        # 2. Reserve (raises InsufficientFundsError)
        campaign = Campaign(
            user_id=user_id,
            category_id=category_id,
            target_type=target_type,
            target_link=target_link,
            target_chat_id=target_chat_id,
            target_username=target_username,
            target_title_snapshot=target_title_snapshot,
            target=target,
            completed=0,
            buyer_price_snapshot=to_db(buyer_price),
            seller_payout_snapshot=to_db(seller_payout),
            category_name_snapshot=category.name,
            status="active",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.session.add(campaign)
        await self.session.flush()  # get campaign.id

        # 3. Reserve funds
        await self.balance_service.reserve_for_campaign(
            user_id=user_id,
            amount=total_cost,
            campaign_id=campaign.id,
            idempotency_key=f"campaign_reserve:{campaign.id}",
        )

        logger.info(
            "Campaign created: id=%d user=%d target=%d cost=%s",
            campaign.id, user_id, target, total_cost,
        )
        return campaign

    # ── State transitions ────────────────────────────────────────────────

    async def pause_campaign(self, campaign_id: int, user_id: int | None = None) -> bool:
        """
        Pause: active -> paused.
        Active tasks remain and CAN be completed while paused.
        No new tasks will be distributed.
        """
        if user_id is not None:
            c = await self.campaign_repo.get_by_id(campaign_id)
            if not c or c.user_id != user_id:
                return False

        return await self.campaign_repo.conditional_update_status(
            campaign_id=campaign_id,
            allowed_from=("active",),
            new_status="paused",
        )

    async def resume_campaign(self, campaign_id: int, user_id: int | None = None) -> bool:
        """Resume: paused -> active."""
        if user_id is not None:
            c = await self.campaign_repo.get_by_id(campaign_id)
            if not c or c.user_id != user_id:
                return False

        return await self.campaign_repo.conditional_update_status(
            campaign_id=campaign_id,
            allowed_from=("paused",),
            new_status="active",
        )

    async def cancel_campaign(
        self,
        campaign_id: int,
        cancelled_by: str = "user",
        commission_percent: Decimal | None = None,
        user_id: int | None = None,
    ) -> dict[str, Any] | None:
        """
        Cancel: active/paused -> cancelled.

        Steps:
        1. Conditional UPDATE status
        2. Cancel all active tasks
        3. Calculate remaining reserve (target - completed) * buyer_price
        4. Apply commission, refund net to user

        Returns refund details or None if transition failed.
        """
        campaign = await self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            return None
            
        if user_id is not None and campaign.user_id != user_id:
            return None

        # 1. Transition
        ok = await self.campaign_repo.conditional_update_status(
            campaign_id=campaign_id,
            allowed_from=("active", "paused"),
            new_status="cancelled",
        )
        if not ok:
            return None
        if not campaign:
            return None

        # 2. Cancel active tasks
        cancelled_count = await self.task_repo.cancel_active_tasks_for_campaign(
            campaign_id
        )
        logger.info(
            "Cancelled %d active tasks for campaign %d",
            cancelled_count, campaign_id,
        )

        # 3-4. Refund remaining reserve
        remaining_slots = campaign.target - campaign.completed
        if remaining_slots <= 0:
            return {"refunded": "0.00", "commission": "0.00", "cancelled_tasks": cancelled_count}

        if commission_percent is None:
            settings_repo = SettingsRepository(self.session)
            val = await settings_repo.get_value("cancel_commission_percent")
            commission_percent = Decimal(val) if val else Decimal("10")

        buyer_price = from_db(campaign.buyer_price_snapshot)
        gross = buyer_price * remaining_slots
        commission = round_down(gross * commission_percent / Decimal("100"))
        net = gross - commission

        refund_type = "user_cancel" if cancelled_by == "user" else "admin_force_complete"

        # Record refund
        refund = Refund(
            user_id=campaign.user_id,
            campaign_id=campaign_id,
            refund_type=refund_type,
            gross_amount=to_db(gross),
            commission_percent=to_db(commission_percent),
            commission_amount=to_db(commission),
            net_amount=to_db(net),
            idempotency_key=f"refund:{refund_type}:{campaign_id}",
            created_at=utc_now(),
        )
        self.session.add(refund)

        # Refund balance
        await self.balance_service.refund_with_commission(
            user_id=campaign.user_id,
            gross_amount=gross,
            commission_amount=commission,
            campaign_id=campaign_id,
            idempotency_key=f"refund_balance:{refund_type}:{campaign_id}",
        )

        logger.info(
            "Campaign %d cancelled: refund=%s commission=%s",
            campaign_id, net, commission,
        )

        return {
            "refunded": str(net),
            "commission": str(commission),
            "cancelled_tasks": cancelled_count,
        }

    async def admin_force_complete(
        self,
        campaign_id: int,
        commission_percent: Decimal | None = None,
    ) -> dict[str, Any] | None:
        """
        Admin force-complete: active/paused -> completed.
        Cancel active tasks + refund remaining with commission.
        """
        ok = await self.campaign_repo.conditional_update_status(
            campaign_id=campaign_id,
            allowed_from=("active", "paused"),
            new_status="completed",
        )
        if not ok:
            return None

        campaign = await self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            return None

        campaign.completed_at = utc_now()

        # Cancel active tasks
        cancelled_count = await self.task_repo.cancel_active_tasks_for_campaign(
            campaign_id
        )

        # Refund remaining
        remaining_slots = campaign.target - campaign.completed
        if remaining_slots <= 0:
            return {"refunded": "0.00", "commission": "0.00", "cancelled_tasks": cancelled_count}

        if commission_percent is None:
            settings_repo = SettingsRepository(self.session)
            val = await settings_repo.get_value("cancel_commission_percent")
            commission_percent = Decimal(val) if val else Decimal("10")

        buyer_price = from_db(campaign.buyer_price_snapshot)
        gross = buyer_price * remaining_slots
        commission = round_down(gross * commission_percent / Decimal("100"))
        net = gross - commission

        refund = Refund(
            user_id=campaign.user_id,
            campaign_id=campaign_id,
            refund_type="admin_force_complete",
            gross_amount=to_db(gross),
            commission_percent=to_db(commission_percent),
            commission_amount=to_db(commission),
            net_amount=to_db(net),
            idempotency_key=f"refund:force_complete:{campaign_id}",
            created_at=utc_now(),
        )
        self.session.add(refund)

        await self.balance_service.refund_with_commission(
            user_id=campaign.user_id,
            gross_amount=gross,
            commission_amount=commission,
            campaign_id=campaign_id,
            idempotency_key=f"refund_balance:force_complete:{campaign_id}",
        )

        return {
            "refunded": str(net),
            "commission": str(commission),
            "cancelled_tasks": cancelled_count,
        }

    # ── Target management ────────────────────────────────────────────────

    async def increase_target(
        self, campaign_id: int, additional: int
    ) -> bool:
        """
        Increase campaign target (active/paused).

        Uses buyer_price_snapshot for new reserve calculation.
        Atomic: reserve-or-nothing.
        """
        campaign = await self.campaign_repo.get_by_id(campaign_id)
        if not campaign or campaign.status not in ("active", "paused"):
            return False

        buyer_price = from_db(campaign.buyer_price_snapshot)
        additional_cost = buyer_price * additional

        # Reserve additional funds (raises InsufficientFundsError)
        await self.balance_service.reserve_for_campaign(
            user_id=campaign.user_id,
            amount=additional_cost,
            campaign_id=campaign_id,
            idempotency_key=f"campaign_increase:{campaign_id}:{campaign.target + additional}",
        )

        # Update target atomically
        await self.session.execute(
            update(Campaign)
            .where(Campaign.id == campaign_id, Campaign.status.in_(("active", "paused")))
            .values(target=Campaign.target + additional, updated_at=utc_now())
        )

        logger.info(
            "Campaign %d target increased by %d (new=%d)",
            campaign_id, additional, campaign.target,
        )
        return True

    async def decrease_target(
        self,
        campaign_id: int,
        new_target: int,
        commission_percent: Decimal | None = None,
        user_id: int | None = None,
    ) -> dict[str, Any] | None:
        """
        Decrease campaign target (active/paused).

        new_target must be >= completed.
        Refund the difference with commission.
        Cancel any excess active tasks.
        """
        campaign = await self.campaign_repo.get_by_id(campaign_id)
        if not campaign or campaign.status not in ("active", "paused"):
            return None
            
        if user_id is not None and campaign.user_id != user_id:
            return None

        if new_target < campaign.completed:
            raise ValueError(
                f"Новый таргет ({new_target}) не может быть меньше "
                f"выполненных ({campaign.completed})."
            )

        if new_target >= campaign.target:
            raise ValueError("Новый таргет должен быть меньше текущего.")

        old_target = campaign.target
        slots_removed = old_target - new_target

        # Check if campaign is now complete
        if new_target == campaign.completed:
            return await self._finalize_completion_on_decrease(
                campaign, new_target, slots_removed, commission_percent
            )

        if commission_percent is None:
            settings_repo = SettingsRepository(self.session)
            val = await settings_repo.get_value("cancel_commission_percent")
            commission_percent = Decimal(val) if val else Decimal("10")

        buyer_price = from_db(campaign.buyer_price_snapshot)
        gross = buyer_price * slots_removed
        commission = round_down(gross * commission_percent / Decimal("100"))
        net = gross - commission

        # Refund
        refund = Refund(
            user_id=campaign.user_id,
            campaign_id=campaign_id,
            refund_type="decrease_target",
            gross_amount=to_db(gross),
            commission_percent=to_db(commission_percent),
            commission_amount=to_db(commission),
            net_amount=to_db(net),
            idempotency_key=f"refund:decrease:{campaign_id}:{new_target}",
            created_at=utc_now(),
        )
        self.session.add(refund)

        await self.balance_service.refund_with_commission(
            user_id=campaign.user_id,
            gross_amount=gross,
            commission_amount=commission,
            campaign_id=campaign_id,
            idempotency_key=f"refund_balance:decrease:{campaign_id}:{new_target}",
        )

        # Update target atomically
        await self.session.execute(
            update(Campaign)
            .where(Campaign.id == campaign_id, Campaign.status.in_(("active", "paused")))
            .values(target=new_target, updated_at=utc_now())
        )

        # Cancel excess active tasks if any
        active_count = await self.task_repo.count_active_for_campaign(campaign_id)
        available_slots = new_target - campaign.completed
        if active_count > available_slots:
            excess = active_count - available_slots
            await self.task_repo.cancel_excess_active_tasks(campaign_id, excess)

        return {"refunded": str(net), "commission": str(commission)}

    async def _finalize_completion_on_decrease(
        self,
        campaign: Campaign,
        new_target: int,
        slots_removed: int,
        commission_percent: Decimal | None,
    ) -> dict[str, Any]:
        """When decrease makes target == completed, finalize campaign."""
        if commission_percent is None:
            settings_repo = SettingsRepository(self.session)
            val = await settings_repo.get_value("cancel_commission_percent")
            commission_percent = Decimal(val) if val else Decimal("10")
            
        buyer_price = from_db(campaign.buyer_price_snapshot)
        gross = buyer_price * slots_removed
        commission = round_down(gross * commission_percent / Decimal("100"))
        net = gross - commission

        refund = Refund(
            user_id=campaign.user_id,
            campaign_id=campaign.id,
            refund_type="decrease_target",
            gross_amount=to_db(gross),
            commission_percent=to_db(commission_percent),
            commission_amount=to_db(commission),
            net_amount=to_db(net),
            idempotency_key=f"refund:decrease:{campaign.id}:{new_target}",
            created_at=utc_now(),
        )
        self.session.add(refund)

        await self.balance_service.refund_with_commission(
            user_id=campaign.user_id,
            gross_amount=gross,
            commission_amount=commission,
            campaign_id=campaign.id,
            idempotency_key=f"refund_balance:decrease:{campaign.id}:{new_target}",
        )

        ok = await self.campaign_repo.conditional_update_status(
            campaign_id=campaign.id,
            allowed_from=("active", "paused"),
            new_status="completed",
            target=new_target,
            updated_at=utc_now()
        )
        if not ok:
            return {"refunded": "0.00", "commission": "0.00", "completed": False}

        # Cancel all remaining active tasks
        await self.task_repo.cancel_active_tasks_for_campaign(campaign.id)

        return {"refunded": str(net), "commission": str(commission), "completed": True}

    # ── Queries ──────────────────────────────────────────────────────────

    async def get_by_id(self, campaign_id: int) -> Campaign | None:
        return await self.campaign_repo.get_by_id(campaign_id)

    async def get_user_campaigns(
        self, user_id: int, status: str | None = None
    ) -> list[Campaign]:
        return await self.campaign_repo.get_by_user(user_id, status)

    async def get_active_campaigns_for_category(
        self, category_id: int
    ) -> list[Campaign]:
        """Get active campaigns in a category (for task distribution)."""
        return await self.campaign_repo.get_active_for_category(category_id)

    async def get_distributable_campaigns(
        self, category_id: int
    ) -> list[Campaign]:
        """
        Get campaigns that have available slots (completed < target)
        and are in 'active' status.
        """
        return await self.campaign_repo.get_distributable(category_id)
