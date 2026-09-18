"""safe_historical_restriction_backfill

Revision ID: b28855b9651a
Revises: e80e09802b9f
Create Date: 2026-09-18 23:40:11.714819
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b28855b9651a'
down_revision: Union[str, None] = 'e80e09802b9f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        UPDATE user_restrictions
        SET restricted_by_bot = 0,
            desired_state = 'OFF'
        WHERE NOT EXISTS (
            SELECT 1 FROM campaign_tasks ct
            WHERE ct.user_telegram_id = user_restrictions.user_telegram_id
              AND ct.group_id = user_restrictions.group_id
              AND ct.status IN ('active', 'pending_restriction')
        )
    """)


def downgrade() -> None:
    pass
