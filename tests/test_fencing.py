import pytest
import uuid
import json
from decimal import Decimal
from app.db.models import Withdrawal, UserRestriction, NotificationOutbox, User, Group, Category
from app.utils.time_utils import utc_now, minutes_from_now
from app.utils.decimal_utils import to_db
from sqlalchemy import select, update
from app.repositories.withdrawal_repo import WithdrawalRepository
from app.repositories.restriction_repo import RestrictionRepository
from app.repositories.notification_repo import NotificationRepository
from app.services.withdrawal_service import WithdrawalService
from app.services.restriction_service import RestrictionService

pytestmark = pytest.mark.asyncio

async def test_withdrawal_fencing_stale_worker(db_session):
    """
    Test that a stale worker cannot finalize a withdrawal if it has been claimed by another worker.
    """
    # Create withdrawal
    w = Withdrawal(
        user_id=1,
        recipient_telegram_id=123,
        amount=to_db(Decimal("10.0")),
        asset="USDT",
        status="processing",
        worker_id="worker_1",
        claim_token="token_1",
        lease_expires_at=minutes_from_now(5),
        generation=1
    )
    db_session.add(w)
    await db_session.flush()

    # Worker 1 prepares payout
    service = WithdrawalService(db_session)
    prep = await service.prepare_payout(w.id, "worker_1", "token_1", 1)
    assert prep is not None
    await db_session.flush()

    # Emulate lease expiration and worker 2 claiming it
    await db_session.execute(
        update(Withdrawal)
        .where(Withdrawal.id == w.id)
        .values(
            worker_id="worker_2",
            claim_token="token_2",
            generation=3 # worker_2 claim bumped generation
        )
    )
    await db_session.flush()

    # Worker 1 tries to finalize success (it expects generation 2 because prepare_payout bumped it from 1 to 2)
    # But generation is now 3
    ok = await service.finalize_payout_success(w.id, "token_1", 2, transfer_id=999)
    assert not ok

    db_w = await db_session.scalar(select(Withdrawal).where(Withdrawal.id == w.id))
    assert db_w.status == "processing" # Not completed!
    assert db_w.transfer_id is None

async def test_restriction_fencing_operation_token_change(db_session, mock_telegram_api):
    """
    Test that if desired_state changes while HTTP is inflight, we don't apply the old actual_state.
    """
    repo = RestrictionRepository(db_session)
    
    r = UserRestriction(
        group_id=1,
        user_telegram_id=123,
        desired_state="ON",
        actual_state="OFF",
        operation_token=1,
        generation=1,
        worker_id="worker_1",
        claim_token="token_1",
        lease_expires_at=minutes_from_now(5),
        restricted_by_bot=True
    )
    db_session.add(r)
    await db_session.flush()

    # While worker_1 is doing HTTP, a handler changes desired_state to OFF
    await repo.remove_restriction(1, 123)
    await db_session.flush()

    db_r = await db_session.scalar(select(UserRestriction).where(UserRestriction.id == r.id))
    assert db_r.desired_state == "OFF"
    assert db_r.operation_token == 2

    # Worker 1 finishes HTTP and tries to finalize
    service = RestrictionService(db_session, mock_telegram_api)
    res = await service.finalize_reconciliation(r.id, "token_1", 1, True, current_op_token=1)
    
    # Should be fenced out due to operation token mismatch
    assert not res["ok"]
    assert "Operation token mismatch" in res["error"]

    db_r_after = await db_session.scalar(select(UserRestriction).where(UserRestriction.id == r.id))
    assert db_r_after.actual_state == "OFF" # NOT ON!

async def test_notification_fencing_stale_worker(db_session):
    """
    Test that a stale notification sender cannot finalize an outbox message.
    """
    repo = NotificationRepository(db_session)

    n = NotificationOutbox(
        telegram_id=123,
        text="Hello",
        status="processing",
        worker_id="worker_1",
        claim_token="token_1",
        generation=1,
        lease_expires_at=minutes_from_now(5)
    )
    db_session.add(n)
    await db_session.flush()

    # Lease expires, worker 2 claims it
    await db_session.execute(
        update(NotificationOutbox)
        .where(NotificationOutbox.id == n.id)
        .values(
            worker_id="worker_2",
            claim_token="token_2",
            generation=2
        )
    )
    await db_session.flush()

    # Worker 1 tries to finalize
    fenced = await repo.mark_sent(n.id, expected_generation=1, claim_token="token_1")
    assert not fenced

    db_n = await db_session.scalar(select(NotificationOutbox).where(NotificationOutbox.id == n.id))
    assert db_n.status == "processing" # Remains processing for worker_2
