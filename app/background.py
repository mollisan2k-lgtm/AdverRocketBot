"""
Background tasks — periodic jobs running alongside the bot.

Tasks:
1. Invoice checker — poll Crypto Pay for paid invoices
2. Task expiry — expire overdue tasks (TTL)
3. Rights recheck — verify bot admin rights in seller groups
4. Withdrawal processor — process approved withdrawals
5. Task distributor — push tasks to eligible group members
"""

from __future__ import annotations

import asyncio
import logging

from app.db.engine import session_factory
from app.services.deposit_service import DepositService
from app.services.task_service import TaskService
from app.services.group_service import GroupService
from app.services.withdrawal_service import WithdrawalService
from app.integrations.crypto_pay import CryptoPayService
from app.integrations.telegram_api import TelegramAPIService
from app.repositories.error_repo import SystemErrorRepository
from app.utils.time_utils import utc_now

logger = logging.getLogger(__name__)


# ── Invoice Checker ──────────────────────────────────────────────────────────

async def invoice_checker_loop(
    crypto_pay: CryptoPayService,
    interval: int = 30,
) -> None:
    """
    Poll Crypto Pay for paid invoices every `interval` seconds.

    Finds all local deposits with status='pending' that have an invoice_id,
    checks their status via Crypto Pay API, and credits balance if paid.
    """
    logger.info("Invoice checker started (interval=%ds)", interval)

    while True:
        try:
            async with session_factory() as session:
                deposit_service = DepositService(session, crypto_pay)
                pending = await deposit_service.get_pending_deposits()

                if not pending:
                    await asyncio.sleep(interval)
                    continue

                # Batch check: collect invoice IDs
                invoice_ids = [
                    d.invoice_id for d in pending
                    if d.invoice_id is not None
                ]

                if not invoice_ids:
                    await asyncio.sleep(interval)
                    continue

                # Query Crypto Pay in batches of 100
                for i in range(0, len(invoice_ids), 100):
                    batch = invoice_ids[i:i + 100]
                    try:
                        invoices = await crypto_pay.get_invoices(
                            invoice_ids=batch,
                        )
                    except Exception as e:
                        logger.error("Invoice check batch failed: %s", e)
                        continue

                    for inv in invoices:
                        if inv.status == "paid":
                            # Find matching deposit
                            deposit = await deposit_service.get_by_invoice_id(
                                inv.invoice_id,
                            )
                            if deposit and deposit.status == "pending":
                                try:
                                    await deposit_service.confirm_payment(
                                        deposit.id, inv,
                                    )
                                    logger.info(
                                        "Deposit %d confirmed via polling",
                                        deposit.id,
                                    )
                                except Exception as e:
                                    logger.error(
                                        "Failed to confirm deposit %d: %s",
                                        deposit.id, e,
                                    )

                        elif inv.status == "expired":
                            deposit = await deposit_service.get_by_invoice_id(
                                inv.invoice_id,
                            )
                            if deposit and deposit.status == "pending":
                                await deposit_service.mark_expired(deposit.id)
                                logger.info(
                                    "Deposit %d marked expired", deposit.id,
                                )

                await session.commit()

        except Exception as e:
            logger.error("Invoice checker error: %s", e, exc_info=True)

        await asyncio.sleep(interval)


# ── Task Expiry ──────────────────────────────────────────────────────────────

async def task_expiry_loop(interval: int = 60) -> None:
    """
    Expire tasks that exceeded their TTL.
    Runs every `interval` seconds.
    """
    logger.info("Task expiry checker started (interval=%ds)", interval)

    while True:
        try:
            async with session_factory() as session:
                task_service = TaskService(session)
                expired = await task_service.expire_stale_tasks()
                if expired > 0:
                    logger.info("Expired %d stale tasks", expired)
                await session.commit()
        except Exception as e:
            logger.error("Task expiry error: %s", e, exc_info=True)

        await asyncio.sleep(interval)


