"""
Common handlers: /start, /help, user registration middleware.
"""

from __future__ import annotations

import logging

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.user_service import UserService
from app.services.balance_service import BalanceService
from app.repositories.settings_repo import TextRepository
from app.config import config
from app.utils.decimal_utils import format_amount_plain

logger = logging.getLogger(__name__)

common_router = Router(name="common")


# ── /start ───────────────────────────────────────────────────────────────────

@common_router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, state: FSMContext) -> None:
    """Handle /start — register user + show main menu."""
    await state.clear()
    user_service = UserService(session)
    user = await user_service.get_or_create(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )

    # Check if blocked
    if user.is_blocked:
        await message.answer(
            "⛔ Ваш аккаунт заблокирован.\n"
            f"Причина: {user.block_reason or 'не указана'}",
        )
        return

    text_repo = TextRepository(session)
    welcome = await text_repo.get_text("welcome")

    kb = _main_menu_keyboard(is_admin=message.from_user.id == config.admin_telegram_id)
    await message.answer(welcome, reply_markup=kb)


# ── /help ────────────────────────────────────────────────────────────────────

@common_router.message(Command("help"))
async def cmd_help(message: Message, session: AsyncSession) -> None:
    """Show help text."""
    text_repo = TextRepository(session)
    help_text = await text_repo.get_text("help")
    support = config.support_username or "поддержку"
    await message.answer(help_text.replace("{support}", support))


# ── /balance ─────────────────────────────────────────────────────────────────

@common_router.message(Command("balance"))
async def cmd_balance(message: Message, session: AsyncSession) -> None:
    """Show user balance."""
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(message.from_user.id)
    if not user:
        await message.answer("Используйте /start для регистрации.")
        return

    balance_service = BalanceService(session)
    available, reserved = await balance_service.get_balance(user.id)
    total = available + reserved

    text_repo = TextRepository(session)
    tpl = await text_repo.get_text("balance_info")
    text = tpl.format(
        available=format_amount_plain(available),
        reserved=format_amount_plain(reserved),
        total=format_amount_plain(total),
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Пополнить", callback_data="deposit:start")
    kb.button(text="📤 Вывести", callback_data="withdraw:start")
    kb.button(text="📊 История", callback_data="ledger:list:1")
    kb.adjust(2, 1)

    await message.answer(text, reply_markup=kb.as_markup())


# ── Back to main menu callback ──────────────────────────────────────────────

@common_router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    """Return to main menu."""
    await state.clear()
    text_repo = TextRepository(session)
    welcome = await text_repo.get_text("welcome")
    kb = _main_menu_keyboard(
        is_admin=callback.from_user.id == config.admin_telegram_id,
    )
    await callback.message.edit_text(welcome, reply_markup=kb)
    await callback.answer()


# ── Keyboard builders ────────────────────────────────────────────────────────

def _main_menu_keyboard(is_admin: bool = False):
    """Build main menu inline keyboard."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 Купить подписчиков", callback_data="buyer:menu")
    kb.button(text="💰 Заработать", callback_data="seller:menu")
    kb.button(text="💳 Баланс", callback_data="balance:show")
    kb.button(text="❓ Помощь", callback_data="help:show")
    if is_admin:
        kb.button(text="⚙️ Админ-панель", callback_data="admin:menu")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


# ── Balance show callback ───────────────────────────────────────────────────

@common_router.callback_query(F.data == "balance:show")
async def cb_balance_show(callback: CallbackQuery, session: AsyncSession) -> None:
    """Show balance inline."""
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)
    if not user:
        await callback.answer("Используйте /start", show_alert=True)
        return

    balance_service = BalanceService(session)
    available, reserved = await balance_service.get_balance(user.id)
    total = available + reserved

    text_repo = TextRepository(session)
    tpl = await text_repo.get_text("balance_info")
    text = tpl.format(
        available=format_amount_plain(available),
        reserved=format_amount_plain(reserved),
        total=format_amount_plain(total),
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Пополнить", callback_data="deposit:start")
    kb.button(text="📤 Вывести", callback_data="withdraw:start")
    kb.button(text="📊 История", callback_data="ledger:list:1")
    kb.button(text="◀️ Назад", callback_data="main_menu")
    kb.adjust(2, 1, 1)

    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await callback.answer()



# ── Ledger / Transaction history ─────────────────────────────────────────────

_LEDGER_PAGE_SIZE = 10

_OP_LABELS = {
    "deposit":           "💳 Пополнение",
    "campaign_reserve":  "🔒 Резерв кампании",
    "campaign_spend":    "💸 Списание за задание",
    "reserve_release":   "↩️ Возврат резерва",
    "seller_reward":     "🏆 Вознаграждение",
    "withdrawal_reserve":"🔒 Резерв вывода",
    "withdrawal_payout": "📤 Вывод",
    "withdrawal_release":"↩️ Возврат вывода",
    "refund":            "💰 Возврат",
    "admin_adjustment":  "⚙️ Корректировка",
}


@common_router.callback_query(F.data.startswith("ledger:list:"))
async def cb_ledger_list(callback: CallbackQuery, session: AsyncSession) -> None:
    """Show paginated transaction history."""
    page = int(callback.data.split(":")[2])
    if page < 1:
        page = 1

    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)
    if not user:
        await callback.answer("Используйте /start", show_alert=True)
        return

    from app.repositories.ledger_repo import LedgerRepository
    ledger_repo = LedgerRepository(session)

    offset = (page - 1) * _LEDGER_PAGE_SIZE
    entries = await ledger_repo.get_by_user(
        user_id=user.id,
        offset=offset,
        limit=_LEDGER_PAGE_SIZE,
    )
    total = await ledger_repo.count_by_user(user.id)
    total_pages = max(1, (total + _LEDGER_PAGE_SIZE - 1) // _LEDGER_PAGE_SIZE)

    if not entries:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data="balance:show")
        await callback.message.edit_text(
            "📊 <b>История транзакций</b>\n\nОпераций пока нет.",
            reply_markup=kb.as_markup(),
        )
        await callback.answer()
        return

    from app.utils.time_utils import format_datetime
    from app.utils.decimal_utils import from_db

    lines = [f"📊 <b>История транзакций</b> (стр. {page}/{total_pages})\n"]
    for e in entries:
        label = _OP_LABELS.get(e.operation_type, e.operation_type)
        amount = from_db(e.amount)
        sign = "+" if e.direction == "credit" else "−"
        date_str = format_datetime(e.created_at)
        lines.append(f"{sign}<code>{amount:.2f}</code> USDT  {label}\n  <i>{date_str}</i>")

    kb = InlineKeyboardBuilder()
    nav = []
    if page > 1:
        nav.append(
            kb.button(text="◀️ Назад", callback_data=f"ledger:list:{page - 1}")
        )
    if page < total_pages:
        nav.append(
            kb.button(text="Вперёд ▶️", callback_data=f"ledger:list:{page + 1}")
        )
    kb.button(text="↩️ К балансу", callback_data="balance:show")
    if nav:
        kb.adjust(2, 1)
    else:
        kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@common_router.callback_query(F.data == "help:show")
async def cb_help_show(callback: CallbackQuery, session: AsyncSession) -> None:
    """Show help inline."""
    text_repo = TextRepository(session)
    help_text = await text_repo.get_text("help")
    support = config.support_username or "поддержку"

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data="main_menu")

    await callback.message.edit_text(
        help_text.replace("{support}", support),
        reply_markup=kb.as_markup(),
    )
    await callback.answer()
