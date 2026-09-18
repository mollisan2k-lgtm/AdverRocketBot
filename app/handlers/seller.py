"""
Seller handlers: group management, tasks, withdrawals.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, ChatMemberUpdated
from aiogram.filters import ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.user_service import UserService
from app.services.group_service import GroupService
from app.services.task_service import TaskService
from app.services.category_service import CategoryService
from app.services.withdrawal_service import WithdrawalService
from app.services.balance_service import BalanceService
from app.repositories.settings_repo import TextRepository
from app.integrations.telegram_api import TelegramAPIService
from app.integrations.crypto_pay import CryptoPayService
from app.utils.decimal_utils import from_db, format_amount_plain, round_down, is_valid_amount
from app.utils.time_utils import format_datetime

logger = logging.getLogger(__name__)

seller_router = Router(name="seller")


# ── FSM States ───────────────────────────────────────────────────────────────

class GroupRegistration(StatesGroup):
    entering_chat_id = State()
    choosing_category = State()


class WithdrawalFlow(StatesGroup):
    entering_amount = State()
    confirming = State()


class GroupSettings(StatesGroup):
    editing_field = State()


# ── Seller menu ──────────────────────────────────────────────────────────────

@seller_router.callback_query(F.data == "seller:menu")
async def cb_seller_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    """Show seller menu."""
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Мои группы", callback_data="groups:list")
    kb.button(text="📋 Мои задания", callback_data="tasks:list")
    kb.button(text="📤 Вывести средства", callback_data="withdraw:start")
    kb.button(text="◀️ Назад", callback_data="main_menu")
    kb.adjust(1)

    await callback.message.edit_text(
        "💰 <b>Заработок</b>\n\n"
        "Подключите свою группу, и бот будет раздавать участникам "
        "задания на подписку. Вы получаете вознаграждение за каждого "
        "нового подписчика.",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


# ── Groups list ──────────────────────────────────────────────────────────────

@seller_router.callback_query(F.data == "groups:list")
async def cb_groups_list(
    callback: CallbackQuery, session: AsyncSession,
    telegram_api: TelegramAPIService,
) -> None:
    """Show seller's groups."""
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)
    if not user:
        await callback.answer("Используйте /start", show_alert=True)
        return

    group_service = GroupService(session, telegram_api)
    groups = await group_service.get_by_user(user.id)

    if not groups:
        kb = InlineKeyboardBuilder()
        kb.button(text="➕ Добавить группу", callback_data="groups:add")
        kb.button(text="◀️ Назад", callback_data="seller:menu")
        kb.adjust(1)
        await callback.message.edit_text(
            "📭 У вас нет подключённых групп.\n\n"
            "Добавьте свою группу, чтобы начать зарабатывать!",
            reply_markup=kb.as_markup(),
        )
        await callback.answer()
        return

    text_parts = ["👥 <b>Ваши группы</b>\n"]
    for g in groups:
        status_emoji = {
            "pending": "🕐", "approved": "✅", "rejected": "❌", "disabled": "⛔",
        }.get(g.status, "⬜")
        rights = "✅" if g.bot_has_rights else "⚠️"
        text_parts.append(
            f"{status_emoji} {g.title or g.telegram_chat_id}\n"
            f"   Права: {rights} · Участники: {g.member_count or '?'}"
        )

    kb = InlineKeyboardBuilder()
    for g in groups[:10]:
        kb.button(
            text=f"{g.title or str(g.telegram_chat_id)[:20]}",
            callback_data=f"groups:view:{g.id}",
        )
    kb.button(text="➕ Добавить группу", callback_data="groups:add")
    kb.button(text="◀️ Назад", callback_data="seller:menu")
    kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(text_parts), reply_markup=kb.as_markup(),
    )
    await callback.answer()


# ── Group registration ───────────────────────────────────────────────────────

@seller_router.callback_query(F.data == "groups:add")
async def cb_groups_add(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext,
) -> None:
    """Start group registration — ask for chat ID."""
    await callback.message.edit_text(
        "➕ <b>Добавление группы</b>\n\n"
        "1. Добавьте бота в вашу группу как администратора\n"
        "2. Дайте боту право «Ограничение участников»\n"
        "3. Отправьте ID группы (отрицательное число)\n\n"
        "💡 Узнать ID: перешлите любое сообщение из группы "
        "боту @userinfobot",
    )
    await state.set_state(GroupRegistration.entering_chat_id)
    await callback.answer()


