"""Repository layer — data access through SQLAlchemy."""

from app.repositories.user_repo import UserRepository
from app.repositories.category_repo import CategoryRepository
from app.repositories.group_repo import GroupRepository
from app.repositories.campaign_repo import CampaignRepository
from app.repositories.task_repo import TaskRepository
from app.repositories.history_repo import HistoryRepository
from app.repositories.deposit_repo import DepositRepository
from app.repositories.withdrawal_repo import WithdrawalRepository
from app.repositories.payout_repo import PayoutRepository
from app.repositories.ledger_repo import LedgerRepository
from app.repositories.refund_repo import RefundRepository
from app.repositories.draft_repo import DraftRepository
from app.repositories.settings_repo import SettingsRepository, TextRepository
from app.repositories.audit_repo import AuditLogRepository
from app.repositories.error_repo import SystemErrorRepository
from app.repositories.idempotency_repo import IdempotencyRepository
from app.repositories.restriction_repo import RestrictionRepository

__all__ = [
    "UserRepository",
    "CategoryRepository",
    "GroupRepository",
    "CampaignRepository",
    "TaskRepository",
    "HistoryRepository",
    "DepositRepository",
    "WithdrawalRepository",
    "PayoutRepository",
    "LedgerRepository",
    "RefundRepository",
    "DraftRepository",
    "SettingsRepository",
    "TextRepository",
    "AuditLogRepository",
    "SystemErrorRepository",
    "IdempotencyRepository",
    "RestrictionRepository",
]
