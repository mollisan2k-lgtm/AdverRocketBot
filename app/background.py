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
                                    from app.db.engine import run_atomic
                                    async def _confirm_op(write_session: AsyncSession):
                                        write_deposit_service = DepositService(write_session, crypto_pay)
                                        await write_deposit_service.confirm_payment(deposit.id, inv)
                                    await run_atomic(_confirm_op)
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
                                from app.db.engine import run_atomic
                                async def _expire_op(write_session: AsyncSession):
                                    write_deposit_service = DepositService(write_session, crypto_pay)
                                    await write_deposit_service.mark_expired(deposit.id)
                                await run_atomic(_expire_op)
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
            from app.db.engine import run_atomic
            async def _task_expire_op(session: AsyncSession) -> int:
                task_service = TaskService(session)
                return await task_service.expire_stale_tasks()
            expired = await run_atomic(_task_expire_op)
            if expired > 0:
                logger.info("Expired %d stale tasks", expired)
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
            from app.db.engine import session_factory, run_atomic
            async with session_factory() as session:
                from app.repositories.group_repo import GroupRepository
                group_repo = GroupRepository(session)
                groups = await group_repo.get_approved()

            checked = 0
            lost = 0

            for group in groups:
                if not group: continue
                try:
                    # HTTP call OUTSIDE of transaction
                    check = await telegram_api.check_group_suitability(group.telegram_chat_id)
                    
                    # DB update INSIDE transaction
                    async def _recheck_op(update_session: AsyncSession):
                        update_repo = GroupRepository(update_session)
                        db_group = await update_repo.get_by_id(group.id)
                        if db_group:
                            had_rights = db_group.bot_has_rights
                            db_group.bot_has_rights = check.ok

                            if not check.ok and had_rights:
                                db_group.rights_lost_at = utc_now()
                                logger.warning(
                                    "Bot lost rights in group %d (%s): %s",
                                    db_group.id, db_group.telegram_chat_id, check.reason,
                                )
                            elif check.ok and not had_rights:
                                db_group.rights_lost_at = None
                                logger.info("Bot rights restored in group %d", db_group.id)

                            if check.member_count > 0:
                                db_group.member_count = check.member_count

                            db_group.updated_at = utc_now()
                    await run_atomic(_recheck_op)

                    checked += 1
                    if not check.ok:
                        lost += 1
                except Exception as e:
                    logger.warning(
                        "Rights check failed for group %d: %s",
                        group.id, e,
                    )
                # Rate limit: 1 check per 0.5s
                await asyncio.sleep(0.5)

            if checked > 0:
                logger.info(
                    "Rights recheck: %d groups checked, %d lost rights",
                    checked, lost,
                )

        except Exception as e:
            logger.error("Rights recheck error: %s", e, exc_info=True)

        await asyncio.sleep(interval)


# ── Restriction Reconciliation ───────────────────────────────────────────────

async def restriction_reconciliation_loop(
    telegram_api: TelegramAPIService,
    interval: int = 15,
) -> None:
    """
    Background loop to align actual_state with desired_state for user restrictions.
    Uses worker_token fencing and atomic_session.
    """
    import uuid
    worker_token = str(uuid.uuid4())
    logger.info("Restriction reconciliation started (interval=%ds, worker_token=%s)", interval, worker_token)

    while True:
        try:
            from app.db.engine import run_atomic
            
            # 1. Claim restrictions that need reconciliation
            async def _claim_op(session: AsyncSession):
                from app.repositories.restriction_repo import RestrictionRepository
                repo = RestrictionRepository(session)
                return await repo.claim_for_reconciliation(worker_token, lease_minutes=2)
            claimed = await run_atomic(_claim_op)
            
            if not claimed:
                await asyncio.sleep(interval)
                continue

            # 2. Process each claimed record
            for r in claimed:
                try:
                    async def _reconcile_op(process_session: AsyncSession):
                        from app.services.restriction_service import RestrictionService
                        r_service = RestrictionService(process_session, telegram_api)
                        return await r_service.reconcile_record(r.id, worker_token)
                    result = await run_atomic(_reconcile_op)
                    if result.get("ok"):
                        logger.debug(
                            "Restriction %d reconciled (user=%d, group=%d)",
                            r.id, r.user_telegram_id, r.group_id
                        )
                    else:
                        logger.error(
                            "Restriction %d failed reconciliation: %s",
                            r.id, result.get("error")
                        )
                except Exception as e:
                    logger.error("Error reconciling restriction %d: %s", r.id, e, exc_info=True)

        except Exception as e:
            logger.error("Restriction reconciliation loop error: %s", e, exc_info=True)

        await asyncio.sleep(interval)


# ── Withdrawal Processor ────────────────────────────────────────────────────

