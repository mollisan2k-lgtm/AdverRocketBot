"""Target user history repository (v4 repeat restrictions)."""

from __future__ import annotations

from sqlalchemy import select, update

from app.db.models import TargetUserHistory
from app.repositories.base import BaseRepository
from app.utils.time_utils import utc_now, minutes_from_now


class HistoryRepository(BaseRepository[TargetUserHistory]):
    model = TargetUserHistory

    async def get_record(
        self, user_telegram_id: int, target_chat_id: int
    ) -> TargetUserHistory | None:
        """Get history record for (user, target) pair."""
        result = await self.session.execute(
            select(TargetUserHistory)
            .where(
                TargetUserHistory.user_telegram_id == user_telegram_id,
                TargetUserHistory.target_chat_id == target_chat_id,
            )
        )
        return result.scalar_one_or_none()

    async def is_available(
        self, user_telegram_id: int, target_chat_id: int
    ) -> bool:
        """
        Check if user can take a task for this target.
        Available if no record exists OR next_available_at is in the past.
        """
        record = await self.get_record(user_telegram_id, target_chat_id)
        if record is None:
            return True
        if record.next_available_at is None:
            return True
        return record.next_available_at <= utc_now()

    async def record_completion(
        self,
        user_telegram_id: int,
        target_chat_id: int,
        cooldown_minutes: int,
    ) -> TargetUserHistory:
        """
        Record task completion and set next_available_at.
        Creates or updates the history record (upsert pattern).
        """
        record = await self.get_record(user_telegram_id, target_chat_id)
        now = utc_now()
        next_at = minutes_from_now(cooldown_minutes)

        if record is None:
            return await self.create(
                user_telegram_id=user_telegram_id,
                target_chat_id=target_chat_id,
                last_completed_at=now,
                next_available_at=next_at,
                completion_count=1,
            )

        record.last_completed_at = now
        record.next_available_at = next_at
        record.completion_count += 1
        await self.session.flush()
        return record

    async def get_completion_count(
        self, user_telegram_id: int, target_chat_id: int
    ) -> int:
        """Get how many times user completed tasks for this target."""
        record = await self.get_record(user_telegram_id, target_chat_id)
        return record.completion_count if record else 0
