"""
Admin handlers: admin panel, moderation, settings, monitoring.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.user_service import UserService
from app.services.campaign_service import CampaignService
from app.services.category_service import CategoryService
from app.services.group_service import GroupService
from app.services.withdrawal_service import WithdrawalService
from app.services.balance_service import BalanceService
from app.repositories.settings_repo import SettingsRepository, TextRepository
from app.repositories.audit_repo import AuditLogRepository
from app.repositories.error_repo import SystemErrorRepository
from app.repositories.withdrawal_repo import WithdrawalRepository
from app.integrations.telegram_api import TelegramAPIService
from app.integrations.crypto_pay import CryptoPayService
from app.utils.decimal_utils import from_db, format_amount_plain, round_down
from app.config import config

logger = logging.getLogger(__name__)

admin_router = Router(name="admin")


# ── Admin check filter ───────────────────────────────────────────────────────

def _is_admin(user_id: int) -> bool:
    return user_id == config.admin_telegram_id


# ── FSM States ───────────────────────────────────────────────────────────────

class CategoryCreation(StatesGroup):
    entering_name = State()
    entering_buyer_price = State()
    entering_seller_payout = State()


class AdminBalanceAdjust(StatesGroup):
    entering_user = State()
    entering_amount = State()
    entering_reason = State()


class AdminUserManagement(StatesGroup):
    entering_query = State()


class AdminSettingsEdit(StatesGroup):
    entering_value = State()

class AdminTextEdit(StatesGroup):
    entering_text = State()


# ── Admin menu ───────────────────────────────────────────────────────────────

@admin_router.callback_query(F.data == "admin:menu")
async def cb_admin_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    """Show admin panel."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return

    # Dashboard counters
    wd_repo = WithdrawalRepository(session)
    error_repo = SystemErrorRepository(session)
    pending_wd = await wd_repo.count_pending()
    critical_errors = await error_repo.count_critical()

    from app.repositories.group_repo import GroupRepository
    group_repo = GroupRepository(session)
    pending_groups = await group_repo.get_pending()

    text = (
        "⚙️ <b>Админ-панель</b>\n\n"
        f"📤 Выводы на рассмотрении: {pending_wd}\n"
        f"👥 Группы на модерации: {len(pending_groups)}\n"
        f"🚨 Критических ошибок: {critical_errors}\n"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text=f"📤 Выводы ({pending_wd})", callback_data="admin:withdrawals")
    kb.button(text=f"👥 Модерация ({len(pending_groups)})", callback_data="admin:moderation")
    kb.button(text="📂 Категории", callback_data="admin:categories")
    kb.button(text=f"🚨 Ошибки ({critical_errors})", callback_data="admin:errors")
    kb.button(text="⚙️ Настройки", callback_data="admin:settings")
    kb.button(text="📝 Тексты бота", callback_data="admin:texts")
    kb.button(text="👤 Пользователи", callback_data="admin:users")
    kb.button(text="💰 Корректировка баланса", callback_data="admin:adjust")
    kb.button(text="◀️ Назад", callback_data="main_menu")
    kb.adjust(2, 2, 2, 2, 1)

    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await callback.answer()


# ── Withdrawal moderation ────────────────────────────────────────────────────