@seller_router.message(GroupRegistration.entering_chat_id)
async def msg_group_chat_id(
    message: Message, session: AsyncSession, state: FSMContext,
) -> None:
    """Validate chat ID → choose category."""
    try:
        chat_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Отправьте числовой ID группы.")
        return

    if chat_id >= 0:
        await message.answer("❌ ID группы должен быть отрицательным числом.")
        return

    await state.update_data(chat_id=chat_id)

    # Show categories
    cat_service = CategoryService(session)
    categories = await cat_service.get_active()
    if not categories:
        await message.answer("Нет доступных категорий.")
        await state.clear()
        return

    kb = InlineKeyboardBuilder()
    for cat in categories:
        payout = from_db(cat.seller_payout)
        kb.button(
            text=f"{cat.name} — {format_amount_plain(payout)} USDT/подп.",
            callback_data=f"groups:cat:{cat.id}",
        )
    kb.button(text="◀️ Отмена", callback_data="groups:list")
    kb.adjust(1)

    await message.answer(
        "📂 <b>Выберите категорию</b>\n\n"
        "Категория определяет, какие задания будут раздаваться в группе "
        "и размер вашего вознаграждения:",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(GroupRegistration.choosing_category)


@seller_router.callback_query(
    GroupRegistration.choosing_category,
    F.data.startswith("groups:cat:"),
)
async def cb_group_category_chosen(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext,
    telegram_api: TelegramAPIService,
) -> None:
    """Register group with chosen category."""
    category_id = int(callback.data.split(":")[2])
    data = await state.get_data()
    chat_id = data["chat_id"]

    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)

    group_service = GroupService(session, telegram_api)
    try:
        group = await group_service.register_group(
            user_id=user.id,
            chat_id=chat_id,
            category_id=category_id,
        )
    except ValueError as e:
        await callback.message.edit_text(f"❌ {e}")
        await state.clear()
        await callback.answer()
        return

    await callback.message.edit_text(
        f"✅ Группа <b>{group.title or chat_id}</b> отправлена на модерацию.\n\n"
        "Вы получите уведомление после проверки администратором.",
    )
    await state.clear()
    await callback.answer()


# ── Group view ───────────────────────────────────────────────────────────────

@seller_router.callback_query(F.data.startswith("groups:view:"))
async def cb_group_view(
    callback: CallbackQuery, session: AsyncSession,
    telegram_api: TelegramAPIService,
) -> None:
    """View group details."""
    group_id = int(callback.data.split(":")[2])
    group_service = GroupService(session, telegram_api)
    group = await group_service.get_by_id(group_id)

    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)
    if not group or not user or group.user_id != user.id:
        return await callback.answer("Ошибка доступа", show_alert=True)

    status_label = {
        "pending": "🕐 На модерации",
        "approved": "✅ Одобрена",
        "rejected": f"❌ Отклонена: {group.rejection_reason or '—'}",
        "disabled": "⛔ Отключена",
    }.get(group.status, group.status)

    text = (
        f"👥 <b>{group.title or group.telegram_chat_id}</b>\n\n"
        f"Статус: {status_label}\n"
        f"Участники: {group.member_count or '?'}\n"
        f"Права бота: {'✅' if group.bot_has_rights else '⚠️ Утеряны'}\n"
        f"Заданий за раздачу: {group.tasks_per_distribution}\n"
        f"Интервал: {group.interval_minutes} мин\n"
    )

    kb = InlineKeyboardBuilder()
    if group.status == "approved":
        kb.button(text="⚙️ Настройки", callback_data=f"groups:settings:{group.id}")
    kb.button(text="◀️ Назад", callback_data="groups:list")
    kb.adjust(1)

    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await callback.answer()


# ── Tasks list ───────────────────────────────────────────────────────────────

@seller_router.callback_query(F.data == "tasks:list")
async def cb_tasks_list(callback: CallbackQuery, session: AsyncSession) -> None:
    """Show user's active tasks."""
    task_service = TaskService(session)
    tasks = await task_service.get_user_active_tasks(callback.from_user.id)

    if not tasks:
        text_repo = TextRepository(session)
        no_tasks = await text_repo.get_text("no_active_tasks")
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data="seller:menu")
        await callback.message.edit_text(no_tasks, reply_markup=kb.as_markup())
        await callback.answer()
        return

    text_parts = ["📋 <b>Ваши активные задания</b>\n"]
    for t in tasks[:20]:
        campaign = t.campaign
        title = campaign.target_title_snapshot if campaign else "?"
        text_parts.append(
            f"📌 #{t.id} → {title}\n"
            f"   Создано: {format_datetime(t.created_at)}"
        )

    kb = InlineKeyboardBuilder()
    for t in tasks[:10]:
        kb.button(
            text=f"#{t.id} Проверить",
            callback_data=f"task:check:{t.id}",
        )
    kb.button(text="◀️ Назад", callback_data="seller:menu")
    kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(text_parts), reply_markup=kb.as_markup(),
    )
    await callback.answer()


