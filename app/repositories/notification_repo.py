"""Notification repository."""

from __future__ import annotations

from typing import Sequence
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from app.db.models import NotificationOutbox
from app.repositories.base import BaseRepository
from app.utils.time_utils import utc_now, minutes_from_now


class NotificationRepository(BaseRepository[NotificationOutbox]):
    model = NotificationOutbox

    async def add_notification(self, telegram_id: int, text: str, parse_mode: str = "HTML", reply_markup_json: str | None = None, kind: str | None = None, dedupe_key: str | None = None) -> NotificationOutbox | None:
        """Add a notification to the outbox. Returns None if dedupe_key exists."""
        try:
            return await self.create(
                telegram_id=telegram_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup_json=reply_markup_json,
                kind=kind,
                dedupe_key=dedupe_key,
                status="pending"
            )
        except IntegrityError:
            # Dedupe key violation
            return None

    async def claim_for_sending(self, worker_id: str, claim_token: str, limit: int = 50) -> Sequence[NotificationOutbox]:
        """Claim notifications for processing."""
        now = utc_now()
        
        # 1. Find claimable notifications
        result = await self.session.execute(
            select(NotificationOutbox)
            .where(
                (NotificationOutbox.status == "pending") &
                ((NotificationOutbox.next_attempt_at.is_(None)) | (NotificationOutbox.next_attempt_at <= now)) &
                ((NotificationOutbox.lease_expires_at.is_(None)) | (NotificationOutbox.lease_expires_at <= now))
            )
            .order_by(NotificationOutbox.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        notifications = result.scalars().all()
        if not notifications:
            return []
            
        ids = [n.id for n in notifications]
        lease_expiry = minutes_from_now(5)
        
        # 2. Update them
        await self.session.execute(
            update(NotificationOutbox)
            .where(NotificationOutbox.id.in_(ids))
            .values(
                status="processing",
                worker_id=worker_id,
                claim_token=claim_token,
                lease_expires_at=lease_expiry,
                generation=NotificationOutbox.generation + 1
            )
        )
        await self.session.flush()
        
        # 3. Return claimed
        res = await self.session.execute(
            select(NotificationOutbox)
            .where(NotificationOutbox.id.in_(ids))
        )
        return res.scalars().all()

    async def mark_sent(self, notification_id: int, expected_generation: int, claim_token: str) -> bool:
        """Mark notification as sent."""
        res = await self.session.execute(
            update(NotificationOutbox)
            .where(
                NotificationOutbox.id == notification_id,
                NotificationOutbox.status == "processing",
                NotificationOutbox.claim_token == claim_token,
                NotificationOutbox.generation == expected_generation
            )
            .values(
                status="sent",
                sent_at=utc_now(),
                generation=NotificationOutbox.generation + 1
            )
        )
        return res.rowcount > 0

    async def mark_error(self, notification_id: int, expected_generation: int, claim_token: str, error_details: str, is_permanent: bool = False, current_attempts: int = 0) -> bool:
        """Mark notification as failed, setup retry."""
        attempts = current_attempts + 1
        if is_permanent or attempts >= 5:
            new_status = "dead"
            next_attempt = None
        else:
            new_status = "pending"
            next_attempt = minutes_from_now(2 ** attempts)

        res = await self.session.execute(
            update(NotificationOutbox)
            .where(
                NotificationOutbox.id == notification_id,
                NotificationOutbox.status == "processing",
                NotificationOutbox.claim_token == claim_token,
                NotificationOutbox.generation == expected_generation
            )
            .values(
                status=new_status,
                attempts=attempts,
                next_attempt_at=next_attempt,
                last_error=error_details,
                generation=NotificationOutbox.generation + 1
            )
        )
        return res.rowcount > 0