@admin_router.callback_query(F.data == "admin:withdrawals")
async def cb_admin_withdrawals(callback: CallbackQuery, session: AsyncSession) -> None:
    """List pending withdrawals."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return

    wd_repo = WithdrawalRepository(session)
    pending = await wd_repo.get_pending()

    if not pending:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data="admin:menu")
        await callback.message.edit_text(
            "✅ Нет заявок на вывод.", reply_markup=kb.as_markup(),
        )
        await callback.answer()
        return

    text_parts = ["📤 <b>Заявки на вывод</b>\n"]
    for w in pending[:20]:
        text_parts.append(
            f"#{w.id} · {format_amount_plain(from_db(w.amount))} USDT · user={w.user_id}"
        )

    kb = InlineKeyboardBuilder()
    for w in pending[:10]:
        kb.button(
            text=f"#{w.id} — {format_amount_plain(from_db(w.amount))} USDT",
            callback_data=f"admin:wd:{w.id}",
        )
    kb.button(text="◀️ Назад", callback_data="admin:menu")
    kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(text_parts), reply_markup=kb.as_markup(),
    )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:wd:"))
async def cb_admin_wd_view(callback: CallbackQuery, session: AsyncSession) -> None:
    """View withdrawal for approval/rejection."""
    if not _is_admin(callback.from_user.id):
        return

    wd_id = int(callback.data.split(":")[2])
    wd_repo = WithdrawalRepository(session)
    w = await wd_repo.get_by_id(wd_id)
    if not w:
        await callback.answer("Не найдена.", show_alert=True)
        return

    user_service = UserService(session)
    user = await user_service.get_by_id(w.user_id)

    text = (
        f"📤 <b>Вывод #{w.id}</b>\n\n"
        f"Пользователь: {user.username or user.telegram_id if user else '?'}\n"
        f"Сумма: <code>{format_amount_plain(from_db(w.amount))}</code> USDT\n"
        f"Статус: {w.status}\n"
    )

    kb = InlineKeyboardBuilder()
    if w.status == "pending":
        kb.button(text="✅ Одобрить", callback_data=f"admin:wd_approve:{wd_id}")
        kb.button(text="❌ Отклонить", callback_data=f"admin:wd_reject:{wd_id}")
    kb.button(text="◀️ Назад", callback_data="admin:withdrawals")
    kb.adjust(2, 1)

    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:wd_approve:"))
async def cb_admin_wd_approve(
    callback: CallbackQuery, session: AsyncSession,
    crypto_pay: CryptoPayService | None,
) -> None:
    """Approve and process withdrawal."""
    if not _is_admin(callback.from_user.id):
        return

    wd_id = int(callback.data.split(":")[2])
    wd_service = WithdrawalService(session, crypto_pay)

    ok = await wd_service.approve(wd_id)
    if not ok:
        await callback.answer("Не удалось одобрить.", show_alert=True)
        return

    # Audit log
    audit_repo = AuditLogRepository(session)
    await audit_repo.log_action(
        actor_telegram_id=callback.from_user.id,
        action="withdrawal_approved",
        object_type="withdrawal",
        object_id=wd_id,
    )

    await callback.answer("✅ Вывод одобрен")
    await cb_admin_withdrawals(callback, session)


@admin_router.callback_query(F.data.startswith("admin:wd_reject:"))
async def cb_admin_wd_reject(callback: CallbackQuery, session: AsyncSession) -> None:
    """Reject withdrawal (simplified — uses default reason)."""
    if not _is_admin(callback.from_user.id):
        return

    wd_id = int(callback.data.split(":")[2])
    wd_service = WithdrawalService(session)
    ok = await wd_service.reject(wd_id, "Отклонено администратором")

    if ok:
        audit_repo = AuditLogRepository(session)
        await audit_repo.log_action(
            actor_telegram_id=callback.from_user.id,
            action="withdrawal_rejected",
            object_type="withdrawal",
            object_id=wd_id,
        )

    await callback.answer("❌ Вывод отклонён" if ok else "Ошибка")
    await cb_admin_withdrawals(callback, session)


# ── Group moderation ─────────────────────────────────────────────────────────

@admin_router.callback_query(F.data == "admin:moderation")
async def cb_admin_moderation(
    callback: CallbackQuery, session: AsyncSession,
    telegram_api: TelegramAPIService,
) -> None:
    """Show pending groups for moderation."""
    if not _is_admin(callback.from_user.id):
        return

    group_service = GroupService(session, telegram_api)
    pending = await group_service.get_pending()

    if not pending:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data="admin:menu")
        await callback.message.edit_text(
            "✅ Нет групп на модерации.", reply_markup=kb.as_markup(),
        )
        await callback.answer()
        return

    text_parts = ["👥 <b>Группы на модерации</b>\n"]
    for g in pending:
        text_parts.append(
            f"#{g.id} · {g.title or g.telegram_chat_id} · "
            f"Участники: {g.member_count or '?'}"
        )

    kb = InlineKeyboardBuilder()
    for g in pending[:10]:
        kb.button(
            text=f"#{g.id} {g.title or '?'}",
            callback_data=f"admin:group_mod:{g.id}",
        )
    kb.button(text="◀️ Назад", callback_data="admin:menu")
    kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(text_parts), reply_markup=kb.as_markup(),
    )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:group_mod:"))
async def cb_admin_group_mod(
    callback: CallbackQuery, session: AsyncSession,
    telegram_api: TelegramAPIService,
) -> None:
    """View group for moderation."""
    if not _is_admin(callback.from_user.id):
        return

    group_id = int(callback.data.split(":")[2])
    group_service = GroupService(session, telegram_api)
    group = await group_service.get_by_id(group_id)

    if not group:
        await callback.answer("Не найдена.", show_alert=True)
        return

    text = (
        f"👥 <b>{group.title or group.telegram_chat_id}</b>\n\n"
        f"ID чата: <code>{group.telegram_chat_id}</code>\n"
        f"Участники: {group.member_count or '?'}\n"
        f"Категория: {group.category.name if group.category else '?'}\n"
        f"Права бота: {'✅' if group.bot_has_rights else '❌'}\n"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить", callback_data=f"admin:group_approve:{group_id}")
    kb.button(text="❌ Отклонить", callback_data=f"admin:group_reject:{group_id}")
    kb.button(text="◀️ Назад", callback_data="admin:moderation")
    kb.adjust(2, 1)

    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:group_approve:"))
async def cb_admin_group_approve(
    callback: CallbackQuery, session: AsyncSession,
    telegram_api: TelegramAPIService,
) -> None:
    if not _is_admin(callback.from_user.id):
        return

    group_id = int(callback.data.split(":")[2])
    group_service = GroupService(session, telegram_api)
    ok = await group_service.approve(group_id)

    if ok:
        audit_repo = AuditLogRepository(session)
        await audit_repo.log_action(
            actor_telegram_id=callback.from_user.id,
            action="group_approved",
            object_type="seller_group",
            object_id=group_id,
        )

    await callback.answer("✅ Группа одобрена" if ok else "Ошибка")
    await cb_admin_moderation(callback, session, telegram_api)


@admin_router.callback_query(F.data.startswith("admin:group_reject:"))
async def cb_admin_group_reject(
    callback: CallbackQuery, session: AsyncSession,
    telegram_api: TelegramAPIService,
) -> None:
    if not _is_admin(callback.from_user.id):
        return

    group_id = int(callback.data.split(":")[2])
    group_service = GroupService(session, telegram_api)
    ok = await group_service.reject(group_id, "Отклонено администратором")

    if ok:
        audit_repo = AuditLogRepository(session)
        await audit_repo.log_action(
            actor_telegram_id=callback.from_user.id,
            action="group_rejected",
            object_type="seller_group",
            object_id=group_id,
        )

    await callback.answer("❌ Группа отклонена" if ok else "Ошибка")
    await cb_admin_moderation(callback, session, telegram_api)


# ── Categories management ───────────────────────────────────────────────────

@admin_router.callback_query(F.data == "admin:categories")
async def cb_admin_categories(callback: CallbackQuery, session: AsyncSession) -> None:
    """List all categories."""
    if not _is_admin(callback.from_user.id):
        return

    cat_service = CategoryService(session)
    categories = await cat_service.get_all()

    text_parts = ["📂 <b>Категории</b>\n"]
    for c in categories:
        status = {"active": "✅", "disabled": "⛔", "archived": "📦"}.get(c.status, "?")
        text_parts.append(
            f"{status} {c.name}\n"
            f"   Покупка: {format_amount_plain(from_db(c.buyer_price))} · "
            f"Выплата: {format_amount_plain(from_db(c.seller_payout))}"
        )

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Создать", callback_data="admin:cat_create")
    kb.button(text="◀️ Назад", callback_data="admin:menu")
    kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(text_parts) if categories else "Нет категорий.",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@admin_router.callback_query(F.data == "admin:cat_create")
async def cb_admin_cat_create(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext,
) -> None:
    """Start category creation."""
    if not _is_admin(callback.from_user.id):
        return

    await callback.message.edit_text("📂 Введите название категории:")
    await state.set_state(CategoryCreation.entering_name)
    await callback.answer()


@admin_router.message(CategoryCreation.entering_name)
async def msg_cat_name(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return

    name = message.text.strip()
    if len(name) < 2 or len(name) > 100:
        await message.answer("Название: от 2 до 100 символов.")
        return

    await state.update_data(name=name)
    await message.answer(
        f"Категория: <b>{name}</b>\n\n"
        "Введите цену для покупателя (USDT за подписчика):"
    )
    await state.set_state(CategoryCreation.entering_buyer_price)


@admin_router.message(CategoryCreation.entering_buyer_price)
async def msg_cat_buyer_price(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return

    try:
        price = round_down(Decimal(message.text.strip()))
    except Exception:
        await message.answer("Введите число (например, 0.50):")
        return

    if price <= Decimal("0"):
        await message.answer("Цена должна быть положительной.")
        return

    await state.update_data(buyer_price=str(price))
    await message.answer(
        f"Цена покупателя: <b>{format_amount_plain(price)} USDT</b>\n\n"
        "Введите выплату продавцу (USDT за подписчика):"
    )
    await state.set_state(CategoryCreation.entering_seller_payout)


@admin_router.message(CategoryCreation.entering_seller_payout)
async def msg_cat_seller_payout(
    message: Message, session: AsyncSession, state: FSMContext,
) -> None:
    if not _is_admin(message.from_user.id):
        return

    try:
        payout = round_down(Decimal(message.text.strip()))
    except Exception:
        await message.answer("Введите число:")
        return

    data = await state.get_data()
    buyer_price = Decimal(data["buyer_price"])

    cat_service = CategoryService(session)
    try:
        category = await cat_service.create(
            name=data["name"],
            buyer_price=buyer_price,
            seller_payout=payout,
        )
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return

    audit_repo = AuditLogRepository(session)
    await audit_repo.log_action(
        actor_telegram_id=message.from_user.id,
        action="category_created",
        object_type="category",
        object_id=category.id,
        new_value={
            "name": category.name,
            "buyer_price": str(buyer_price),
            "seller_payout": str(payout),
        },
    )

    await message.answer(
        f"✅ Категория <b>{category.name}</b> создана.\n\n"
        f"Покупка: {format_amount_plain(buyer_price)} USDT\n"
        f"Выплата: {format_amount_plain(payout)} USDT"
    )
    await state.clear()


# ── System errors ────────────────────────────────────────────────────────────

@admin_router.callback_query(F.data == "admin:errors")
async def cb_admin_errors(callback: CallbackQuery, session: AsyncSession) -> None:
    """Show unresolved system errors."""
    if not _is_admin(callback.from_user.id):
        return

    error_repo = SystemErrorRepository(session)
    errors = await error_repo.get_unresolved()

    if not errors:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data="admin:menu")
        await callback.message.edit_text(
            "✅ Нет ошибок.", reply_markup=kb.as_markup(),
        )
        await callback.answer()
        return

    text_parts = ["🚨 <b>Ошибки</b>\n"]
    for e in errors[:20]:
        sev = {"critical": "🔴", "high": "🟠", "normal": "🟡"}.get(e.severity, "⚪")
        text_parts.append(f"{sev} #{e.id} [{e.error_type}] {e.message[:60]}")

    kb = InlineKeyboardBuilder()
    for e in errors[:5]:
        kb.button(
            text=f"✅ Resolve #{e.id}",
            callback_data=f"admin:error_resolve:{e.id}",
        )
    kb.button(text="◀️ Назад", callback_data="admin:menu")
    kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(text_parts), reply_markup=kb.as_markup(),
    )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:error_resolve:"))
async def cb_admin_error_resolve(
    callback: CallbackQuery, session: AsyncSession,
) -> None:
    if not _is_admin(callback.from_user.id):
        return

    error_id = int(callback.data.split(":")[2])
    error_repo = SystemErrorRepository(session)
    ok = await error_repo.resolve(error_id)
    await callback.answer("✅ Resolved" if ok else "Не найдена")
    await cb_admin_errors(callback, session)


# ── Settings ─────────────────────────────────────────────────────────────────

@admin_router.callback_query(F.data == "admin:settings")
async def cb_admin_settings(callback: CallbackQuery, session: AsyncSession) -> None:
    """Show bot settings."""
    if not _is_admin(callback.from_user.id):
        return

    settings_repo = SettingsRepository(session)
    settings = await settings_repo.get_all_settings()

    text_parts = ["⚙️ <b>Настройки</b>\n\nВыберите настройку для изменения:"]
    kb = InlineKeyboardBuilder()

    for s in settings:
        text_parts.append(f"<code>{s.key}</code> = <b>{s.value}</b>")
        if s.description:
            text_parts.append(f"  <i>{s.description}</i>")
        kb.button(text=f"✏️ {s.key}", callback_data=f"admin:set_edit:{s.key}")

    kb.button(text="◀️ Назад", callback_data="admin:menu")
    kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(text_parts), reply_markup=kb.as_markup(),
    )
    await callback.answer()

@admin_router.callback_query(F.data.startswith("admin:set_edit:"))
async def cb_admin_set_edit(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return

    key = callback.data.split(":", 2)[2]
    settings_repo = SettingsRepository(session)
    val = await settings_repo.get_value(key)
    
    if val is None:
        await callback.answer("Настройка не найдена", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data="admin:settings")
    
    await callback.message.edit_text(
        f"⚙️ Изменение настройки <b>{key}</b>\n\nТекущее значение: <code>{val}</code>\n\nВведите новое значение:",
        reply_markup=kb.as_markup()
    )
    await state.update_data(setting_key=key)
    await state.set_state(AdminSettingsEdit.entering_value)
    await callback.answer()

@admin_router.message(AdminSettingsEdit.entering_value)
async def msg_admin_set_edit_value(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return

    new_val = message.text.strip()
    data = await state.get_data()
    key = data["setting_key"]

    settings_repo = SettingsRepository(session)
    
    # Validation logic
    try:
        if "percent" in key:
            val = Decimal(new_val)
            if not (0 <= val <= 100):
                raise ValueError("Процент должен быть от 0 до 100")
        elif "amount" in key or "deposit" in key or "withdrawal" in key:
            val = Decimal(new_val)
            if val < 0:
                raise ValueError("Сумма не может быть отрицательной")
        elif "interval" in key or "ttl" in key or "cooldown" in key or "max" in key:
            val = int(new_val)
            if val < 0:
                raise ValueError("Значение не может быть отрицательным")
    except Exception as e:
        await message.answer(f"❌ Ошибка валидации: {e}. Попробуйте снова:")
        return

    await settings_repo.set_value(key, new_val)
    
    audit_repo = AuditLogRepository(session)
    await audit_repo.log_action(
        actor_telegram_id=message.from_user.id,
        action="setting_changed",
        object_type="setting",
        object_id=0,
        new_value={"key": key, "value": new_val},
    )

    await state.clear()
    
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад к настройкам", callback_data="admin:settings")
    await message.answer(f"✅ Настройка <b>{key}</b> успешно изменена на <code>{new_val}</code>", reply_markup=kb.as_markup())


@admin_router.callback_query(F.data == "admin:texts")
async def cb_admin_texts(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        return

    text_repo = TextRepository(session)
    texts = await text_repo.get_all_texts()
    
    kb = InlineKeyboardBuilder()
    for t in texts:
        kb.button(text=f"📝 {t.key}", callback_data=f"admin:text_edit:{t.key}")
    
    kb.button(text="◀️ Назад", callback_data="admin:menu")
    kb.adjust(1)
    
    await callback.message.edit_text("📝 <b>Тексты бота</b>\n\nВыберите текст для редактирования:", reply_markup=kb.as_markup())
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:text_edit:"))
async def cb_admin_text_edit(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return

    key = callback.data.split(":", 2)[2]
    text_repo = TextRepository(session)
    val = await text_repo.get_text(key)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Сбросить на стандартный", callback_data=f"admin:text_reset:{key}")
    kb.button(text="◀️ Назад", callback_data="admin:texts")
    kb.adjust(1)
    
    await callback.message.edit_text(
        f"📝 <b>Редактирование текста: {key}</b>\n\n"
        f"Текущее значение:\n<pre>{val}</pre>\n\n"
        f"Введите новый текст (отправьте сообщение). HTML-теги поддерживаются.",
        reply_markup=kb.as_markup()
    )
    await state.update_data(text_key=key)
    await state.set_state(AdminTextEdit.entering_text)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:text_reset:"))
async def cb_admin_text_reset(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        return

    key = callback.data.split(":", 2)[2]
    text_repo = TextRepository(session)
    
    # We don't have a reset method yet, so we'll get default text directly
    from sqlalchemy import select
    from app.db.models import BotText
    
    result = await session.execute(select(BotText).where(BotText.key == key))
    bt = result.scalar_one_or_none()
    
    if bt:
        bt.text = bt.default_text
        await session.commit()
        await callback.answer("✅ Текст сброшен на стандартный")
    else:
        await callback.answer("Текст не найден", show_alert=True)
        
    await cb_admin_texts(callback, session)


@admin_router.message(AdminTextEdit.entering_text)
async def msg_admin_text_edit_value(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return

    new_val = message.text
    data = await state.get_data()
    key = data["text_key"]

    text_repo = TextRepository(session)
    await text_repo.set_text(key, new_val)
    
    audit_repo = AuditLogRepository(session)
    await audit_repo.log_action(
        actor_telegram_id=message.from_user.id,
        action="text_changed",
        object_type="text",
        object_id=0,
        new_value={"key": key, "value": new_val},
    )

    await state.clear()
    
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад к текстам", callback_data="admin:texts")
    await message.answer(f"✅ Текст <b>{key}</b> успешно изменён.\n\nПревью:\n{new_val}", reply_markup=kb.as_markup())


@admin_router.callback_query(F.data == "admin:users")
async def cb_admin_users(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data="admin:menu")
    await callback.message.edit_text(
        "👤 <b>Управление пользователями</b>\n\n"
        "Введите Telegram ID пользователя для просмотра профиля и блокировки:",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(AdminUserManagement.entering_query)
    await callback.answer()

@admin_router.message(AdminUserManagement.entering_query)
async def msg_admin_user_query(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    
    query = message.text.strip()
    user_service = UserService(session)
    
    try:
        tg_id = int(query)
        user = await user_service.get_by_telegram_id(tg_id)
    except ValueError:
        user = None

    if not user:
        await message.answer("❌ Пользователь с таким ID не найден. Попробуйте еще раз:")
        return

    from app.services.balance_service import BalanceService
    from app.utils.decimal_utils import format_amount_plain
    
    balance_service = BalanceService(session)
    available, reserved, held = await balance_service.get_balance(user.id)
    
    text = (
        f"👤 <b>Профиль пользователя</b>\n\n"
        f"ID: <code>{user.telegram_id}</code>\n"
        f"Username: @{user.username or '—'}\n"
        f"Доступно: {format_amount_plain(available)} USDT\n"
        f"В резерве: {format_amount_plain(reserved)} USDT\n"
        f"В холде: {format_amount_plain(held)} USDT\n"
        f"Статус: {'⛔ Заблокирован' if user.is_blocked else '✅ Активен'}\n"
    )
    if user.is_blocked:
        text += f"Причина: {user.block_reason}\n"

    kb = InlineKeyboardBuilder()
    if user.is_blocked:
        kb.button(text="✅ Разблокировать", callback_data=f"admin:unblock:{user.id}")
    else:
        kb.button(text="⛔ Заблокировать", callback_data=f"admin:block:{user.id}")
    kb.button(text="◀️ Назад", callback_data="admin:users")
    kb.adjust(1)

    await message.answer(text, reply_markup=kb.as_markup())
    await state.clear()

@admin_router.callback_query(F.data.startswith("admin:block:"))
async def cb_admin_block_user(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        return
    user_id = int(callback.data.split(":")[2])
    user_service = UserService(session)
    ok = await user_service.block_user(user_id, "Заблокирован администратором")
    await callback.answer("⛔ Пользователь заблокирован" if ok else "Ошибка")
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ В меню", callback_data="admin:menu")
    await callback.message.edit_text("Пользователь заблокирован.", reply_markup=kb.as_markup())

@admin_router.callback_query(F.data.startswith("admin:unblock:"))
async def cb_admin_unblock_user(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        return
    user_id = int(callback.data.split(":")[2])
    user_service = UserService(session)
    ok = await user_service.unblock_user(user_id)
    await callback.answer("✅ Пользователь разблокирован" if ok else "Ошибка")
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ В меню", callback_data="admin:menu")
    await callback.message.edit_text("Пользователь разблокирован.", reply_markup=kb.as_markup())


@admin_router.callback_query(F.data == "admin:adjust")
async def cb_admin_adjust(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext,
) -> None:
    """Start balance adjustment flow."""
    if not _is_admin(callback.from_user.id):
        return

    await callback.message.edit_text(
        "💰 <b>Корректировка баланса</b>\n\n"
        "Введите Telegram ID пользователя:",
    )
    await state.set_state(AdminBalanceAdjust.entering_user)
    await callback.answer()


@admin_router.message(AdminBalanceAdjust.entering_user)
async def msg_admin_adjust_user(
    message: Message, session: AsyncSession, state: FSMContext,
) -> None:
    if not _is_admin(message.from_user.id):
        return

    try:
        tg_id = int(message.text.strip())
    except ValueError:
        await message.answer("Введите числовой Telegram ID.")
        return

    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(tg_id)
    if not user:
        await message.answer("Пользователь не найден.")
        return

    balance_service = BalanceService(session)
    available, reserved, held = await balance_service.get_balance(user.id)

    await state.update_data(user_id=user.id, tg_id=tg_id)
    await message.answer(
        f"Пользователь: @{user.username or tg_id}\n"
        f"Баланс: {format_amount_plain(available)} USDT\n\n"
        f"Введите сумму (+ пополнение, - списание):",
    )
    await state.set_state(AdminBalanceAdjust.entering_amount)


@admin_router.message(AdminBalanceAdjust.entering_amount)
async def msg_admin_adjust_amount(
    message: Message, state: FSMContext,
) -> None:
    if not _is_admin(message.from_user.id):
        return

    try:
        amount = Decimal(message.text.strip())
    except Exception:
        await message.answer("Введите число (например: 10.00 или -5.00):")
        return

    await state.update_data(amount=str(amount))
    await message.answer("Введите причину корректировки:")
    await state.set_state(AdminBalanceAdjust.entering_reason)


@admin_router.message(AdminBalanceAdjust.entering_reason)
async def msg_admin_adjust_reason(
    message: Message, session: AsyncSession, state: FSMContext,
) -> None:
    if not _is_admin(message.from_user.id):
        return

    reason = message.text.strip()
    data = await state.get_data()

    balance_service = BalanceService(session)
    try:
        snapshot = await balance_service.admin_adjustment(
            user_id=data["user_id"],
            amount=Decimal(data["amount"]),
            reason=reason,
            admin_telegram_id=message.from_user.id,
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        await state.clear()
        return

    audit_repo = AuditLogRepository(session)
    await audit_repo.log_action(
        actor_telegram_id=message.from_user.id,
        action="balance_adjustment",
        object_type="user",
        object_id=data["user_id"],
        new_value={
            "amount": data["amount"],
            "reason": reason,
            "new_available": str(snapshot.available),
        },
    )

    await message.answer(
        f"✅ Баланс скорректирован.\n\n"
        f"Сумма: {data['amount']} USDT\n"
        f"Новый баланс: {format_amount_plain(snapshot.available)} USDT\n"
        f"Причина: {reason}",
    )
    await state.clear()