# ── Rights Recheck ───────────────────────────────────────────────────────────

async def rights_recheck_loop(
    telegram_api: TelegramAPIService,
    interval: int = 900,  # 15 minutes
) -> None:
    """
    Periodically check if bot still has admin rights in approved groups.
    """
    logger.info("Rights recheck started (interval=%ds)", interval)

    while True:
        try:
            async with session_factory() as session:
                from app.repositories.group_repo import GroupRepository
                group_repo = GroupRepository(session)
                groups = await group_repo.get_approved()

                group_service = GroupService(session, telegram_api)
                checked = 0
                lost = 0

                for group in groups:
                    try:
                        has_rights = await group_service.check_and_update_rights(group)
                        checked += 1
                        if not has_rights:
                            lost += 1
                    except Exception as e:
                        logger.warning(
                            "Rights check failed for group %d: %s",
                            group.id, e,
                        )
                    # Rate limit: 1 check per 0.5s
                    await asyncio.sleep(0.5)

                await session.commit()

                if checked > 0:
                    logger.info(
                        "Rights recheck: %d groups checked, %d lost rights",
                        checked, lost,
                    )

        except Exception as e:
            logger.error("Rights recheck error: %s", e, exc_info=True)

        await asyncio.sleep(interval)


# ── Withdrawal Processor ────────────────────────────────────────────────────

async def withdrawal_processor_loop(
    crypto_pay: CryptoPayService,
    interval: int = 60,
) -> None:
    """
    Process approved withdrawals via Crypto Pay transfers.
    """
    logger.info("Withdrawal processor started (interval=%ds)", interval)

    while True:
        try:
            async with session_factory() as session:
                wd_service = WithdrawalService(session, crypto_pay)
                approved = await wd_service.get_approved()

                for w in approved:
                    try:
                        # Look up user's telegram_id
                        from app.repositories.user_repo import UserRepository
                        user_repo = UserRepository(session)
                        user = await user_repo.get_by_id(w.user_id)
                        if not user:
                            continue

                        result = await wd_service.process_payout(
                            w.id, user.telegram_id,
                        )
                        if result.get("ok"):
                            logger.info(
                                "Withdrawal %d processed: transfer_id=%s",
                                w.id, result.get("transfer_id"),
                            )
                        elif result.get("needs_reconciliation"):
                            logger.warning(
                                "Withdrawal %d needs reconciliation",
                                w.id,
                            )
                        else:
                            logger.error(
                                "Withdrawal %d failed: %s",
                                w.id, result.get("error"),
                            )

                    except Exception as e:
                        logger.error(
                            "Withdrawal %d processing error: %s",
                            w.id, e, exc_info=True,
                        )
                        # Log to system_errors
                        error_repo = SystemErrorRepository(session)
                        await error_repo.log_error(
                            error_type="withdrawal_processing",
                            message=str(e),
                            severity="high",
                            details_json=f'{{"withdrawal_id": {w.id}}}',
                        )

                await session.commit()

        except Exception as e:
            logger.error("Withdrawal processor error: %s", e, exc_info=True)

        await asyncio.sleep(interval)


# ── Task Distributor ─────────────────────────────────────────────────────────