# ── Task check (verification) ───────────────────────────────────────────────

@seller_router.callback_query(F.data.startswith("task:check:"))
async def cb_task_check(
    callback: CallbackQuery, session: AsyncSession,
    telegram_api: TelegramAPIService,
) -> None:
    """Check subscription and complete task; unrestrict user on success."""
    task_id = int(callback.data.split(":")[2])
    task_service = TaskService(session)
    task = await task_service.get_task_by_id(task_id)

    if not task or task.user_telegram_id != callback.from_user.id:
        return await callback.answer("Ошибка доступа", show_alert=True)

    if task.status != "active":
        return await callback.answer("Задание не активно.", show_alert=True)

    # Check subscription via Telegram API
    is_subscribed = await telegram_api.is_user_subscribed(
        task.target_chat_id, callback.from_user.id,
    )

    try:
        from app.db.engine import run_atomic
        
        async def _verify_op(write_session: AsyncSession):
            task_service_write = TaskService(write_session)
            res = await task_service_write.verify_and_complete(task_id, is_subscribed)
            
            # Set desired restriction state to OFF, let worker handle actual Telegram call
            from app.repositories.restriction_repo import RestrictionRepository
            restriction_repo = RestrictionRepository(write_session)
            await restriction_repo.set_desired_state(
                group_id=task.group_id,
                user_telegram_id=callback.from_user.id,
                desired_state="OFF"
            )
            return res
            
        result = await run_atomic(_verify_op)
        # Transaction commits here
    except Exception as e:
        await callback.answer(str(e), show_alert=True)
        return

    text_repo = TextRepository(session)
    tpl = await text_repo.get_text("task_completed")
    text = tpl.format(
        reward=result["seller_reward"],
        completed=result["completed"],
        target=result["target"],
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Мои задания", callback_data="tasks:list")
    kb.button(text="◀️ Меню", callback_data="main_menu")
    kb.adjust(1)

    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await callback.answer("✅ Задание выполнено!")


# ── Withdrawal flow ──────────────────────────────────────────────────────────

@seller_router.callback_query(F.data == "withdraw:start")
async def cb_withdraw_start(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext,
) -> None:
    """Start withdrawal — show balance and ask amount."""
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)
    if not user:
        await callback.answer("Используйте /start", show_alert=True)
        return

    balance_service = BalanceService(session)
    available, _, _ = await balance_service.get_balance(user.id)

    await callback.message.edit_text(
        f"📤 <b>Вывод средств</b>\n\n"
        f"Доступно: <code>{format_amount_plain(available)}</code> USDT\n\n"
        f"Введите сумму для вывода (мин. 5.00 USDT):",
    )
    await state.set_state(WithdrawalFlow.entering_amount)
    await callback.answer()


@seller_router.message(WithdrawalFlow.entering_amount)
async def msg_withdraw_amount(
    message: Message, session: AsyncSession, state: FSMContext,
) -> None:
    """Process withdrawal amount."""
    try:
        amount = round_down(Decimal(message.text.strip()))
    except Exception:
        await message.answer("❌ Введите корректную сумму.")
        return

    if amount < Decimal("5.00"):
        await message.answer("❌ Минимальная сумма вывода: 5.00 USDT")
        return

    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(message.from_user.id)

    balance_service = BalanceService(session)
    available, _, _ = await balance_service.get_balance(user.id)

    if available < amount:
        await message.answer(
            f"❌ Недостаточно средств.\n"
            f"Доступно: {format_amount_plain(available)} USDT"
        )
        return

    # Check for existing pending withdrawal
    from app.repositories.withdrawal_repo import WithdrawalRepository
    wd_repo = WithdrawalRepository(session)
    has_pending = await wd_repo.has_pending(user.id)
    if has_pending:
        await message.answer(
            "⚠️ У вас уже есть необработанная заявка на вывод.\n"
            "Дождитесь её завершения."
        )
        await state.clear()
        return

    await state.update_data(amount=str(amount))

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data="withdraw:confirm")
    kb.button(text="◀️ Отмена", callback_data="seller:menu")
    kb.adjust(2)

    await message.answer(
        f"📤 <b>Подтверждение вывода</b>\n\n"
        f"Сумма: <code>{format_amount_plain(amount)}</code> USDT\n\n"
        f"Средства будут зарезервированы до одобрения администратором.",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(WithdrawalFlow.confirming)


@seller_router.callback_query(
    WithdrawalFlow.confirming,
    F.data == "withdraw:confirm",
)
async def cb_withdraw_confirm(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext,
    crypto_pay: CryptoPayService | None,
) -> None:
    """Create withdrawal request."""
    data = await state.get_data()
    amount = Decimal(data["amount"])

    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)

    wd_service = WithdrawalService(session, crypto_pay)
    try:
        withdrawal = await wd_service.request_withdrawal(
            user_id=user.id,
            amount=amount,
            recipient_telegram_id=user.telegram_id,
        )
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}")
        await state.clear()
        await callback.answer()
        return

    text_repo = TextRepository(session)
    tpl = await text_repo.get_text("withdrawal_requested")
    text = tpl.format(amount=format_amount_plain(amount))

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Меню", callback_data="main_menu")

    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await state.clear()
    await callback.answer()


