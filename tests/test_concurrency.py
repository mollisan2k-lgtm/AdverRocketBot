import asyncio
import os
import pytest
from decimal import Decimal
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from app.db.models import Base, User, Campaign, BalanceLedger, Category, SellerGroup
from app.services.campaign_service import CampaignService
from app.services.balance_service import BalanceService
from app.services.user_service import UserService
from app.db.engine import run_atomic, _set_sqlite_pragmas
from sqlalchemy import event

# Setup isolated engine
DB_URL = "sqlite+aiosqlite:///file:test_concurrency.db?mode=rwc&uri=true"
test_engine = create_async_engine(DB_URL, echo=False, pool_pre_ping=True, connect_args={"check_same_thread": False})
event.listen(test_engine.sync_engine, "connect", _set_sqlite_pragmas)
test_session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

# Monkey-patch engine for run_atomic
import app.db.engine as engine_module
engine_module.engine = test_engine
engine_module.session_factory = test_session_factory

import pytest_asyncio

@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    # Cleanup after test
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

@pytest.mark.asyncio
async def test_concurrent_balance_update():
    # Setup test user
    async with test_session_factory() as session:
        user = User(telegram_id=101010)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        user_id = user.id

    async def add_funds(amount: str):
        async def _op(session: AsyncSession):
            balance_service = BalanceService(session)
            await balance_service.admin_adjustment(
                user_id=user_id,
                amount=Decimal(amount),
                reason="Test deposit",
                admin_telegram_id=1,
                idempotency_key=f"dep_{amount}_{asyncio.current_task().get_name()}"
            )
            return True
        return await run_atomic(_op)

    # Launch 50 concurrent balance additions
    tasks = [asyncio.create_task(add_funds("10.00"), name=str(i)) for i in range(50)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Verify no failures
    for res in results:
        assert res is True, f"Operation failed: {res}"

    # Verify final balance is 500.00
    async with test_session_factory() as session:
        balance_service = BalanceService(session)
        available, reserved, held = await balance_service.get_balance(user_id)
        assert available == Decimal("500.00")


@pytest.mark.asyncio
async def test_concurrent_campaign_completions():
    # Setup
    from app.services.task_service import TaskService
    async with test_session_factory() as session:
        buyer = User(telegram_id=202020)
        cat = Category(name="TestCat", buyer_price="1.00", seller_payout="0.50")
        session.add_all([buyer, cat])
        await session.commit()
        await session.refresh(buyer)
        await session.refresh(cat)

        group = SellerGroup(
            user_id=buyer.id,
            telegram_chat_id=-100987654321,
            title="TestGroup",
            category_id=cat.id,
            interval_minutes=10,
            status="approved"
        )
        session.add(group)
        
        # Create 50 seller users
        sellers = [User(telegram_id=300000 + i) for i in range(50)]
        session.add_all(sellers)
        await session.commit()
        await session.refresh(group)
        
        balance_service = BalanceService(session)
        await balance_service.admin_adjustment(
            user_id=buyer.id, amount=Decimal("100.00"), reason="Initial", admin_telegram_id=1, idempotency_key="init_dep"
        )
        
        campaign_service = CampaignService(session)
        campaign = await campaign_service.create_campaign(
            user_id=buyer.id, category_id=cat.id, target=50,
            target_type="channel", target_chat_id=-100123456789,
            target_username="test", target_title_snapshot="Test", target_link="https://t.me/test"
        )
        await session.commit()
        campaign_id = campaign.id
        group_id = group.id

    # Insert tasks directly to bypass assign_task validations (we only test completion concurrency)
    from app.db.models import CampaignTask
    task_ids = []
    async with test_session_factory() as session:
        for s in sellers:
            task = CampaignTask(
                campaign_id=campaign_id,
                group_id=group_id,
                user_telegram_id=s.telegram_id,
                target_chat_id=-100123456789,
                interval_minutes_snapshot=10,
                status="active"
            )
            session.add(task)
            await session.commit()
            task_ids.append(task.id)

    # Launch 50 completions concurrently
    async def complete_sub(t_id: int):
        async def _op(session: AsyncSession):
            # Bypass real telegram API check in verify_and_complete by mocking it or manually calling what it does
            # Wait, verify_and_complete calls telegram API! We can't do that in test.
            # Let's just bypass verify_and_complete and do the internal steps:
            task_service = TaskService(session)
            db_task = await task_service.get_task_by_id(t_id)
            db_task.status = "completed"
            
            from app.services.user_service import UserService
            user_service = UserService(session)
            seller = await user_service.get_by_telegram_id(db_task.user_telegram_id)
            
            # Pay out
            balance_service = BalanceService(session)
            await balance_service.credit_seller_reward(
                user_id=seller.id,
                amount=Decimal("0.50"),
                campaign_id=campaign_id,
                task_id=t_id,
                idempotency_key=f"pay_{t_id}"
            )
            
            # Increment campaign
            campaign_service = CampaignService(session)
            camp = await campaign_service.get_by_id(campaign_id)
            camp.completed += 1
            if camp.completed >= camp.target:
                await task_service._finalize_campaign_completion(camp)
            return True
        return await run_atomic(_op)

    tasks = [asyncio.create_task(complete_sub(tid), name=str(tid)) for tid in task_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    success_count = sum(1 for r in results if r is True)
    assert success_count == 50, f"Expected 50 successes, got {success_count}. Results: {results}"

    async with test_session_factory() as session:
        campaign_service = CampaignService(session)
        campaign = await campaign_service.get_by_id(campaign_id)
        assert campaign.completed == 50
        assert campaign.status == "completed"