async def task_distribution_loop(
    telegram_api: TelegramAPIService,
    interval: int = 300,  # 5 minutes
) -> None:
    """
    Periodically distribute tasks to active members of approved seller groups.

    For each approved group:
    1. Fetch members via Telegram API (getChatAdministrators for health, actual
       member scanning is done via restriction mechanism on join event).
       Here we just look at known restrictions / queued users.
    2. Pick distributable campaigns for group's category.
    3. Call task_service.assign_task for each eligible user.

    Note: Primary distribution happens via on_new_member (ChatMemberUpdated).
    This loop handles periodic re-offers to members who haven't gotten a task yet
    (e.g. members who joined before bot or whose task expired).
    """
    logger.info("Task distributor started (interval=%ds)", interval)

    while True:
        try:
            async with session_factory() as session:
                from app.repositories.group_repo import GroupRepository
                from app.repositories.restriction_repo import RestrictionRepository
                from app.services.task_service import TaskService, TaskDistributionError
                from app.services.campaign_service import CampaignService
                from app.repositories.settings_repo import SettingsRepository
                from app.repositories.task_repo import TaskRepository

                group_repo = GroupRepository(session)
                restriction_repo = RestrictionRepository(session)
                task_service = TaskService(session)
                campaign_service = CampaignService(session)
                settings_repo = SettingsRepository(session)
                task_repo = TaskRepository(session)

                max_tasks_str = await settings_repo.get_value("max_tasks_per_user")
                max_tasks_per_user = int(max_tasks_str) if max_tasks_str else 3

                groups = await group_repo.get_approved()
                distributed = 0
                skipped = 0

                for group in groups:
                    if not group.bot_has_rights:
                        continue  # Skip groups where bot lost rights

                    # Get users currently restricted in this group (awaiting task completion)
                    restricted_users = await restriction_repo.get_active_by_group(
                        group.id
                    )

                    # Find distributable campaigns for this group's category
                    campaigns = await campaign_service.get_distributable_campaigns(
                        group.category_id
                    )
                    if not campaigns:
                        continue

                    for restriction in restricted_users:
                        user_tg_id = restriction.user_telegram_id

                        # Check max_tasks_per_user
                        active_tasks = await task_repo.get_active_by_user(user_tg_id)
                        active_count = len(active_tasks)
                        if active_count >= max_tasks_per_user:
                            continue

                        user_distributed = 0

                        # Try to find campaigns for them
                        for campaign in campaigns:
                            if user_distributed >= group.tasks_per_distribution:
                                break
                                
                            if active_count >= max_tasks_per_user:
                                break

                            if campaign.completed >= campaign.target:
                                continue

                            try:
                                task = await task_service.assign_task(
                                    user_telegram_id=user_tg_id,
                                    campaign_id=campaign.id,
                                    group_id=group.id,
                                )
                                distributed += 1
                                user_distributed += 1
                                active_count += 1
                                logger.debug(
                                    "Task %d distributed: user=%d campaign=%d group=%d",
                                    task.id, user_tg_id, campaign.id, group.id,
                                )
                            except TaskDistributionError:
                                skipped += 1
                                continue
                            except Exception as e:
                                logger.warning(
                                    "Distribution error for user=%d campaign=%d: %s",
                                    user_tg_id, campaign.id, e,
                                )
                                continue

                    await asyncio.sleep(0.1)  # Yield between groups

                await session.commit()

                if distributed > 0:
                    logger.info(
                        "Task distributor: distributed=%d skipped=%d",
                        distributed, skipped,
                    )

        except Exception as e:
            logger.error("Task distributor error: %s", e, exc_info=True)

        await asyncio.sleep(interval)


# ── Start all background tasks ───────────────────────────────────────────────

def start_background_tasks(
    crypto_pay: CryptoPayService,
    telegram_api: TelegramAPIService,
) -> list[asyncio.Task]:
    """Create and return all background task coroutines."""
    tasks = [
        asyncio.create_task(
            invoice_checker_loop(crypto_pay, interval=30),
            name="invoice_checker",
        ),
        asyncio.create_task(
            task_expiry_loop(interval=60),
            name="task_expiry",
        ),
        asyncio.create_task(
            rights_recheck_loop(telegram_api, interval=900),
            name="rights_recheck",
        ),
        asyncio.create_task(
            withdrawal_processor_loop(crypto_pay, interval=60),
            name="withdrawal_processor",
        ),
        asyncio.create_task(
            task_distribution_loop(telegram_api, interval=300),
            name="task_distributor",
        ),
    ]
    logger.info("Started %d background tasks", len(tasks))
    return tasks
