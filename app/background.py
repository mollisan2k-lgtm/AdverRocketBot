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
    Uses strict 3-phase fencing.
    """
    import uuid
    import platform
    worker_id = f"invoice_checker_loop@{platform.node()}"
    logger.info("Invoice checker started (interval=%ds, worker_id=%s)", interval, worker_id)

    while True:
        try:
            from app.db.engine import run_atomic
            from sqlalchemy import select, update
            claim_token = str(uuid.uuid4())
            
            # PHASE 1: Claim
            async def _claim(session: AsyncSession):
                now = utc_now()
                res = await session.execute(
                    select(Deposit)
                    .where(
                        (Deposit.status == "invoice_created") &
                        ((Deposit.lease_expires_at.is_(None)) | (Deposit.lease_expires_at <= now)) &
                        (Deposit.invoice_id.is_not(None))
                    )
                    .limit(100)
                    .with_for_update(skip_locked=True)
                )
                deps = list(res.scalars().all())
                if not deps:
                    return []
                    
                ids = [d.id for d in deps]
                await session.execute(
                    update(Deposit)
                    .where(Deposit.id.in_(ids))
                    .values(
                        worker_id=worker_id,
                        claim_token=claim_token,
                        lease_expires_at=minutes_from_now(2),
                        generation=Deposit.generation + 1
                    )
                )
                res2 = await session.execute(select(Deposit).where(Deposit.id.in_(ids)))
                return list(res2.scalars().all())
                
            claimed = await run_atomic(_claim)
            
            if not claimed:
                await asyncio.sleep(interval)
                continue

            # PHASE 2: HTTP
            invoice_ids = [str(d.invoice_id) for d in claimed if d.invoice_id]
            inv_map = {}
            if invoice_ids:
                try:
                    invoices = await crypto_pay.get_invoices(invoice_ids=invoice_ids)
                    inv_map = {str(inv.invoice_id): inv for inv in invoices}
                except Exception as e:
                    logger.error("Invoice check batch failed: %s", e)
            
            # PHASE 3: DB Finalize
            for d in claimed:
                try:
                    async def _fin(s: AsyncSession):
                        db_dep = await s.scalar(
                            select(Deposit)
                            .where(
                                Deposit.id == d.id,
                                Deposit.status == "invoice_created",
                                Deposit.claim_token == claim_token,
                                Deposit.generation == d.generation
                            )
                        )
                        if not db_dep:
                            logger.warning("Fenced out of deposit %d during check", d.id)
                            return
                        
                        inv = inv_map.get(str(db_dep.invoice_id))
                        if inv:
                            if inv.status == "paid":
                                deposit_service = DepositService(s, crypto_pay)
                                await deposit_service.confirm_payment(db_dep.id, inv)
                            elif inv.status == "expired":
                                deposit_service = DepositService(s, crypto_pay)
                                await deposit_service.mark_expired(db_dep.id)
                            else:
                                # Still pending, just release lease
                                db_dep.lease_expires_at = None
                        else:
                            # Not found in API? Maybe too old or error. Just release lease for now.
                            db_dep.lease_expires_at = None
                            
                        db_dep.generation += 1
                        
                    await run_atomic(_fin)
                except Exception as e:
                    logger.error("Failed to finalize check for deposit %d: %s", d.id, e)
                    
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
    import platform
    import json
    worker_id = f"restriction_reconciler_loop@{platform.node()}"
    logger.info("Restriction reconciliation started (interval=%ds, worker_id=%s)", interval, worker_id)

    while True:
        try:
            from app.db.engine import run_atomic
            claim_token = str(uuid.uuid4())
            
            # Phase 1: Claim
            async def _claim_op(session: AsyncSession):
                from app.repositories.restriction_repo import RestrictionRepository
                repo = RestrictionRepository(session)
                claimed = await repo.claim_for_reconciliation(worker_id, claim_token, limit=50)
                return list(claimed) if type(claimed) == list else list(claimed.scalars().all())
            claimed = await run_atomic(_claim_op)
            
            if not claimed:
                await asyncio.sleep(interval)
                continue

            # Process each claimed record
            for r in claimed:
                try:
                    # Collect needed info
                    group_id = r.group_id
                    user_id = r.user_telegram_id
                    desired = r.desired_state
                    expected_gen = r.generation
                    op_token = r.operation_token
                    
                    # We need the group's telegram chat ID
                    chat_id = None
                    async def _get_group(s: AsyncSession):
                        from app.repositories.group_repo import GroupRepository
                        gr = GroupRepository(s)
                        g = await gr.get_by_id(group_id)
                        return g.telegram_chat_id if g else None
                        
                    chat_id = await run_atomic(_get_group)
                    if not chat_id:
                        continue
                        
                    # Phase 2: HTTP
                    success = False
                    try:
                        if desired == "ON":
                            success = await telegram_api.restrict_member(chat_id, user_id)
                        else:
                            perms = None
                            if r.original_permissions_json:
                                try:
                                    perms = json.loads(r.original_permissions_json)
                                except Exception:
                                    pass
                            success = await telegram_api.unrestrict_member(chat_id, user_id, perms)
                    except Exception as e:
                        logger.error("Restriction HTTP error for user %d in %s: %s", user_id, chat_id, e)
                        success = False
                        
                    # Phase 3: Finalize
                    async def _fin(s: AsyncSession):
                        from app.services.restriction_service import RestrictionService
                        r_service = RestrictionService(s, telegram_api)
                        return await r_service.finalize_reconciliation(r.id, claim_token, expected_gen, success, op_token)
                        
                    result = await run_atomic(_fin)
                    if result and result.get("ok"):
                        logger.debug(
                            "Restriction %d reconciled (user=%d, group=%d)",
                            r.id, user_id, group_id
                        )
                    else:
                        logger.error(
                            "Restriction %d failed reconciliation: %s",
                            r.id, result.get("error") if result else "Unknown error"
                        )
                except Exception as e:
                    logger.error("Error reconciling restriction %d: %s", r.id, e, exc_info=True)
                    
        except Exception as e:
            logger.error("Restriction reconciliation loop error: %s", e, exc_info=True)
            
        await asyncio.sleep(interval)


# ── Notification Sender ──────────────────────────────────────────────────────

async def notification_sender_loop(
    telegram_api: TelegramAPIService,
    interval: int = 5,
) -> None:
    """
    Process outbox notifications.
    Uses strict 3-phase fencing.
    """
    import uuid
    import platform
    worker_id = f"notification_sender_loop@{platform.node()}"
    logger.info("Notification sender started (interval=%ds, worker_id=%s)", interval, worker_id)

    while True:
        try:
            from app.db.engine import run_atomic
            claim_token = str(uuid.uuid4())
            
            # PHASE 1: DB Claim
            async def _claim_op(session: AsyncSession):
                from app.repositories.notification_repo import NotificationRepository
                repo = NotificationRepository(session)
                claimed = await repo.claim_for_sending(worker_id, claim_token, limit=50)
                # Ensure they are detached or return scalars
                return list(claimed) if type(claimed) == list else list(claimed.scalars().all())
            claimed = await run_atomic(_claim_op)
            
            if not claimed:
                await asyncio.sleep(interval)
                continue

            # PHASE 2 & 3
            for n in claimed:
                try:
                    # PHASE 2: HTTP
                    import json
                    reply_markup = None
                    if n.reply_markup_json:
                        try:
                            reply_markup = json.loads(n.reply_markup_json)
                        except Exception:
                            pass
                    
                    try:
                        success = await telegram_api.send_message(
                            chat_id=n.telegram_id,
                            text=n.text,
                            parse_mode=n.parse_mode,
                            reply_markup=reply_markup
                        )
                        error = None if success else "Telegram API returned False"
                        is_permanent = False
                    except Exception as e:
                        success = False
                        error = str(e)
                        is_permanent = "bot was blocked by the user" in error.lower() or "chat not found" in error.lower()

                    # PHASE 3: DB Finalize
                    async def _fin(s: AsyncSession):
                        from app.repositories.notification_repo import NotificationRepository
                        repo = NotificationRepository(s)
                        # We expect generation + 1 because claim_for_sending incremented it by 1
                        expected_gen = n.generation
                        if success:
                            fenced = await repo.mark_sent(n.id, expected_gen, claim_token)
                        else:
                            fenced = await repo.mark_error(n.id, expected_gen, claim_token, str(error), is_permanent, n.attempts)
                        if not fenced:
                            logger.warning("Fenced out! Stale worker detected for notification %d", n.id)
                    await run_atomic(_fin)
                except Exception as e:
                    logger.error("Error sending notification %d: %s", n.id, e, exc_info=True)

        except Exception as e:
            logger.error("Notification sender error: %s", e, exc_info=True)

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
    import platform
    worker_id = f"withdrawal_processor_loop@{platform.node()}"
    logger.info("Withdrawal processor started (interval=%ds, worker_id=%s)", interval, worker_id)

    while True:
        try:
            from app.db.engine import run_atomic
            claim_token = str(uuid.uuid4())
            
            # 1. Claim withdrawals
            async def _claim_wd_op(session: AsyncSession):
                from app.repositories.withdrawal_repo import WithdrawalRepository
                wd_repo = WithdrawalRepository(session)
                claimed = await wd_repo.claim_for_processing(worker_id, claim_token, limit=5)
                # Ensure they are detached or return scalars
                return list(claimed) if type(claimed) == list else list(claimed.scalars().all())
            claimed = await run_atomic(_claim_wd_op)
            
            if not claimed:
                await asyncio.sleep(interval)
                continue

            # 2. Process each claimed withdrawal
            for w in claimed:
                try:
                    # Phase 1: Prepare DB
                    async def _prep(session: AsyncSession):
                        wd_service = WithdrawalService(session, crypto_pay)
                        return await wd_service.prepare_payout(w.id, worker_id, claim_token, w.generation)
                    prep_res = await run_atomic(_prep)
                    if not prep_res:
                        logger.warning("Failed to prepare payout for withdrawal %d", w.id)
                        continue
                        
                    spend_id = prep_res["spend_id"]
                    amount = prep_res["amount"]
                    
                    # Phase 2: HTTP
                    success = False
                    transfer_id = None
                    is_ambiguous = False
                    error_msg = None
                    
                    try:
                        transfer = await crypto_pay.transfer(
                            user_id=w.recipient_telegram_id,
                            asset=w.asset,
                            amount=str(amount),
                            spend_id=spend_id,
                            comment=f"Выплата с AdverRocketBot #{w.id}"
                        )
                        success = True
                        transfer_id = transfer.transfer_id
                    except CryptoPayNetworkError as e:
                        error_msg = str(e)
                        is_ambiguous = True
                    except Exception as e:
                        error_msg = str(e)
                        is_ambiguous = False
                        
                    # Phase 3: Finalize
                    async def _fin(session: AsyncSession):
                        wd_service = WithdrawalService(session, crypto_pay)
                        # prepare_payout incremented generation
                        expected_gen = w.generation + 1
                        
                        if success:
                            ok = await wd_service.finalize_payout_success(w.id, claim_token, expected_gen, transfer_id)
                        elif is_ambiguous:
                            ok = await wd_service.mark_payout_ambiguous(w.id, claim_token, expected_gen)
                        else:
                            ok = await wd_service.finalize_payout_failure(w.id, claim_token, expected_gen, error_msg)
                            
                        if not ok:
                            logger.error("Failed to finalize payout for withdrawal %d", w.id)
                            
                    await run_atomic(_fin)

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


# ── Withdrawal Reconciler ───────────────────────────────────────────────────

async def withdrawal_reconciler_loop(
    crypto_pay: CryptoPayService,
    interval: int = 60,
) -> None:
    """
    Reconciles withdrawals in reconciliation_required state.
    Uses 3-phase fencing.
    """
    import uuid
    import platform
    worker_id = f"withdrawal_reconciler_loop@{platform.node()}"
    logger.info("Withdrawal reconciler started (interval=%ds, worker_id=%s)", interval, worker_id)

    while True:
        try:
            from app.db.engine import run_atomic
            claim_token = str(uuid.uuid4())
            
            # Phase 1: Claim
            async def _claim(session: AsyncSession):
                from app.repositories.withdrawal_repo import WithdrawalRepository
                wd_repo = WithdrawalRepository(session)
                claimed = await wd_repo.claim_for_reconciliation(worker_id, claim_token, limit=5)
                return list(claimed) if type(claimed) == list else list(claimed.scalars().all())
            claimed = await run_atomic(_claim)
            
            if not claimed:
                await asyncio.sleep(interval)
                continue
                
            for w in claimed:
                try:
                    # Phase 2: HTTP
                    spend_id = w.spend_id
                    if not spend_id:
                        spend_id = f"w{w.id}"
                        
                    found = False
                    transfer_id = None
                    try:
                        transfers = await crypto_pay.get_transfers(spend_id=spend_id)
                        if transfers and len(transfers) > 0:
                            found = True
                            transfer_id = transfers[0].transfer_id
                    except Exception as e:
                        logger.error("Withdrawal %d reconciliation network error: %s", w.id, e)
                        continue # leave it in reconciliation_required
                        
                    # Phase 3: Finalize
                    async def _fin(session: AsyncSession):
                        from sqlalchemy import select, update
                        from app.services.withdrawal_service import WithdrawalService
                        
                        wd_service = WithdrawalService(session, crypto_pay)
                        expected_gen = w.generation
                        
                        db_w = await session.scalar(
                            select(Withdrawal)
                            .where(
                                Withdrawal.id == w.id,
                                Withdrawal.claim_token == claim_token,
                                Withdrawal.generation == expected_gen,
                                Withdrawal.status == "reconciliation_required"
                            )
                        )
                        if not db_w:
                            logger.warning("Fenced out! Withdrawal %d", w.id)
                            return
                            
                        # Temporarily mark processing to reuse finalize logic
                        db_w.status = "processing"
                        await session.flush()
                        
                        if found:
                            await wd_service.finalize_payout_success(db_w.id, claim_token, expected_gen, transfer_id)
                        else:
                            # It actually failed
                            await wd_service.finalize_payout_failure(db_w.id, claim_token, expected_gen, "Provider lookup returned nothing.")
                            
                    await run_atomic(_fin)
                except Exception as e:
                    logger.error("Error reconciling withdrawal %d: %s", w.id, e)
        except Exception as e:
            logger.error("Withdrawal reconciler error: %s", e, exc_info=True)
            
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
            invoice_creator_loop(crypto_pay, interval=10),
            name="invoice_creator",
        ),
        asyncio.create_task(
            deposit_reconciler_loop(crypto_pay, interval=60),
            name="deposit_reconciler",
        ),
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
            withdrawal_reconciler_loop(crypto_pay, interval=60),
            name="withdrawal_reconciler",
        ),
        asyncio.create_task(
            task_distribution_loop(telegram_api, interval=300),
            name="task_distributor",
        ),
        asyncio.create_task(
            restriction_reconciliation_loop(telegram_api, interval=15),
            name="restriction_reconciliation",
        ),
        asyncio.create_task(
            notification_sender_loop(telegram_api, interval=5),
            name="notification_sender",
        ),
    ]
    logger.info("Started %d background tasks", len(tasks))
    return tasks


import asyncio
import logging
from typing import cast
import uuid
import platform
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from app.db.models import Deposit
from app.db.engine import run_atomic
from app.utils.time_utils import utc_now, minutes_from_now
from app.integrations.crypto_pay import CryptoPayService, CryptoPayNetworkError
from app.services.notification_service import NotificationService

logger = logging.getLogger(__name__)


async def invoice_creator_loop(
    crypto_pay: CryptoPayService,
    interval: int = 10,
) -> None:
    """
    Background worker that picks up `creation_pending` deposits
    and calls createInvoice on Crypto Pay.
    """
    worker_id = f"invoice_creator_loop@{platform.node()}"
    logger.info("Invoice creator started (interval=%ds, worker_id=%s)", interval, worker_id)

    while True:
        try:
            claim_token = str(uuid.uuid4())
            
            # PHASE 1: Claim pending deposits
            async def _claim(session: AsyncSession):
                now = utc_now()
                # find
                result = await session.execute(
                    select(Deposit)
                    .where(
                        (Deposit.status == "creation_pending") &
                        ((Deposit.lease_expires_at.is_(None)) | (Deposit.lease_expires_at <= now))
                    )
                    .limit(10)
                    .with_for_update(skip_locked=True)
                )
                deposits = list(result.scalars().all())
                if not deposits:
                    return []
                
                ids = [d.id for d in deposits]
                await session.execute(
                    update(Deposit)
                    .where(Deposit.id.in_(ids))
                    .values(
                        worker_id=worker_id,
                        claim_token=claim_token,
                        lease_expires_at=minutes_from_now(5),
                        generation=Deposit.generation + 1
                    )
                )
                
                # return updated
                res = await session.execute(select(Deposit).where(Deposit.id.in_(ids)))
                return list(res.scalars().all())

            claimed = await run_atomic(_claim)
            
            if not claimed:
                await asyncio.sleep(interval)
                continue

            for d in claimed:
                try:
                    # PHASE 2: Create invoice
                    success = False
                    error_msg = None
                    is_ambiguous = False
                    invoice_id = None
                    pay_url = None
                    
                    try:
                        from app.utils.decimal_utils import from_db
                        inv = await crypto_pay.create_invoice(
                            amount=str(from_db(d.amount)),
                            asset=d.asset,
                            description=f"Пополнение баланса #{d.id}",
                            payload=f"deposit:{d.id}",
                            expires_in=3600,
                        )
                        success = True
                        invoice_id = inv.invoice_id
                        pay_url = inv.pay_url
                    except CryptoPayNetworkError as e:
                        error_msg = str(e)
                        is_ambiguous = True
                    except Exception as e:
                        error_msg = str(e)
                        is_ambiguous = False

                    # PHASE 3: Finalize
                    async def _fin(s: AsyncSession):
                        from sqlalchemy import select
                        from app.db.models import User
                        
                        db_dep = await s.scalar(
                            select(Deposit)
                            .where(
                                Deposit.id == d.id,
                                Deposit.status == "creation_pending",
                                Deposit.claim_token == claim_token,
                                Deposit.generation == d.generation
                            )
                        )
                        if not db_dep:
                            logger.warning("Fenced out of deposit %d", d.id)
                            return
                            
                        user = await s.scalar(select(User).where(User.id == db_dep.user_id))
                        
                        if success:
                            db_dep.status = "invoice_created"
                            db_dep.invoice_id = invoice_id
                            db_dep.pay_url = pay_url
                            
                            # Push notification via Outbox
                            if user and pay_url:
                                ns = NotificationService(s)
                                import json
                                rm = json.dumps({
                                    "inline_keyboard": [[{"text": "Оплатить", "url": pay_url}]]
                                })
                                await ns.schedule_notification(
                                    telegram_id=user.telegram_id,
                                    text=f"Счет на пополнение <b>{from_db(db_dep.amount)} {db_dep.asset}</b> создан!\n\nОплатите его по кнопке ниже. Счет действителен 1 час.",
                                    parse_mode="HTML",
                                    reply_markup_json=rm,
                                    dedupe_key=f"deposit_inv_{d.id}"
                                )
                        elif is_ambiguous:
                            db_dep.status = "reconciliation_required"
                        else:
                            db_dep.status = "failed"
                            
                        db_dep.generation += 1
                        
                    await run_atomic(_fin)

                except Exception as e:
                    logger.error("Error processing deposit %d: %s", d.id, e, exc_info=True)

        except Exception as e:
            logger.error("Invoice creator error: %s", e, exc_info=True)
            await asyncio.sleep(interval)


async def deposit_reconciler_loop(
    crypto_pay: CryptoPayService,
    interval: int = 60,
) -> None:
    """
    Reconciles deposits stuck in reconciliation_required.
    """
    worker_id = f"deposit_reconciler_loop@{platform.node()}"
    logger.info("Deposit reconciler started (interval=%ds, worker_id=%s)", interval, worker_id)

    while True:
        try:
            claim_token = str(uuid.uuid4())
            
            async def _claim(s: AsyncSession):
                now = utc_now()
                res = await s.execute(
                    select(Deposit)
                    .where(
                        (Deposit.status == "reconciliation_required") &
                        ((Deposit.lease_expires_at.is_(None)) | (Deposit.lease_expires_at <= now))
                    )
                    .limit(5)
                    .with_for_update(skip_locked=True)
                )
                deps = list(res.scalars().all())
                if not deps: return []
                ids = [d.id for d in deps]
                await s.execute(
                    update(Deposit)
                    .where(Deposit.id.in_(ids))
                    .values(
                        worker_id=worker_id,
                        claim_token=claim_token,
                        lease_expires_at=minutes_from_now(5),
                        generation=Deposit.generation + 1
                    )
                )
                res2 = await s.execute(select(Deposit).where(Deposit.id.in_(ids)))
                return list(res2.scalars().all())

            claimed = await run_atomic(_claim)
            
            if not claimed:
                await asyncio.sleep(interval)
                continue
                
            for d in claimed:
                # PHASE 2: Check CryptoPay for payload match
                # Get recent active invoices
                found_inv = None
                try:
                    active_invs = await crypto_pay.get_invoices(status="active", count=100)
                    for inv in active_invs:
                        if getattr(inv, 'payload', None) == f"deposit:{d.id}":
                            found_inv = inv
                            break
                except Exception as e:
                    logger.warning("Reconciler failed to fetch active invoices: %s", e)
                    continue # Try again later
                
                # PHASE 3:
                async def _fin(s: AsyncSession):
                    db_dep = await s.scalar(
                        select(Deposit)
                        .where(
                            Deposit.id == d.id,
                            Deposit.status == "reconciliation_required",
                            Deposit.claim_token == claim_token,
                            Deposit.generation == d.generation
                        )
                    )
                    if not db_dep:
                        return
                        
                    if found_inv:
                        db_dep.status = "invoice_created"
                        db_dep.invoice_id = found_inv.invoice_id
                        db_dep.pay_url = found_inv.pay_url
                        
                        from app.db.models import User
                        from app.utils.decimal_utils import from_db
                        user = await s.scalar(select(User).where(User.id == db_dep.user_id))
                        if user and db_dep.pay_url:
                            ns = NotificationService(s)
                            import json
                            rm = json.dumps({"inline_keyboard": [[{"text": "Оплатить", "url": db_dep.pay_url}]]})
                            await ns.schedule_notification(
                                telegram_id=user.telegram_id,
                                text=f"Счет на пополнение <b>{from_db(db_dep.amount)} {db_dep.asset}</b> успешно создан после задержки!\n\nОплатите его по кнопке ниже. Счет действителен 1 час.",
                                parse_mode="HTML",
                                reply_markup_json=rm,
                                dedupe_key=f"deposit_inv_{d.id}"
                            )
                    else:
                        # If it's been more than 5 minutes since creation, we can safely consider it failed.
                        from datetime import timezone
                        age = (utc_now().replace(tzinfo=timezone.utc) - db_dep.created_at.replace(tzinfo=timezone.utc)).total_seconds()
                        if age > 300:
                            db_dep.status = "failed"
                    
                    db_dep.generation += 1
                await run_atomic(_fin)

        except Exception as e:
            logger.error("Deposit reconciler error: %s", e, exc_info=True)
            await asyncio.sleep(interval)
