"""Audit log repository."""

from __future__ import annotations

import json
from typing import Sequence

from sqlalchemy import select, func

from app.db.models import AuditLog
from app.repositories.base import BaseRepository


class AuditLogRepository(BaseRepository[AuditLog]):
    model = AuditLog

    async def log_action(
        self,
        actor_telegram_id: int,
        action: str,
        object_type: str | None = None,
        object_id: int | None = None,
        old_value: dict | None = None,
        new_value: dict | None = None,
        reason: str | None = None,
    ) -> AuditLog:
        """Record an admin/system action."""
        return await self.create(
            actor_telegram_id=actor_telegram_id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            old_value_json=json.dumps(old_value, ensure_ascii=False) if old_value else None,
            new_value_json=json.dumps(new_value, ensure_ascii=False) if new_value else None,
            reason=reason,
        )

    async def get_by_object(
        self, object_type: str, object_id: int
    ) -> Sequence[AuditLog]:
        """Get audit trail for a specific entity."""
        result = await self.session.execute(
            select(AuditLog)
            .where(
                AuditLog.object_type == object_type,
                AuditLog.object_id == object_id,
            )
            .order_by(AuditLog.created_at.desc())
        )
        return result.scalars().all()

    async def get_by_actor(
        self, actor_telegram_id: int, offset: int = 0, limit: int = 50
    ) -> Sequence[AuditLog]:
        """Get actions by a specific admin."""
        result = await self.session.execute(
            select(AuditLog)
            .where(AuditLog.actor_telegram_id == actor_telegram_id)
            .order_by(AuditLog.created_at.desc())
            .offset(offset).limit(limit)
        )
        return result.scalars().all()

    async def get_recent(self, limit: int = 100) -> Sequence[AuditLog]:
        """Get most recent audit entries."""
        result = await self.session.execute(
            select(AuditLog)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        return result.scalars().all()