async def withdrawal_processor_loop(
    crypto_pay: CryptoPayService,
    interval: int = 60,
) -> None:
    """
    Process approved withdrawals via Crypto Pay transfers.
    Uses worker_token fencing and atomic_session to prevent double-spending.
    """
    import uuid
    worker_token = str(uuid.uuid4())
    logger.info("Withdrawal processor started (interval=%ds, worker_token=%s)", interval, worker_token)

    while True:
        try:
            from app.db.engine import run_atomic
            
            # 1. Claim withdrawals
            async def _claim_wd_op(session: AsyncSession):
                from app.repositories.withdrawal_repo import WithdrawalRepository
                wd_repo = WithdrawalRepository(session)
                return await wd_repo.claim_for_processing(worker_token, lease_minutes=5)
            claimed = await run_atomic(_claim_wd_op)
            
            if not claimed:
                await asyncio.sleep(interval)
                continue

            # 2. Process each claimed withdrawal
            for w in claimed:
                try:
                    async def _process_wd_op(process_session: AsyncSession):
                        wd_service = WithdrawalService(process_session, crypto_pay)
                        from app.repositories.user_repo import UserRepository
                        user_repo = UserRepository(process_session)
                        user = await user_repo.get_by_id(w.user_id)
                        if not user:
                            return None
                        return await wd_service.process_payout(
                            w.id, worker_token=worker_token
                        )
                    result = await run_atomic(_process_wd_op)
                    if result is None:
                        continue
                    if result.get("ok"):
                        logger.info("Withdrawal %d processed: transfer_id=%s", w.id, result.get("transfer_id"))
                    elif result.get("needs_reconciliation"):
                        logger.warning("Withdrawal %d needs reconciliation", w.id)
                    else:
                        logger.error("Withdrawal %d failed: %s", w.id, result.get("error"))

                except Exception as e:
                    logger.error(
                        "Withdrawal %d processing error: %s",
                        w.id, e, exc_info=True,
                    )
                    async def _err_op(err_session: AsyncSession):
                        from app.repositories.error_repo import SystemErrorRepository
                        error_repo = SystemErrorRepository(err_session)
                        await error_repo.log_error(
                            error_type="withdrawal_processing",
                            message=str(e),
                            severity="high",
                            details_json=f'{{"withdrawal_id": {w.id}}}',
                        )
                    await run_atomic(_err_op)

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
            from app.db.engine import session_factory, run_atomic
            
            async with session_factory() as session:
                from app.repositories.group_repo import GroupRepository
                group_repo = GroupRepository(session)
                groups = await group_repo.get_approved()

            distributed = 0
            skipped = 0

            for group in groups:
                if not group.bot_has_rights:
                    continue

                async with session_factory() as session:
                    from app.repositories.restriction_repo import RestrictionRepository
                    from app.services.campaign_service import CampaignService
                    restriction_repo = RestrictionRepository(session)
                    campaign_service = CampaignService(session)
                    
                    restricted_users = await restriction_repo.get_active_by_group(group.id)
                    campaigns = await campaign_service.get_distributable_campaigns(group.category_id)

                if not campaigns or not restricted_users:
                    continue

                for restriction in restricted_users:
                    user_tg_id = restriction.user_telegram_id

                    # Assign tasks atomically per user
                    try:
                        async def _assign_dist_op(assign_session: AsyncSession) -> list[int]:
                            nonlocal skipped
                            from app.services.task_service import TaskService, TaskDistributionError
                            from app.repositories.task_repo import TaskRepository
                            from app.repositories.settings_repo import SettingsRepository
                            from app.repositories.campaign_repo import CampaignRepository
                            
                            task_service = TaskService(assign_session)
                            task_repo = TaskRepository(assign_session)
                            settings_repo = SettingsRepository(assign_session)
                            campaign_repo = CampaignRepository(assign_session)

                            max_tasks_str = await settings_repo.get_value("max_tasks_per_user")
                            max_tasks_per_user = int(max_tasks_str) if max_tasks_str else 3

                            active_tasks = await task_repo.get_active_by_user(user_tg_id)
                            active_count = len(active_tasks)
                            if active_count >= max_tasks_per_user:
                                return []

                            user_distributed = 0
                            assigned_tasks = []

                            for camp in campaigns:
                                if user_distributed >= group.tasks_per_distribution:
                                    break
                                    
                                if active_count >= max_tasks_per_user:
                                    break

                                fresh_camp = await campaign_repo.get_by_id(camp.id)
                                if not fresh_camp or fresh_camp.completed >= fresh_camp.target:
                                    continue

                                try:
                                    task = await task_service.assign_task(
                                        user_telegram_id=user_tg_id,
                                        campaign_id=fresh_camp.id,
                                        group_id=group.id,
                                    )
                                    assigned_tasks.append(task.id)
                                    user_distributed += 1
                                    active_count += 1
                                    logger.debug(
                                        "Task %d distributed: user=%d campaign=%d group=%d",
                                        task.id, user_tg_id, fresh_camp.id, group.id,
                                    )
                                except TaskDistributionError:
                                    skipped += 1
                                    continue
                                except Exception as e:
                                    logger.warning(
                                        "Distribution error for user=%d campaign=%d: %s",
                                        user_tg_id, fresh_camp.id, e,
                                    )
                                    continue
                            return assigned_tasks
                            
                        assigned_ids = await run_atomic(_assign_dist_op)
                        
                        if assigned_ids:
                            from app.services.restriction_service import RestrictionService
                            from app.db.engine import session_factory
                            
                            original_perms = await telegram_api.get_member_permissions(
                                group.telegram_chat_id, user_tg_id
                            )
                            
                            async with session_factory() as temp_session:
                                restriction_service = RestrictionService(temp_session, telegram_api)
                                for t_id in assigned_ids:
                                    res = await restriction_service.apply_restriction_for_task(
                                        t_id, original_permissions=original_perms
                                    )
                                    if res["ok"]:
                                        distributed += 1
                                    else:
                                        logger.warning("Auto-distribution restriction failed for task %d", t_id)
                                        
                    except Exception as e:
                        logger.error("Distribution atomic session failed for user %d: %s", user_tg_id, e)

                await asyncio.sleep(0.1)  # Yield between groups

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
        asyncio.create_task(
            restriction_reconciliation_loop(telegram_api, interval=15),
            name="restriction_reconciliation",
        ),
    ]
    logger.info("Started %d background tasks", len(tasks))
    return tasks
