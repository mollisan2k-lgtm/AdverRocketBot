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

    async def reconcile_record(self, restriction_id: int, worker_token: str) -> dict:
        """
        Reconcile a single restriction record if worker_token matches.
        Calls Telegram API to apply the desired state.
        """
        # We need get_by_id, but we inherit from BaseRepository so it exists.
        r = await self.repo.get_by_id(restriction_id)
        if not r:
            return {"ok": False, "error": "Not found"}
            
        if r.worker_token != worker_token:
            return {"ok": False, "error": "Worker token mismatch (fencing)"}
            
        if r.desired_state == r.actual_state:
            return {"ok": True, "message": "Already in desired state"}

        from app.repositories.group_repo import GroupRepository
        group_repo = GroupRepository(self.session)
        group = await group_repo.get_by_id(r.group_id)
        
        if not group:
            return {"ok": False, "error": "Group not found"}
            
        if not group.bot_has_rights:
             return {"ok": False, "error": "Bot lost admin rights"}
            
        chat_id = group.telegram_chat_id
        user_id = r.user_telegram_id
        
        target_state = r.desired_state
        success = False
        
        if target_state == "ON":
            success = await self.telegram_api.restrict_member(chat_id, user_id)
        elif target_state == "OFF":
            perms = None
            if r.original_permissions_json:
                try:
                    perms = json.loads(r.original_permissions_json)
                except Exception:
                    pass
            success = await self.telegram_api.unrestrict_member(chat_id, user_id, perms)

        if success:
            r.actual_state = target_state
            # When restriction is successfully lifted, we can clean up the record
            # but usually it's kept around. The system just leaves it as OFF.
            # However, if actual_state = OFF, it won't be returned by get_active_by_group.
            return {"ok": True}
        else:
            return {"ok": False, "error": "Telegram API failed"}
