"""Notification repository."""

from __future__ import annotations

from typing import Sequence
from sqlalchemy import select
from app.db.models import NotificationOutbox
from app.repositories.base import BaseRepository
from app.utils.time_utils import utc_now


class NotificationRepository(BaseRepository[NotificationOutbox]):
    model = NotificationOutbox

    async def add_notification(self, telegram_id: int, text: str, parse_mode: str = "HTML") -> NotificationOutbox:
        """Add a notification to the outbox."""
        return await self.create(
            telegram_id=telegram_id,
            text=text,
            parse_mode=parse_mode,
            status="pending"
        )

    async def get_pending(self, limit: int = 50) -> Sequence[NotificationOutbox]:
        """Get pending notifications."""
        result = await self.session.execute(
            select(NotificationOutbox)
            .where(NotificationOutbox.status == "pending")
            .order_by(NotificationOutbox.created_at)
            .limit(limit)
        )
        return result.scalars().all()

    async def mark_sent(self, notification_id: int) -> None:
        """Mark notification as sent."""
        notification = await self.get_by_id(notification_id)
        if notification:
            notification.status = "sent"
            notification.processed_at = utc_now()
            await self.session.flush()

    async def mark_error(self, notification_id: int, error_details: str) -> None:
        """Mark notification as failed."""
        notification = await self.get_by_id(notification_id)
        if notification:
            notification.status = "error"
            notification.error_details = error_details
            notification.processed_at = utc_now()
            await self.session.flush()
