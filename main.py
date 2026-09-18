"""
AdverRocketBot entry point.

Startup sequence:
1. Ensure data directories exist
2. Run pending DB migrations
3. Seed default settings & texts
4. Initialize Crypto Pay client
5. Register handlers & middleware
6. Start polling
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from app.config import config, setup_logging
from app.db.engine import engine, session_factory
from app.db.migrations import run_migrations
from app.db.models import Base
from app.integrations.crypto_pay import CryptoPayService
from app.integrations.telegram_api import TelegramAPIService

logger = logging.getLogger(__name__)

# ── Bot & Dispatcher ─────────────────────────────────────────────────────────

bot = Bot(
    token=config.bot_token,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

dp = Dispatcher()

# ── Global services (initialized at startup) ────────────────────────────────

crypto_pay: CryptoPayService | None = None
telegram_api: TelegramAPIService | None = None


# ── Startup ──────────────────────────────────────────────────────────────────

async def on_startup() -> None:
    """Run on bot startup."""
    global crypto_pay, telegram_api

    # 1. Ensure data directory
    config.ensure_dirs()
    logger.info("Data directory: %s", config.data_dir)

    # 2. (Removed create_all; Alembic is the only source of truth)
    # 3. Run migrations
    await run_migrations()

    # 4. Seed default settings
    await _seed_defaults()

    # 5. Initialize Crypto Pay
    crypto_pay = CryptoPayService(
        api_token=config.crypto_pay_api_token,
        base_url=config.crypto_pay_api_url,
    )
    await crypto_pay.start()

    # Health check
    cp_ok = await crypto_pay.health_check()
    logger.info("Crypto Pay health: %s", "OK" if cp_ok else "FAIL")

    # 6. Telegram API service
    telegram_api = TelegramAPIService(bot)
    tg_ok = await telegram_api.health_check()
    logger.info("Telegram API health: %s", "OK" if tg_ok else "FAIL")

    # 7. Start background tasks
    from app.background import start_background_tasks
    background_tasks = start_background_tasks(crypto_pay, telegram_api)

    logger.info("🚀 AdverRocketBot started")


async def on_shutdown() -> None:
    """Cleanup on bot shutdown."""
    if crypto_pay:
        await crypto_pay.close()

    await engine.dispose()
    logger.info("AdverRocketBot stopped")


# ── Default settings & texts seeding ─────────────────────────────────────────

async def _seed_defaults() -> None:
    """Seed default bot_settings and bot_texts on first run."""
    from app.repositories.settings_repo import SettingsRepository, TextRepository

    async with session_factory() as session:
        settings_repo = SettingsRepository(session)
        text_repo = TextRepository(session)

        # Default settings
        defaults = {
            "min_deposit": ("1.00", "Минимальная сумма пополнения (USDT)"),
            "min_withdrawal": ("5.00", "Минимальная сумма вывода (USDT)"),
            "max_withdrawal": ("1000.00", "Максимальная сумма вывода (USDT)"),
            "cancel_commission_percent": ("10", "Комиссия за отмену кампании (%)"),
            "task_ttl_hours": ("24", "Время жизни задания (часов)"),
            "max_campaigns_per_user": ("10", "Макс. активных кампаний на пользователя"),
            "max_groups_per_user": ("20", "Макс. групп на продавца"),
            "rights_check_interval_min": ("15", "Интервал проверки прав бота (мин)"),
            "invoice_ttl_seconds": ("3600", "Время жизни инвойса (секунд)"),
            "default_repeat_cooldown_min": ("1440", "Кулдаун повторного задания (мин, 24ч)"),
        }
        for key, (value, desc) in defaults.items():
            existing = await settings_repo.get_value(key)
            if existing is None:
                await settings_repo.set_value(key, value, desc)

        # Default texts
        default_texts = {
            "welcome": (
                "👋 Добро пожаловать в <b>AdverRocketBot</b>!\n\n"
                "🛒 <b>Покупатель</b> — покупай подписчиков для своих каналов/групп.\n"
                "💰 <b>Продавец</b> — зарабатывай на подписках в своих группах.\n\n"
                "Выберите действие в меню ниже:"
            ),
            "help": (
                "❓ <b>Справка</b>\n\n"
                "• /start — Главное меню\n"
                "• Все остальные действия (Баланс, Кампании, Ввод/Вывод) доступны через кнопки в главном меню.\n\n"
                "По вопросам: @{support}"
            ),
            "balance_info": (
                "💰 <b>Ваш баланс</b>\n\n"
                "Доступно: <code>{available}</code> USDT\n"
                "В резерве: <code>{reserved}</code> USDT\n"
                "В холде: <code>{held}</code> USDT\n"
                "Итого: <code>{total}</code> USDT"
            ),
            "no_active_tasks": "📭 У вас нет активных заданий.",
            "no_active_campaigns": "📭 У вас нет активных кампаний.",
            "task_completed": (
                "✅ Задание выполнено!\n\n"
                "Вознаграждение: <code>{reward}</code> USDT\n"
                "Прогресс кампании: {completed}/{target}"
            ),
            "campaign_created": (
                "🚀 Кампания создана!\n\n"
                "📌 {target_title}\n"
                "🎯 Цель: {target} подписчиков\n"
                "💰 Стоимость: {cost} USDT\n"
                "📂 Категория: {category}"
            ),
            "deposit_created": (
                "💳 Счёт на оплату создан!\n\n"
                "Сумма: <code>{amount}</code> USDT\n"
                "Нажмите кнопку ниже для оплаты."
            ),
            "withdrawal_requested": (
                "📤 Заявка на вывод создана.\n\n"
                "Сумма: <code>{amount}</code> USDT\n"
                "Статус: ожидает одобрения"
            ),
            "task_assigned": (
                "📋 <b>Новое задание!</b>\n\n"
                "Подпишитесь на <b>{target_title}</b> и нажмите кнопку ниже.\n"
                "🏆 Награда: <code>{reward}</code> USDT\n\n"
                "После подтверждения подписки вы получите доступ к "
                "написанию сообщений в группе <b>{group_title}</b>."
            ),
        }
        for key, text_value in default_texts.items():
            from sqlalchemy import select
            from app.db.models import BotText
            result = await session.execute(
                select(BotText).where(BotText.key == key)
            )
            existing = result.scalar_one_or_none()
            if existing is None:
                bt = BotText(
                    key=key,
                    text=text_value,
                    default_text=text_value,
                )
                session.add(bt)

        await session.commit()
        logger.info("Default settings & texts seeded")


# ── Middleware: session per update ────────────────────────────────────────────

from aiogram import BaseMiddleware
from aiogram.types import Update


class SessionMiddleware(BaseMiddleware):
    """Inject AsyncSession into handler data for each update."""

    async def __call__(self, handler, event: Update, data: dict):
        async with session_factory() as session:
            data["session"] = session
            data["crypto_pay"] = crypto_pay
            data["telegram_api"] = telegram_api
            try:
                result = await handler(event, data)
                # DO NOT COMMIT here.
                # Handlers MUST NOT mutate ORM objects and depend on middleware commit.
                # All persistent writes must go exclusively through run_atomic().
                return result
            except Exception as e:
                await session.rollback()
                logger.exception("Exception in update %s", event.update_id)
                
                try:
                    from app.repositories.error_repo import SystemErrorRepository
                    error_repo = SystemErrorRepository(session)
                    await error_repo.log_error(
                        error_type=type(e).__name__,
                        message=str(e),
                        severity="high"
                    )
                    await session.commit()
                except Exception as db_e:
                    logger.error("Failed to log error to DB: %s", db_e)

                try:
                    if event.message:
                        await event.message.answer("⚠️ Произошла системная ошибка. Мы уже работаем над её устранением.")
                    elif event.callback_query:
                        await event.callback_query.message.answer("⚠️ Произошла системная ошибка. Мы уже работаем над её устранением.")
                        await event.callback_query.answer()
                except Exception:
                    pass
                return None


class BlockedUserMiddleware(BaseMiddleware):
    """Block processing if user is banned."""
    
    async def __call__(self, handler, event: Update, data: dict):
        session = data.get("session")
        if not session:
            return await handler(event, data)

        tg_user = None
        if event.message:
            tg_user = event.message.from_user
        elif event.callback_query:
            tg_user = event.callback_query.from_user

        if tg_user:
            from app.services.user_service import UserService
            user_service = UserService(session)
            user = await user_service.get_by_telegram_id(tg_user.id)
            if user and user.is_blocked:
                if event.message:
                    await event.message.answer(
                        f"⛔ Ваш аккаунт заблокирован.\nПричина: {user.block_reason or 'не указана'}"
                    )
                elif event.callback_query:
                    await event.callback_query.answer(
                        f"⛔ Аккаунт заблокирован: {user.block_reason or 'нет причины'}", show_alert=True
                    )
                return  # Drop the update
        
        return await handler(event, data)


dp.update.outer_middleware(SessionMiddleware())
dp.update.outer_middleware(BlockedUserMiddleware())


# ── Register routers ─────────────────────────────────────────────────────────

from app.handlers.common import common_router
from app.handlers.buyer import buyer_router
from app.handlers.seller import seller_router
from app.handlers.admin import admin_router

dp.include_router(common_router)
dp.include_router(buyer_router)
dp.include_router(seller_router)
dp.include_router(admin_router)


# ── Main ─────────────────────────────────────────────────────────────────────

async def main() -> None:
    """Entry point."""
    setup_logging(config.log_level)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    logger.info("Starting AdverRocketBot polling...")
    # Explicitly include chat_member to trigger join/leave events
    allowed = dp.resolve_used_update_types()
    if "chat_member" not in allowed:
        allowed.append("chat_member")
    await dp.start_polling(bot, allowed_updates=allowed)


if __name__ == "__main__":
    asyncio.run(main())
