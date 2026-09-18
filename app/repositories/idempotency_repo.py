"""Idempotency key repository."""

from __future__ import annotations

import json

from sqlalchemy import select

from app.db.models import IdempotencyKey
from app.repositories.base import BaseRepository


class IdempotencyRepository(BaseRepository[IdempotencyKey]):
    model = IdempotencyKey

    async def check_and_set(
        self,
        key: str,
        operation: str,
        result_json: str | None = None,
    ) -> tuple[bool, IdempotencyKey]:
        """
        Check if key exists. If not, create it.
        Returns (is_new, entry).
        is_new=True  → first time, proceed with operation.
        is_new=False → duplicate, return cached result.
        """
        existing = await self.get_by_key(key)
        if existing:
            return False, existing

        entry = await self.create(
            key=key,
            operation=operation,
            result_json=result_json,
        )
        return True, entry

    async def get_by_key(self, key: str) -> IdempotencyKey | None:
        """Find by idempotency key."""
        result = await self.session.execute(
            select(IdempotencyKey).where(IdempotencyKey.key == key)
        )
        return result.scalar_one_or_none()

    async def store_result(self, key: str, result_data: dict) -> int:
        """Store operation result after successful execution."""
        entry = await self.get_by_key(key)
        if entry:
            entry.result_json = json.dumps(result_data, ensure_ascii=False)
            await self.session.flush()
            return 1
        return 0

    def parse_result(self, entry: IdempotencyKey) -> dict | None:
        """Parse stored result JSON."""
        if entry.result_json is None:
            return None
        try:
            return json.loads(entry.result_json)
        except (json.JSONDecodeError, TypeError):
            return None
