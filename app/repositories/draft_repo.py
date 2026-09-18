"""Draft repository (campaign creation wizard state)."""

from __future__ import annotations

import json

from sqlalchemy import select, delete

from app.db.models import Draft
from app.repositories.base import BaseRepository
from app.utils.time_utils import utc_now


class DraftRepository(BaseRepository[Draft]):
    model = Draft

    async def get_active_by_user(
        self, user_id: int, draft_type: str = "campaign"
    ) -> Draft | None:
        """Get user's active (non-expired) draft."""
        now = utc_now()
        result = await self.session.execute(
            select(Draft)
            .where(
                Draft.user_id == user_id,
                Draft.draft_type == draft_type,
            )
            .order_by(Draft.updated_at.desc())
        )
        draft = result.scalar_one_or_none()
        if draft and draft.expires_at and draft.expires_at <= now:
            # Expired — delete and return None
            await self.delete_by_id(draft.id)
            return None
        return draft

    async def upsert(
        self,
        user_id: int,
        data: dict,
        draft_type: str = "campaign",
        ttl_minutes: int = 60,
    ) -> Draft:
        """Create or update user's draft."""
        from app.utils.time_utils import minutes_from_now

        existing = await self.get_active_by_user(user_id, draft_type)
        data_json = json.dumps(data, ensure_ascii=False)
        expires_at = minutes_from_now(ttl_minutes)

        if existing:
            existing.data_json = data_json
            existing.expires_at = expires_at
            await self.session.flush()
            return existing

        return await self.create(
            user_id=user_id,
            draft_type=draft_type,
            data_json=data_json,
            expires_at=expires_at,
        )

    def parse_data(self, draft: Draft) -> dict:
        """Parse draft JSON data."""
        try:
            return json.loads(draft.data_json)
        except (json.JSONDecodeError, TypeError):
            return {}

    async def delete_by_user(
        self, user_id: int, draft_type: str = "campaign"
    ) -> int:
        """Delete user's draft (after campaign created or cancelled)."""
        result = await self.session.execute(
            delete(Draft)
            .where(
                Draft.user_id == user_id,
                Draft.draft_type == draft_type,
            )
        )
        return result.rowcount

    async def cleanup_expired(self) -> int:
        """Delete all expired drafts. Returns count."""
        now = utc_now()
        result = await self.session.execute(
            delete(Draft)
            .where(
                Draft.expires_at.isnot(None),
                Draft.expires_at <= now,
            )
        )
        return result.rowcount
