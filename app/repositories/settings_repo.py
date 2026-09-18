"""Bot settings & texts repository (admin-editable config)."""

from __future__ import annotations

from typing import Sequence

from sqlalchemy import select

from app.db.models import BotSetting, BotText
from app.repositories.base import BaseRepository


class SettingsRepository(BaseRepository[BotSetting]):
    model = BotSetting

    async def get_value(self, key: str, default: str | None = None) -> str | None:
        """Get setting value by key."""
        result = await self.session.execute(
            select(BotSetting).where(BotSetting.key == key)
        )
        setting = result.scalar_one_or_none()
        return setting.value if setting else default

    async def set_value(self, key: str, value: str, description: str | None = None) -> BotSetting:
        """Set or create a setting."""
        result = await self.session.execute(
            select(BotSetting).where(BotSetting.key == key)
        )
        setting = result.scalar_one_or_none()

        if setting:
            setting.value = value
            if description is not None:
                setting.description = description
            await self.session.flush()
            return setting

        return await self.create(
            key=key,
            value=value,
            description=description,
        )

    async def get_all_settings(self) -> Sequence[BotSetting]:
        """Get all settings (admin panel)."""
        result = await self.session.execute(
            select(BotSetting).order_by(BotSetting.key)
        )
        return result.scalars().all()

    async def get_int(self, key: str, default: int = 0) -> int:
        """Get setting as integer."""
        val = await self.get_value(key)
        if val is None:
            return default
        try:
            return int(val)
        except ValueError:
            return default

    async def get_decimal(self, key: str, default: str = "0.00") -> str:
        """Get setting as decimal string."""
        val = await self.get_value(key)
        return val if val is not None else default


class TextRepository(BaseRepository[BotText]):
    model = BotText

    async def get_text(self, key: str) -> str:
        """Get editable text by key. Returns default_text if not found."""
        result = await self.session.execute(
            select(BotText).where(BotText.key == key)
        )
        entry = result.scalar_one_or_none()
        return entry.text if entry else f"[{key}]"

    async def set_text(self, key: str, text: str) -> BotText:
        """Update text content."""
        result = await self.session.execute(
            select(BotText).where(BotText.key == key)
        )
        entry = result.scalar_one_or_none()
        if entry:
            entry.text = text
            await self.session.flush()
            return entry
        # Should not normally create — texts are pre-seeded
        return await self.create(key=key, text=text, default_text=text)

    async def reset_to_default(self, key: str) -> BotText | None:
        """Reset text to its default value."""
        result = await self.session.execute(
            select(BotText).where(BotText.key == key)
        )
        entry = result.scalar_one_or_none()
        if entry:
            entry.text = entry.default_text
            await self.session.flush()
        return entry

    async def get_all_texts(self) -> Sequence[BotText]:
        """Get all editable texts (admin panel)."""
        result = await self.session.execute(
            select(BotText).order_by(BotText.key)
        )
        return result.scalars().all()