# ── Group settings ────────────────────────────────────────────────────────────

@seller_router.callback_query(F.data.startswith("groups:settings:"))
async def cb_group_settings(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext,
    telegram_api: TelegramAPIService,
) -> None:
    """Show group settings panel."""
    group_id = int(callback.data.split(":")[2])
    group_service = GroupService(session, telegram_api)
    group = await group_service.get_by_id(group_id)

    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)
    if not group or not user or group.user_id != user.id:
        return await callback.answer("Ошибка доступа", show_alert=True)

    if group.status != "approved":
        await callback.answer("Группа недоступна.", show_alert=True)
        return

    text = (
        f"⚙️ <b>Настройки группы</b>\n\n"
        f"👥 {group.title or group.telegram_chat_id}\n\n"
        f"📦 Заданий за раздачу: <b>{group.tasks_per_distribution}</b>\n"
        f"   (сколько заданий получают участники за одну раздачу)\n\n"
        f"⏱ Интервал повтора: <b>{group.interval_minutes} мин</b>\n"
        f"   (через сколько минут участник может получить новое задание)"
    )

    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"📦 Заданий за раздачу: {group.tasks_per_distribution}",
        callback_data=f"groups:set_tasks:{group_id}",
    )
    kb.button(
        text=f"⏱ Интервал: {group.interval_minutes} мин",
        callback_data=f"groups:set_interval:{group_id}",
    )
    kb.button(text="◀️ Назад", callback_data=f"groups:view:{group_id}")
    kb.adjust(1)

    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await callback.answer()


@seller_router.callback_query(F.data.startswith("groups:set_tasks:"))
async def cb_group_set_tasks(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext,
) -> None:
    """Start editing tasks_per_distribution."""
    group_id = int(callback.data.split(":")[2])
    from app.services.group_service import GroupService
    from app.integrations.telegram_api import TelegramAPIService
    # Create dummy telegram_api as it's required by signature but not used for get_by_id
    telegram_api = TelegramAPIService("dummy")
    group_service = GroupService(session, telegram_api)
    group = await group_service.get_by_id(group_id)
    
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)
    if not group or not user or group.user_id != user.id:
        return await callback.answer("Ошибка доступа", show_alert=True)
        
    await state.update_data(group_id=group_id, field="tasks_per_distribution")
    await callback.message.edit_text(
        "📦 <b>Заданий за раздачу</b>\n\n"
        "Введите число от 1 до 20:\n"
        "(сколько заданий получает каждый участник за одну раздачу)"
    )
    await state.set_state(GroupSettings.editing_field)
    await callback.answer()


@seller_router.callback_query(F.data.startswith("groups:set_interval:"))
async def cb_group_set_interval(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext,
) -> None:
    """Start editing interval_minutes."""
    group_id = int(callback.data.split(":")[2])
    from app.services.group_service import GroupService
    from app.integrations.telegram_api import TelegramAPIService
    telegram_api = TelegramAPIService("dummy")
    group_service = GroupService(session, telegram_api)
    group = await group_service.get_by_id(group_id)
    
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)
    if not group or not user or group.user_id != user.id:
        return await callback.answer("Ошибка доступа", show_alert=True)
        
    await state.update_data(group_id=group_id, field="interval_minutes")
    await callback.message.edit_text(
        "⏱ <b>Интервал повтора задания</b>\n\n"
        "Введите интервал в минутах (от 10 до 10080):\n"
        "Пример: 60 = 1 час, 1440 = 24 часа"
    )
    await state.set_state(GroupSettings.editing_field)
    await callback.answer()


