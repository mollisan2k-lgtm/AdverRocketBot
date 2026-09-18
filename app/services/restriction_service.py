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
