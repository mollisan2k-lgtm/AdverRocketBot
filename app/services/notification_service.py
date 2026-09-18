"""Notification service."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from app.repositories.notification_repo import NotificationRepository


class NotificationService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = NotificationRepository(session)

    async def schedule_notification(
        self,
        telegram_id: int,
        text: str,
        parse_mode: str = "HTML",
        reply_markup_json: str | None = None,
        kind: str | None = None,
        dedupe_key: str | None = None
    ) -> None:
        """
        Schedule a notification to be sent asynchronously.
        This writes to the outbox table in the current database transaction.
        """
        await self.repo.add_notification(
            telegram_id=telegram_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup_json=reply_markup_json,
            kind=kind,
            dedupe_key=dedupe_key
        )
