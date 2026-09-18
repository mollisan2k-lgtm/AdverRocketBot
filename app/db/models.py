"""
SQLAlchemy ORM models for AdverRocketBot.

Tables (v2 + v3 + v4):
- users
- categories
- seller_groups
- campaigns
- campaign_tasks
- target_user_history
- user_restrictions
- deposits (invoices)
- withdrawals
- payouts
- balance_ledger
- refunds
- drafts
- bot_settings
- bot_texts
- audit_logs
- system_errors
- idempotency_keys

All monetary values stored as TEXT (Decimal serialized).
Timestamps stored as UTC DateTime.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text as sa_text,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    """Base class for all ORM models."""
    pass


# ── Users ────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String(255), nullable=True, index=True)
    first_name = Column(String(255), nullable=True)
    last_name = Column(String(255), nullable=True)

    # Balance (stored as TEXT for Decimal precision)
    available = Column(Text, nullable=False, default="0.00")
    reserved = Column(Text, nullable=False, default="0.00")
    held_for_withdrawal = Column(Text, nullable=False, default="0.00")

    is_blocked = Column(Boolean, nullable=False, default=False)
    block_reason = Column(Text, nullable=True)
    blocked_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    campaigns = relationship("Campaign", back_populates="user", lazy="selectin")
    seller_groups = relationship("SellerGroup", back_populates="user", lazy="selectin")

    __table_args__ = (
        CheckConstraint("CAST(available AS REAL) >= 0", name="ck_user_available_gte_0"),
        CheckConstraint("CAST(reserved AS REAL) >= 0", name="ck_user_reserved_gte_0"),
        CheckConstraint("CAST(held_for_withdrawal AS REAL) >= 0", name="ck_user_held_gte_0"),
    )


# ── Categories ───────────────────────────────────────────────────────────────

class Category(Base):
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    buyer_price = Column(Text, nullable=False)   # Decimal as text
    seller_payout = Column(Text, nullable=False)  # Decimal as text
    status = Column(String(20), nullable=False, default="active")
    # Statuses: active, disabled, archived

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_categories_status", "status"),
    )


# ── Seller Groups ────────────────────────────────────────────────────────────

class SellerGroup(Base):
    __tablename__ = "seller_groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    telegram_chat_id = Column(BigInteger, unique=True, nullable=False, index=True)
    title = Column(String(255), nullable=True)
    username = Column(String(255), nullable=True)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=False, index=True)

    # Seller settings
    tasks_per_distribution = Column(Integer, nullable=False, default=3)
    interval_minutes = Column(Integer, nullable=False, default=60)

    # Moderation
    status = Column(String(20), nullable=False, default="pending")
    # Statuses: pending, approved, rejected, disabled
    rejection_reason = Column(Text, nullable=True)

    # Bot rights status
    bot_has_rights = Column(Boolean, nullable=False, default=True)
    rights_lost_at = Column(DateTime, nullable=True)

    member_count = Column(Integer, nullable=True)
    deep_link_code = Column(String(100), nullable=True, unique=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="seller_groups")
    category = relationship("Category", lazy="selectin")

    __table_args__ = (
        Index("ix_seller_groups_status", "status"),
        Index("ix_seller_groups_category", "category_id"),
    )


# ── Campaigns ────────────────────────────────────────────────────────────────

class Campaign(Base):
    __tablename__ = "campaigns"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=False, index=True)

    # Target info
    target_type = Column(String(20), nullable=False)  # 'channel' or 'group'
    target_link = Column(String(500), nullable=False)  # User's original input
    target_chat_id = Column(BigInteger, nullable=False, index=True)  # v4: stable Telegram chat_id
    target_username = Column(String(255), nullable=True)              # v4: username at creation
    target_title_snapshot = Column(String(500), nullable=False)       # v4: title at creation

    # Campaign metrics
    target = Column(Integer, nullable=False)  # Target subscriber count
    completed = Column(Integer, nullable=False, default=0)

    # Historical price snapshots (frozen at creation)
    buyer_price_snapshot = Column(Text, nullable=False)
    seller_payout_snapshot = Column(Text, nullable=False)
    category_name_snapshot = Column(String(255), nullable=False)

    # State
    status = Column(String(20), nullable=False, default="active")
    # Statuses: active, paused, completed, cancelled

    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="campaigns")
    category = relationship("Category", lazy="selectin")
    tasks = relationship("CampaignTask", back_populates="campaign", lazy="selectin")

    __table_args__ = (
        CheckConstraint("completed <= target", name="ck_campaign_completed_lte_target"),
        Index("ix_campaigns_status", "status"),
        Index("ix_campaigns_category", "category_id"),
        Index("ix_campaigns_target_chat", "target_chat_id"),
    )


# ── Campaign Tasks ───────────────────────────────────────────────────────────

class CampaignTask(Base):
    __tablename__ = "campaign_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), nullable=False, index=True)
    group_id = Column(Integer, ForeignKey("seller_groups.id"), nullable=False, index=True)
    user_telegram_id = Column(BigInteger, nullable=False, index=True)

    # v4: target_chat_id for partial unique index
    target_chat_id = Column(BigInteger, nullable=False, index=True)

    # v4: seller settings snapshot at task creation
    interval_minutes_snapshot = Column(Integer, nullable=False)

    status = Column(String(20), nullable=False, default="active")
    # Statuses: active, completed, expired, cancelled

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)  # v3: task TTL
    completed_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    campaign = relationship("Campaign", back_populates="tasks")
    group = relationship("SellerGroup", lazy="selectin")

    __table_args__ = (
        Index("ix_campaign_tasks_status", "status"),
        Index("ix_campaign_tasks_user", "user_telegram_id"),
        Index("ix_campaign_tasks_campaign", "campaign_id"),
        # v3: One active task per (user, campaign)
        Index(
            "uix_active_task_user_campaign",
            "user_telegram_id", "campaign_id",
            unique=True,
            sqlite_where=sa_text("status = 'active'"),
        ),
        # v4: One live task per (user, target) globally
        Index(
            "uix_active_task_user_target",
            "user_telegram_id", "target_chat_id",
            unique=True,
            sqlite_where=sa_text("status IN ('active', 'pending_restriction')"),
        ),
    )


# ── Target User History ──────────────────────────────────────────────────────

class TargetUserHistory(Base):
    """
    v4: Tracks (user, target_chat_id) completion history.
    Used for repeat restriction — global, not per seller group.
    """
    __tablename__ = "target_user_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_telegram_id = Column(BigInteger, nullable=False, index=True)
    target_chat_id = Column(BigInteger, nullable=False, index=True)  # v4: stable numeric ID

    last_completed_at = Column(DateTime, nullable=True)
    next_available_at = Column(DateTime, nullable=True)  # v4: pre-calculated availability
    completion_count = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("user_telegram_id", "target_chat_id", name="uq_target_user_history"),
    )


# ── User Restrictions ────────────────────────────────────────────────────────

class UserRestriction(Base):
    """
    v4: Tracks bot-applied restrictions per (group, user).
    Stores original permissions to restore on unrestrict.
    """
    __tablename__ = "user_restrictions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("seller_groups.id"), nullable=False)
    user_telegram_id = Column(BigInteger, nullable=False)

    restricted_by_bot = Column(Boolean, nullable=False, default=True)
    original_permissions_json = Column(Text, nullable=True)

    # Restriction state machine
    desired_state = Column(String(10), nullable=False, default="ON")
    actual_state = Column(String(10), nullable=False, default="OFF")
    operation_token = Column(Integer, nullable=False, default=0)

    # Worker leases for restriction reconciliation
    worker_token = Column(String(36), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("group_id", "user_telegram_id", name="uq_user_restriction"),
    )


# ── Deposits / Invoices ──────────────────────────────────────────────────────

class Deposit(Base):
    __tablename__ = "deposits"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Crypto Pay invoice data
    invoice_id = Column(BigInteger, nullable=True, unique=True, index=True)
    amount = Column(Text, nullable=False)  # Requested amount USDT
    asset = Column(String(20), nullable=False, default="USDT")

    status = Column(String(20), nullable=False, default="pending")
    # Statuses: pending, paid, expired, cancelled

    pay_url = Column(Text, nullable=True)
    paid_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)

    # Crypto Pay response data
    external_status = Column(String(50), nullable=True)
    external_data_json = Column(Text, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_deposits_status", "status"),
        Index("ix_deposits_user", "user_id"),
    )


# ── Withdrawals ──────────────────────────────────────────────────────────────

class Withdrawal(Base):
    __tablename__ = "withdrawals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    amount = Column(Text, nullable=False)
    asset = Column(String(20), nullable=False, default="USDT")

    status = Column(String(20), nullable=False, default="pending")
    # Statuses: pending, approved, processing, completed, rejected, error

    recipient_telegram_id = Column(BigInteger, nullable=True) # Added in Phase 9

    # Crypto Pay payout data
    spend_id = Column(String(255), nullable=True, unique=True)
    transfer_id = Column(BigInteger, nullable=True)

    rejection_reason = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)

    # Worker leases for withdrawal execution
    worker_token = Column(String(36), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)

    approved_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_withdrawals_status", "status"),
        Index("ix_withdrawals_user", "user_id"),
        Index(
            "uix_live_withdrawal_user",
            "user_id",
            unique=True,
            sqlite_where=sa_text("status IN ('pending', 'approved', 'processing')"),
        ),
    )


# ── Payouts ──────────────────────────────────────────────────────────────────

class Payout(Base):
    """Record of individual Crypto Pay transfer for a withdrawal."""
    __tablename__ = "payouts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    withdrawal_id = Column(Integer, ForeignKey("withdrawals.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    spend_id = Column(String(255), nullable=False, unique=True)
    transfer_id = Column(BigInteger, nullable=True)
    amount = Column(Text, nullable=False)
    asset = Column(String(20), nullable=False, default="USDT")

    status = Column(String(20), nullable=False, default="pending")
    # Statuses: pending, completed, failed

    error_message = Column(Text, nullable=True)
    external_data_json = Column(Text, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── Balance Ledger ───────────────────────────────────────────────────────────

class BalanceLedger(Base):
    """
    Financial ledger — every money movement has an entry.
    Invariant: balance_after = available_after + reserved_after
    """
    __tablename__ = "balance_ledger"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    operation_type = Column(String(50), nullable=False)
    # Types: deposit, campaign_reserve, campaign_spend, reserve_release,
    #        refund, refund_commission, seller_reward, withdrawal_reserve,
    #        withdrawal_release, withdrawal_payout, admin_adjustment

    amount = Column(Text, nullable=False)
    direction = Column(String(10), nullable=False)  # 'credit' or 'debit'

    # Balance snapshots
    balance_before = Column(Text, nullable=False)
    balance_after = Column(Text, nullable=False)
    available_before = Column(Text, nullable=False)
    available_after = Column(Text, nullable=False)
    reserved_before = Column(Text, nullable=False)
    reserved_after = Column(Text, nullable=False)

    # References
    reference_type = Column(String(50), nullable=True)  # campaign, withdrawal, deposit, etc.
    reference_id = Column(Integer, nullable=True)
    idempotency_key = Column(String(255), nullable=True, unique=True, index=True)
    reason = Column(Text, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_ledger_user", "user_id"),
        Index("ix_ledger_created", "created_at"),
        Index("ix_ledger_type", "operation_type"),
    )


# ── Refunds ──────────────────────────────────────────────────────────────────

class Refund(Base):
    __tablename__ = "refunds"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), nullable=False, index=True)

    refund_type = Column(String(30), nullable=False)
    # Types: normal_completion, decrease_target, user_cancel, admin_force_complete

    gross_amount = Column(Text, nullable=False)       # Amount before commission
    commission_percent = Column(Text, nullable=False)  # Snapshot
    commission_amount = Column(Text, nullable=False)
    net_amount = Column(Text, nullable=False)          # Actual refund
    idempotency_key = Column(String(255), nullable=True, unique=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


# ── Drafts ───────────────────────────────────────────────────────────────────

class Draft(Base):
    __tablename__ = "drafts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Draft data (JSON)
    draft_type = Column(String(20), nullable=False, default="campaign")
    data_json = Column(Text, nullable=False, default="{}")

    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_drafts_user", "user_id"),
        Index("ix_drafts_expires", "expires_at"),
    )


# ── Bot Settings ─────────────────────────────────────────────────────────────

class BotSetting(Base):
    """Key-value settings changed via admin panel. No restart needed."""
    __tablename__ = "bot_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(100), unique=True, nullable=False, index=True)
    value = Column(Text, nullable=False)
    description = Column(Text, nullable=True)

    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── Bot Texts ────────────────────────────────────────────────────────────────

class BotText(Base):
    """Editable user-facing texts."""
    __tablename__ = "bot_texts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(100), unique=True, nullable=False, index=True)
    text = Column(Text, nullable=False)
    default_text = Column(Text, nullable=False)  # For "restore default"

    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── Audit Log ────────────────────────────────────────────────────────────────

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    actor_telegram_id = Column(BigInteger, nullable=False, index=True)
    action = Column(String(100), nullable=False, index=True)

    object_type = Column(String(50), nullable=True)
    object_id = Column(Integer, nullable=True)

    old_value_json = Column(Text, nullable=True)
    new_value_json = Column(Text, nullable=True)
    reason = Column(Text, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_audit_object", "object_type", "object_id"),
    )


# ── System Errors ────────────────────────────────────────────────────────────

class SystemError_(Base):
    """System errors for 🚨 Urgent Problems panel."""
    __tablename__ = "system_errors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    severity = Column(String(20), nullable=False, default="normal")
    # Severities: critical, high, normal

    error_type = Column(String(100), nullable=False)
    message = Column(Text, nullable=False)
    details_json = Column(Text, nullable=True)

    resolved = Column(Boolean, nullable=False, default=False)
    resolved_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_errors_severity", "severity"),
        Index("ix_errors_resolved", "resolved"),
    )


# ── Idempotency Keys ────────────────────────────────────────────────────────

class IdempotencyKey(Base):
    """
    Ensures critical operations execute at most once.
    Used for: payments, payouts, task execution, campaign completion, refunds.
    """
    __tablename__ = "idempotency_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(255), unique=True, nullable=False, index=True)
    operation = Column(String(100), nullable=False)
    result_json = Column(Text, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


# ── Notifications ────────────────────────────────────────────────────────────

class NotificationOutbox(Base):
    """
    Transactional outbox for Telegram notifications.
    Written in the same transaction as state changes, sent async by background worker.
    """
    __tablename__ = "notification_outbox"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    text = Column(Text, nullable=False)
    parse_mode = Column(String(50), nullable=True, default="HTML")
    
    # Status: pending, sent, error
    status = Column(String(50), nullable=False, default="pending", index=True)
    error_details = Column(Text, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    processed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_notification_status", "status"),
    )
