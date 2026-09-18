"""Initial schema — all tables

Revision ID: 001
Revises:
Create Date: 2026-09-17
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # === Users ===
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("telegram_id", sa.BigInteger(), unique=True, nullable=False),
        sa.Column("username", sa.String(255), nullable=True),
        sa.Column("first_name", sa.String(255), nullable=True),
        sa.Column("last_name", sa.String(255), nullable=True),
        sa.Column("available", sa.Text(), nullable=False, server_default="0.00"),
        sa.Column("reserved", sa.Text(), nullable=False, server_default="0.00"),
        sa.Column("is_blocked", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("block_reason", sa.Text(), nullable=True),
        sa.Column("blocked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("CAST(available AS REAL) >= 0", name="ck_user_available_gte_0"),
        sa.CheckConstraint("CAST(reserved AS REAL) >= 0", name="ck_user_reserved_gte_0"),
    )
    op.create_index("ix_users_telegram_id", "users", ["telegram_id"], unique=True)
    op.create_index("ix_users_username", "users", ["username"])

    # === Categories ===
    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("buyer_price", sa.Text(), nullable=False),
        sa.Column("seller_payout", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_categories_status", "categories", ["status"])

    # === Seller Groups ===
    op.create_table(
        "seller_groups",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), unique=True, nullable=False),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column("username", sa.String(255), nullable=True),
        sa.Column("category_id", sa.Integer(), sa.ForeignKey("categories.id"), nullable=False),
        sa.Column("tasks_per_distribution", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("interval_minutes", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("bot_has_rights", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("rights_lost_at", sa.DateTime(), nullable=True),
        sa.Column("member_count", sa.Integer(), nullable=True),
        sa.Column("deep_link_code", sa.String(100), nullable=True, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_seller_groups_user", "seller_groups", ["user_id"])
    op.create_index("ix_seller_groups_chat", "seller_groups", ["telegram_chat_id"], unique=True)
    op.create_index("ix_seller_groups_status", "seller_groups", ["status"])
    op.create_index("ix_seller_groups_category", "seller_groups", ["category_id"])

    # === Campaigns ===
    op.create_table(
        "campaigns",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("category_id", sa.Integer(), sa.ForeignKey("categories.id"), nullable=False),
        sa.Column("target_type", sa.String(20), nullable=False),
        sa.Column("target_link", sa.String(500), nullable=False),
        sa.Column("target_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("target_username", sa.String(255), nullable=True),
        sa.Column("target_title_snapshot", sa.String(500), nullable=False),
        sa.Column("target", sa.Integer(), nullable=False),
        sa.Column("completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("buyer_price_snapshot", sa.Text(), nullable=False),
        sa.Column("seller_payout_snapshot", sa.Text(), nullable=False),
        sa.Column("category_name_snapshot", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("completed <= target", name="ck_campaign_completed_lte_target"),
    )
    op.create_index("ix_campaigns_user", "campaigns", ["user_id"])
    op.create_index("ix_campaigns_status", "campaigns", ["status"])
    op.create_index("ix_campaigns_category", "campaigns", ["category_id"])
    op.create_index("ix_campaigns_target_chat", "campaigns", ["target_chat_id"])

    # === Campaign Tasks ===
    op.create_table(
        "campaign_tasks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id"), nullable=False),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("seller_groups.id"), nullable=False),
        sa.Column("user_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("target_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("interval_minutes_snapshot", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_campaign_tasks_campaign", "campaign_tasks", ["campaign_id"])
    op.create_index("ix_campaign_tasks_group", "campaign_tasks", ["group_id"])
    op.create_index("ix_campaign_tasks_user", "campaign_tasks", ["user_telegram_id"])
    op.create_index("ix_campaign_tasks_status", "campaign_tasks", ["status"])
    op.create_index("ix_campaign_tasks_target_chat", "campaign_tasks", ["target_chat_id"])

    # Partial unique indexes (SQLite supports these natively)
    op.execute(
        "CREATE UNIQUE INDEX uix_active_task_user_campaign "
        "ON campaign_tasks (user_telegram_id, campaign_id) "
        "WHERE status = 'active'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uix_active_task_user_target "
        "ON campaign_tasks (user_telegram_id, target_chat_id) "
        "WHERE status = 'active'"
    )

    # === Target User History ===
    op.create_table(
        "target_user_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("target_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("last_completed_at", sa.DateTime(), nullable=True),
        sa.Column("next_available_at", sa.DateTime(), nullable=True),
        sa.Column("completion_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_telegram_id", "target_chat_id", name="uq_target_user_history"),
    )
    op.create_index("ix_target_history_user", "target_user_history", ["user_telegram_id"])
    op.create_index("ix_target_history_target", "target_user_history", ["target_chat_id"])

    # === User Restrictions ===
    op.create_table(
        "user_restrictions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("seller_groups.id"), nullable=False),
        sa.Column("user_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("restricted_by_bot", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("original_permissions_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("group_id", "user_telegram_id", name="uq_user_restriction"),
    )

    # === Deposits ===
    op.create_table(
        "deposits",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("invoice_id", sa.BigInteger(), nullable=True, unique=True),
        sa.Column("amount", sa.Text(), nullable=False),
        sa.Column("asset", sa.String(20), nullable=False, server_default="USDT"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("pay_url", sa.Text(), nullable=True),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("external_status", sa.String(50), nullable=True),
        sa.Column("external_data_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_deposits_user", "deposits", ["user_id"])
    op.create_index("ix_deposits_status", "deposits", ["status"])
    op.create_index("ix_deposits_invoice", "deposits", ["invoice_id"], unique=True)

    # === Withdrawals ===
    op.create_table(
        "withdrawals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("amount", sa.Text(), nullable=False),
        sa.Column("asset", sa.String(20), nullable=False, server_default="USDT"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("spend_id", sa.String(255), nullable=True, unique=True),
        sa.Column("transfer_id", sa.BigInteger(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_withdrawals_user", "withdrawals", ["user_id"])
    op.create_index("ix_withdrawals_status", "withdrawals", ["status"])

    # === Payouts ===
    op.create_table(
        "payouts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("withdrawal_id", sa.Integer(), sa.ForeignKey("withdrawals.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("spend_id", sa.String(255), nullable=False, unique=True),
        sa.Column("transfer_id", sa.BigInteger(), nullable=True),
        sa.Column("amount", sa.Text(), nullable=False),
        sa.Column("asset", sa.String(20), nullable=False, server_default="USDT"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("external_data_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_payouts_withdrawal", "payouts", ["withdrawal_id"])
    op.create_index("ix_payouts_user", "payouts", ["user_id"])

    # === Balance Ledger ===
    op.create_table(
        "balance_ledger",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("operation_type", sa.String(50), nullable=False),
        sa.Column("amount", sa.Text(), nullable=False),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("balance_before", sa.Text(), nullable=False),
        sa.Column("balance_after", sa.Text(), nullable=False),
        sa.Column("available_before", sa.Text(), nullable=False),
        sa.Column("available_after", sa.Text(), nullable=False),
        sa.Column("reserved_before", sa.Text(), nullable=False),
        sa.Column("reserved_after", sa.Text(), nullable=False),
        sa.Column("reference_type", sa.String(50), nullable=True),
        sa.Column("reference_id", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(255), nullable=True, unique=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_ledger_user", "balance_ledger", ["user_id"])
    op.create_index("ix_ledger_created", "balance_ledger", ["created_at"])
    op.create_index("ix_ledger_type", "balance_ledger", ["operation_type"])
    op.create_index("ix_ledger_idem", "balance_ledger", ["idempotency_key"], unique=True)

    # === Refunds ===
    op.create_table(
        "refunds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id"), nullable=False),
        sa.Column("refund_type", sa.String(30), nullable=False),
        sa.Column("gross_amount", sa.Text(), nullable=False),
        sa.Column("commission_percent", sa.Text(), nullable=False),
        sa.Column("commission_amount", sa.Text(), nullable=False),
        sa.Column("net_amount", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=True, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_refunds_user", "refunds", ["user_id"])
    op.create_index("ix_refunds_campaign", "refunds", ["campaign_id"])

    # === Drafts ===
    op.create_table(
        "drafts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("draft_type", sa.String(20), nullable=False, server_default="campaign"),
        sa.Column("data_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_drafts_user", "drafts", ["user_id"])
    op.create_index("ix_drafts_expires", "drafts", ["expires_at"])

    # === Bot Settings ===
    op.create_table(
        "bot_settings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(100), unique=True, nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_bot_settings_key", "bot_settings", ["key"], unique=True)

    # === Bot Texts ===
    op.create_table(
        "bot_texts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(100), unique=True, nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("default_text", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_bot_texts_key", "bot_texts", ["key"], unique=True)

    # === Audit Logs ===
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("actor_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("object_type", sa.String(50), nullable=True),
        sa.Column("object_id", sa.Integer(), nullable=True),
        sa.Column("old_value_json", sa.Text(), nullable=True),
        sa.Column("new_value_json", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_audit_actor", "audit_logs", ["actor_telegram_id"])
    op.create_index("ix_audit_action", "audit_logs", ["action"])
    op.create_index("ix_audit_object", "audit_logs", ["object_type", "object_id"])
    op.create_index("ix_audit_created", "audit_logs", ["created_at"])

    # === System Errors ===
    op.create_table(
        "system_errors",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("severity", sa.String(20), nullable=False, server_default="normal"),
        sa.Column("error_type", sa.String(100), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=True),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_errors_severity", "system_errors", ["severity"])
    op.create_index("ix_errors_resolved", "system_errors", ["resolved"])
    op.create_index("ix_errors_created", "system_errors", ["created_at"])

    # === Idempotency Keys ===
    op.create_table(
        "idempotency_keys",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(255), unique=True, nullable=False),
        sa.Column("operation", sa.String(100), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_idempotency_key", "idempotency_keys", ["key"], unique=True)


def downgrade() -> None:
    op.drop_table("idempotency_keys")
    op.drop_table("system_errors")
    op.drop_table("audit_logs")
    op.drop_table("bot_texts")
    op.drop_table("bot_settings")
    op.drop_table("drafts")
    op.drop_table("refunds")
    op.drop_table("balance_ledger")
    op.drop_table("payouts")
    op.drop_table("withdrawals")
    op.drop_table("deposits")
    op.drop_table("user_restrictions")
    op.drop_table("target_user_history")
    op.drop_table("campaign_tasks")
    op.drop_table("campaigns")
    op.drop_table("seller_groups")
    op.drop_table("categories")
    op.drop_table("users")
