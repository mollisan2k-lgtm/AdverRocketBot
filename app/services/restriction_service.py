"""
Restriction service — handles background reconciliation of desired/actual states.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.restriction_repo import RestrictionRepository
from app.integrations.telegram_api import TelegramAPIService

logger = logging.getLogger(__name__)


class RestrictionService:
    """Service to reconcile user restrictions with Telegram."""

    def __init__(self, session: AsyncSession, telegram_api: TelegramAPIService):
        self.session = session
        self.repo = RestrictionRepository(session)
        self.telegram_api = telegram_api

    async def apply_restriction_for_task(
        self, task_id: int, original_permissions: dict | None = None
    ) -> dict:
        """
        Synchronously apply restriction after a task is assigned.
        Atomic transition:
        - If Telegram API fails -> cancel task.
        - If succeeds -> status='active' and actual_state='ON'.
        """
        from app.repositories.task_repo import TaskRepository
        from app.repositories.group_repo import GroupRepository
        from app.db.engine import run_atomic
        from sqlalchemy import update
        from app.db.models import CampaignTask

        task_repo = TaskRepository(self.session)
        task = await task_repo.get_by_id(task_id)

        if not task or task.status != "pending_restriction":
            return {"ok": False, "error": "Invalid task state"}

        group_repo = GroupRepository(self.session)
        group = await group_repo.get_by_id(task.group_id)

        if not group or not group.bot_has_rights:
            async def _cancel(update_session: AsyncSession):
                await update_session.execute(
                    update(CampaignTask)
                    .where(CampaignTask.id == task_id)
                    .values(status="cancelled")
                )
            await run_atomic(_cancel)
            return {"ok": False, "error": "Bot lost admin rights"}

        # Perform Telegram API call outside transaction
        chat_id = group.telegram_chat_id
        user_id = task.user_telegram_id
        success = await self.telegram_api.restrict_member(chat_id, user_id)

        # Atomic transition based on result
        async def _transition(update_session: AsyncSession):
            from app.repositories.restriction_repo import RestrictionRepository
            from app.services.notification_service import NotificationService
            from app.repositories.settings_repo import TextRepository
            from app.services.campaign_service import CampaignService
            from app.utils.decimal_utils import from_db

            rr = RestrictionRepository(update_session)
            notif_service = NotificationService(update_session)
            text_repo = TextRepository(update_session)
            camp_service = CampaignService(update_session)

            if success:
                await update_session.execute(
                    update(CampaignTask)
                    .where(CampaignTask.id == task_id, CampaignTask.status == "pending_restriction")
                    .values(status="active")
                )
                await rr.add_restriction(group.id, user_id, original_permissions)
                record = await rr.get_record(group.id, user_id)
                if record:
                    record.actual_state = "ON"
                
                # Send task_assigned notification
                campaign = await camp_service.campaign_repo.get_by_id(task.campaign_id)
                if campaign:
                    tpl = await text_repo.get_text("task_assigned")
                    text = tpl.format(
                        target_title=campaign.target_title,
                        reward=from_db(campaign.seller_payout_snapshot),
                        group_title=group.title,
                    )
                    await notif_service.schedule_notification(user_id, text)
            else:
                await update_session.execute(
                    update(CampaignTask)
                    .where(CampaignTask.id == task_id, CampaignTask.status == "pending_restriction")
                    .values(status="cancelled")
                )

        await run_atomic(_transition)
        return {"ok": success}

    async def finalize_reconciliation(self, restriction_id: int, claim_token: str, expected_gen: int, success: bool, current_op_token: int) -> dict:
        """
        Phase 3: Finalize restriction reconciliation.
        Checks generation, claim_token, and operation_token (to ensure desired_state didn't change while HTTP was running).
        """
        from sqlalchemy import update
        r = await self.repo.get_by_id(restriction_id)
        if not r:
            return {"ok": False, "error": "Not found"}
            
        if r.claim_token != claim_token or r.generation != expected_gen:
            return {"ok": False, "error": "Fenced out (generation/token mismatch)"}
            
        if r.operation_token != current_op_token:
            # The desired_state changed during our HTTP call!
            # We must NOT update actual_state to desired_state, because we applied an OLD desired_state.
            # Instead, just release the lease and let the next loop iteration handle the new state.
            await self.session.execute(
                update(self.repo.model)
                .where(self.repo.model.id == restriction_id)
                .values(lease_expires_at=None, generation=self.repo.model.generation + 1)
            )
            return {"ok": False, "error": "Operation token mismatch (desired state changed during HTTP)"}
            
        if success:
            await self.session.execute(
                update(self.repo.model)
                .where(
                    self.repo.model.id == restriction_id,
                    self.repo.model.claim_token == claim_token,
                    self.repo.model.generation == expected_gen
                )
                .values(
                    actual_state=r.desired_state,
                    lease_expires_at=None if r.desired_state == "OFF" else r.lease_expires_at,
                    generation=self.repo.model.generation + 1
                )
            )
            return {"ok": True}
        else:
            return {"ok": False, "error": "Telegram API failed"}
