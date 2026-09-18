"""System error repository (🚨 Urgent Problems panel)."""

from __future__ import annotations

from typing import Sequence

from sqlalchemy import select, update, func

from app.db.models import SystemError_
from app.repositories.base import BaseRepository
from app.utils.time_utils import utc_now


class SystemErrorRepository(BaseRepository[SystemError_]):
    model = SystemError_

    async def log_error(
        self,
        error_type: str,
        message: str,
        severity: str = "normal",
        details_json: str | None = None,
    ) -> SystemError_:
        """Record a system error."""
        return await self.create(
            error_type=error_type,
            message=message,
            severity=severity,
            details_json=details_json,
        )

    async def get_unresolved(self) -> Sequence[SystemError_]:
        """Get all unresolved errors (admin dashboard)."""
        result = await self.session.execute(
            select(SystemError_)
            .where(SystemError_.resolved == False)
            .order_by(
                # critical first, then by time
                SystemError_.severity.desc(),
                SystemError_.created_at.desc(),
            )
        )
        return result.scalars().all()

    async def get_critical_unresolved(self) -> Sequence[SystemError_]:
        """Get unresolved critical errors."""
        result = await self.session.execute(
            select(SystemError_)
            .where(
                SystemError_.resolved == False,
                SystemError_.severity == "critical",
            )
            .order_by(SystemError_.created_at.desc())
        )
        return result.scalars().all()

    async def resolve(self, error_id: int) -> int:
        """Mark error as resolved. Returns rowcount."""
        result = await self.session.execute(
            update(SystemError_)
            .where(SystemError_.id == error_id, SystemError_.resolved == False)
            .values(resolved=True, resolved_at=utc_now())
        )
        return result.rowcount

    async def count_unresolved(self) -> int:
        """Count unresolved errors (badge counter)."""
        result = await self.session.execute(
            select(func.count(SystemError_.id))
            .where(SystemError_.resolved == False)
        )
        return result.scalar_one()

    async def count_critical(self) -> int:
        """Count unresolved critical errors."""
        result = await self.session.execute(
            select(func.count(SystemError_.id))
            .where(
                SystemError_.resolved == False,
                SystemError_.severity == "critical",
            )
        )
        return result.scalar_one()