@seller_router.message(GroupSettings.editing_field)
async def msg_group_setting_value(
    message: Message, session: AsyncSession, state: FSMContext,
) -> None:
    """Process new group setting value."""
    data = await state.get_data()
    group_id = data["group_id"]
    field = data["field"]

    try:
        value = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите целое число.")
        return

    # Validate
    if field == "tasks_per_distribution":
        if not 1 <= value <= 20:
            await message.answer("❌ Допустимый диапазон: от 1 до 20.")
            return
        field_label = "Заданий за раздачу"
    elif field == "interval_minutes":
        if not 10 <= value <= 10080:
            await message.answer("❌ Допустимый диапазон: от 10 до 10080 минут.")
            return
        field_label = "Интервал повтора"
    else:
        await state.clear()
        return

    # Apply update
    from app.repositories.group_repo import GroupRepository
    from app.utils.time_utils import utc_now
    group_repo = GroupRepository(session)
    group = await group_repo.get_by_id(group_id)
    
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(message.from_user.id)

    if not group or not user or group.user_id != user.id:
        await message.answer("❌ Ошибка доступа.")
        await state.clear()
        return

    setattr(group, field, value)
    group.updated_at = utc_now()

    kb = InlineKeyboardBuilder()
    kb.button(text="⚙️ Настройки", callback_data=f"groups:settings:{group_id}")
    kb.button(text="◀️ К группе", callback_data=f"groups:view:{group_id}")
    kb.adjust(2)

    await message.answer(
        f"✅ <b>{field_label}</b> обновлено: <b>{value}</b>",
        reply_markup=kb.as_markup(),
    )
    await state.clear()


# ── New member handler (ChatMemberUpdated) ────────────────────────────────────


@seller_router.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_new_member(
    event: ChatMemberUpdated,
    session: AsyncSession,
    telegram_api: TelegramAPIService,
) -> None:
    """
    Triggered when a user joins a group the bot manages.

    Flow:
    1. Check if this group is approved in our system
    2. Skip bots
    3. Restrict new member (bot-applied restriction)
    4. Assign a task from an active campaign matching group's category
    5. Send task notification to user via private message

    If no campaigns available → unrestrict immediately (don't hold user).
    """
    chat_id = event.chat.id
    user = event.new_chat_member.user

    # Skip bots
    if user.is_bot:
        return

    user_tg_id = user.id

    # 1. Check group is approved
    group_service = GroupService(session, telegram_api)
    group = await group_service.get_by_chat_id(chat_id)

    if not group or group.status != "approved" or not group.bot_has_rights:
        return

    # 2. Get original permissions via HTTP (outside transaction)
    original_perms = await telegram_api.get_member_permissions(chat_id, user_tg_id)

    # 3. Transactional task assignment and restriction recording
    from app.db.engine import run_atomic
    from app.repositories.restriction_repo import RestrictionRepository
    from app.services.campaign_service import CampaignService
    from app.services.task_service import TaskService, TaskDistributionError

    async def _assign_op(write_session: AsyncSession) -> int | None:
        campaign_service = CampaignService(write_session)
        task_service = TaskService(write_session)
        restriction_repo = RestrictionRepository(write_session)

        campaigns = await campaign_service.get_distributable_campaigns(group.category_id)
        assigned_task = None
        for campaign in campaigns:
            if campaign.completed >= campaign.target:
                continue
            try:
                assigned_task = await task_service.assign_task(
                    user_telegram_id=user_tg_id,
                    campaign_id=campaign.id,
                    group_id=group.id,
                )
                break
            except TaskDistributionError as e:
                logger.debug(
                    "Task assignment skipped for user=%d campaign=%d: %s",
                    user_tg_id, campaign.id, e,
                )
                continue

        if assigned_task:
            # We got a task, so we want to restrict the user.
            # Record desired_state="ON", actual_state="OFF"
            await restriction_repo.add_restriction(
                group_id=group.id,
                user_telegram_id=user_tg_id,
                original_permissions=original_perms,
            )
            return assigned_task.id
        return None

    task_id = await run_atomic(_assign_op)

    if task_id:
        logger.info(
            "Task %d scheduled for pending_restriction for user=%d in group=%d", 
            task_id, user_tg_id, group.id
        )
    else:
        logger.info(
            "No campaigns for group=%d, user=%d not restricted",
            group.id, user_tg_id,
        )

