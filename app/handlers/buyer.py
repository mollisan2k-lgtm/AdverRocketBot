"""
Buyer handlers: campaign creation wizard, deposit, campaign management.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.user_service import UserService
from app.services.campaign_service import CampaignService
from app.services.category_service import CategoryService
from app.services.deposit_service import DepositService
from app.services.balance_service import BalanceService
from app.repositories.draft_repo import DraftRepository
from app.repositories.settings_repo import TextRepository
from app.integrations.crypto_pay import CryptoPayService
from app.integrations.telegram_api import TelegramAPIService
from app.utils.decimal_utils import from_db, to_db, format_amount_plain, round_down, is_valid_amount, ZERO
from app.config import config

logger = logging.getLogger(__name__)

buyer_router = Router(name="buyer")


# ── FSM States ───────────────────────────────────────────────────────────────

class CampaignCreation(StatesGroup):
    choosing_category = State()
    entering_target_link = State()
    choosing_type = State()
    entering_target_count = State()
    confirming = State()


class DepositFlow(StatesGroup):
    entering_amount = State()


class CampaignIncrease(StatesGroup):
    entering_extra_count = State()


# ── Buyer menu ───────────────────────────────────────────────────────────────

@buyer_router.callback_query(F.data == "buyer:menu")
async def cb_buyer_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    """Show buyer menu."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🆕 Создать кампанию", callback_data="campaign:create")
    kb.button(text="📋 Мои кампании", callback_data="campaign:list")
    kb.button(text="💳 Пополнить баланс", callback_data="deposit:start")
    kb.button(text="◀️ Назад", callback_data="main_menu")
    kb.adjust(1)

    await callback.message.edit_text(
        "🛒 <b>Покупатель</b>\n\n"
        "Создайте рекламную кампанию для продвижения вашего канала или группы.",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


# ── Campaign creation wizard ────────────────────────────────────────────────

@buyer_router.callback_query(F.data == "campaign:create")
async def cb_campaign_create(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext,
) -> None:
    """Step 1: Check drafts or choose category."""
    from app.repositories.draft_repo import DraftRepository
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)
    if user:
        draft_repo = DraftRepository(session)
        draft = await draft_repo.get_active_by_user(user.id)
        if draft:
            kb = InlineKeyboardBuilder()
            kb.button(text="Продолжить", callback_data="campaign:draft:resume")
            kb.button(text="Начать заново", callback_data="campaign:draft:discard")
            kb.button(text="◀️ Отмена", callback_data="buyer:menu")
            kb.adjust(1)
            await callback.message.edit_text(
                "У вас есть неоформленный заказ. Продолжить?",
                reply_markup=kb.as_markup()
            )
            return

    await _start_new_campaign(callback, session, state)

@buyer_router.callback_query(F.data == "campaign:draft:resume")
async def cb_campaign_draft_resume(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext,
) -> None:
    from app.repositories.draft_repo import DraftRepository
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)
    if user:
        draft_repo = DraftRepository(session)
        draft = await draft_repo.get_active_by_user(user.id)
        if draft:
            data = draft_repo.parse_data(draft)
            await state.update_data(**data)
            
            # Determine next step based on available data
            if "target" in data:
                # Need to re-ask count to show confirmation with fresh balance
                await state.set_state(CampaignCreation.entering_target_count)
                await callback.message.edit_text(
                    f"✅ <b>{data.get('target_title')}</b>\n\n"
                    "🎯 Сколько подписчиков вы хотите получить?\n"
                    "Введите число (мин. 10):"
                )
            elif "target_link" in data:
                await state.set_state(CampaignCreation.entering_target_count)
                await callback.message.edit_text(
                    f"✅ <b>{data.get('target_title')}</b>\n\n"
                    "🎯 Сколько подписчиков вы хотите получить?\n"
                    "Введите число (мин. 10):"
                )
            elif "target_type" in data:
                target_type = data["target_type"]
                type_label = "канала" if target_type == "channel" else "группы"
                await state.set_state(CampaignCreation.entering_target_link)
                await callback.message.edit_text(
                    f"🔗 Отправьте ссылку или @username вашего {type_label}:\n\n"
                    "Пример: <code>@mychannel</code> или <code>https://t.me/mychannel</code>"
                )
            elif "category_id" in data:
                await state.set_state(CampaignCreation.choosing_type)
                kb = InlineKeyboardBuilder()
                kb.button(text="📢 Канал", callback_data="campaign:type:channel")
                kb.button(text="👥 Группа", callback_data="campaign:type:group")
                kb.button(text="◀️ Назад", callback_data="campaign:create")
                kb.adjust(2, 1)
                await callback.message.edit_text(
                    f"📂 Категория: <b>{data.get('category_name')}</b>\n\n"
                    "Выберите тип объекта для продвижения:",
                    reply_markup=kb.as_markup()
                )
            return

    await _start_new_campaign(callback, session, state)

@buyer_router.callback_query(F.data == "campaign:draft:discard")
async def cb_campaign_draft_discard(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext,
) -> None:
    from app.repositories.draft_repo import DraftRepository
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)
    if user:
        draft_repo = DraftRepository(session)
        await draft_repo.delete_by_user(user.id)
    await _start_new_campaign(callback, session, state)

async def _start_new_campaign(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext,
) -> None:
    await state.clear()
    cat_service = CategoryService(session)
    categories = await cat_service.get_active()

    if not categories:
        await callback.answer("Нет доступных категорий.", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    for cat in categories:
        buyer_price = from_db(cat.buyer_price)
        kb.button(
            text=f"{cat.name} — {format_amount_plain(buyer_price)} USDT/подп.",
            callback_data=f"campaign:cat:{cat.id}",
        )
    kb.button(text="◀️ Отмена", callback_data="buyer:menu")
    kb.adjust(1)

    await callback.message.edit_text(
        "📂 <b>Выберите категорию</b>\n\n"
        "Цена указана за одного подписчика:",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(CampaignCreation.choosing_category)
    await callback.answer()


@buyer_router.callback_query(
    CampaignCreation.choosing_category,
    F.data.startswith("campaign:cat:"),
)
async def cb_campaign_cat_chosen(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext,
) -> None:
    """Step 2: Category chosen → choose type (channel/group)."""
    category_id = int(callback.data.split(":")[2])

    cat_service = CategoryService(session)
    category = await cat_service.get_by_id(category_id)
    if not category:
        await callback.answer("Категория не найдена.", show_alert=True)
        return

    await state.update_data(
        category_id=category_id,
        category_name=category.name,
        buyer_price=str(category.buyer_price),
        seller_payout=str(category.seller_payout),
    )
    
    # Save draft
    from app.repositories.draft_repo import DraftRepository
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)
    if user:
        draft_repo = DraftRepository(session)
        data = await state.get_data()
        await draft_repo.upsert(user.id, data)

    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Канал", callback_data="campaign:type:channel")
    kb.button(text="👥 Группа", callback_data="campaign:type:group")
    kb.button(text="◀️ Назад", callback_data="campaign:create")
    kb.adjust(2, 1)

    await callback.message.edit_text(
        f"📂 Категория: <b>{category.name}</b>\n\n"
        "Выберите тип объекта для продвижения:",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(CampaignCreation.choosing_type)
    await callback.answer()


@buyer_router.callback_query(
    CampaignCreation.choosing_type,
    F.data.startswith("campaign:type:"),
)
async def cb_campaign_type_chosen(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext,
) -> None:
    """Step 3: Type chosen → enter target link."""
    target_type = callback.data.split(":")[2]
    await state.update_data(target_type=target_type)
    
    # Save draft
    from app.repositories.draft_repo import DraftRepository
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)
    if user:
        draft_repo = DraftRepository(session)
        data = await state.get_data()
        await draft_repo.upsert(user.id, data)

    type_label = "канала" if target_type == "channel" else "группы"
    await callback.message.edit_text(
        f"🔗 Отправьте ссылку или @username вашего {type_label}:\n\n"
        "Пример: <code>@mychannel</code> или <code>https://t.me/mychannel</code>",
    )
    await state.set_state(CampaignCreation.entering_target_link)
    await callback.answer()


@buyer_router.message(CampaignCreation.entering_target_link)
async def msg_campaign_target_link(
    message: Message, session: AsyncSession, state: FSMContext,
    telegram_api: TelegramAPIService,
) -> None:
    """Step 4: Validate target link → enter count."""
    link = message.text.strip()
    data = await state.get_data()
    target_type = data["target_type"]

    # Validate via Telegram API
    try:
        target_info = await telegram_api.validate_campaign_target(link, target_type)
    except ValueError as e:
        await message.answer(f"❌ {e}\n\nПопробуйте ещё раз:")
        return

    await state.update_data(
        target_link=link,
        target_chat_id=target_info.target_chat_id,
        target_username=target_info.target_username,
        target_title=target_info.target_title,
    )
    
    # Save draft
    from app.repositories.draft_repo import DraftRepository
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(message.from_user.id)
    if user:
        draft_repo = DraftRepository(session)
        data = await state.get_data()
        await draft_repo.upsert(user.id, data)

    await message.answer(
        f"✅ <b>{target_info.target_title}</b>\n\n"
        "🎯 Сколько подписчиков вы хотите получить?\n"
        "Введите число (мин. 10):",
    )
    await state.set_state(CampaignCreation.entering_target_count)


@buyer_router.message(CampaignCreation.entering_target_count)
async def msg_campaign_target_count(
    message: Message, session: AsyncSession, state: FSMContext,
) -> None:
    """Step 5: Target count entered → show confirmation."""
    try:
        target = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите целое число.")
        return

    if target < 10:
        await message.answer("❌ Минимум 10 подписчиков.")
        return
    if target > 100000:
        await message.answer("❌ Максимум 100 000 подписчиков.")
        return

    data = await state.get_data()
    buyer_price = from_db(data["buyer_price"])
    total_cost = buyer_price * target

    await state.update_data(target=target, total_cost=str(total_cost))
    
    # Save draft
    from app.repositories.draft_repo import DraftRepository
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(message.from_user.id)
    if user:
        draft_repo = DraftRepository(session)
        data = await state.get_data()
        await draft_repo.upsert(user.id, data)

    # Check balance
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(message.from_user.id)
    balance_service = BalanceService(session)
    available, _, _ = await balance_service.get_balance(user.id)

    balance_ok = available >= total_cost
    balance_status = "✅ Средств достаточно" if balance_ok else (
        f"⚠️ Недостаточно средств (нужно ещё "
        f"{format_amount_plain(total_cost - available)} USDT)"
    )

    kb = InlineKeyboardBuilder()
    if balance_ok:
        kb.button(text="✅ Подтвердить", callback_data="campaign:confirm")
    else:
        kb.button(text="💳 Пополнить", callback_data="deposit:start")
    kb.button(text="◀️ Отмена", callback_data="buyer:menu")
    kb.adjust(1)

    await message.answer(
        f"📋 <b>Подтверждение кампании</b>\n\n"
        f"📌 {data['target_title']}\n"
        f"📂 Категория: {data['category_name']}\n"
        f"🎯 Цель: {target} подписчиков\n"
        f"💰 Цена за подп.: {format_amount_plain(buyer_price)} USDT\n"
        f"💵 Итого: <b>{format_amount_plain(total_cost)} USDT</b>\n\n"
        f"{balance_status}",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(CampaignCreation.confirming)


@buyer_router.callback_query(
    CampaignCreation.confirming,
    F.data == "campaign:confirm",
)
async def cb_campaign_confirm(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext,
) -> None:
    """Create campaign with atomic reserve."""
    data = await state.get_data()
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)

    campaign_service = CampaignService(session)
    try:
        campaign = await campaign_service.create_campaign(
            user_id=user.id,
            category_id=data["category_id"],
            target=data["target"],
            target_type=data["target_type"],
            target_chat_id=data["target_chat_id"],
            target_username=data.get("target_username"),
            target_title_snapshot=data["target_title"],
            target_link=data["target_link"],
        )
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}")
        await state.clear()
        await callback.answer()
        return

    # Delete draft after successful creation
    from app.repositories.draft_repo import DraftRepository
    draft_repo = DraftRepository(session)
    await draft_repo.delete_by_user(user.id)

    text_repo = TextRepository(session)
    tpl = await text_repo.get_text("campaign_created")
    text = tpl.format(
        target_title=data["target_title"],
        target=data["target"],
        cost=format_amount_plain(from_db(data["total_cost"])),
        category=data["category_name"],
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Мои кампании", callback_data="campaign:list")
    kb.button(text="◀️ Меню", callback_data="main_menu")
    kb.adjust(1)

    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await state.clear()
    await callback.answer()


# ── Campaign list ────────────────────────────────────────────────────────────

@buyer_router.callback_query(F.data == "campaign:list")
async def cb_campaign_list(callback: CallbackQuery, session: AsyncSession) -> None:
    """Show user's campaigns."""
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)
    if not user:
        await callback.answer("Используйте /start", show_alert=True)
        return

    campaign_service = CampaignService(session)
    campaigns = await campaign_service.get_user_campaigns(user.id)

    if not campaigns:
        kb = InlineKeyboardBuilder()
        kb.button(text="🆕 Создать", callback_data="campaign:create")
        kb.button(text="◀️ Назад", callback_data="buyer:menu")
        kb.adjust(1)
        await callback.message.edit_text(
            "📭 У вас нет кампаний.", reply_markup=kb.as_markup(),
        )
        await callback.answer()
        return

    text_parts = ["📋 <b>Ваши кампании</b>\n"]
    for c in campaigns[:20]:
        status_emoji = {
            "active": "🟢", "paused": "⏸", "completed": "✅", "cancelled": "🔴",
        }.get(c.status, "⬜")
        text_parts.append(
            f"{status_emoji} #{c.id} {c.target_title_snapshot}\n"
            f"   {c.completed}/{c.target} · {c.status}"
        )

    kb = InlineKeyboardBuilder()
    for c in campaigns[:10]:
        kb.button(
            text=f"#{c.id} {c.target_title_snapshot[:25]}",
            callback_data=f"campaign:view:{c.id}",
        )
    kb.button(text="🆕 Создать", callback_data="campaign:create")
    kb.button(text="◀️ Назад", callback_data="buyer:menu")
    kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(text_parts), reply_markup=kb.as_markup(),
    )
    await callback.answer()


class CampaignDecrease(StatesGroup):
    entering_target = State()

# ── Campaign view ────────────────────────────────────────────────────────────

@buyer_router.callback_query(F.data.startswith("campaign:view:"))
async def cb_campaign_view(callback: CallbackQuery, session: AsyncSession) -> None:
    """View single campaign details."""
    campaign_id = int(callback.data.split(":")[2])
    
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)
    if not user:
        return await callback.answer("Ошибка доступа", show_alert=True)
        
    campaign_service = CampaignService(session)
    campaign = await campaign_service.get_by_id(campaign_id)

    if not campaign or campaign.user_id != user.id:
        await callback.answer("Кампания не найдена или нет доступа.", show_alert=True)
        return

    buyer_price = from_db(campaign.buyer_price_snapshot)
    total_cost = buyer_price * campaign.target
    spent = buyer_price * campaign.completed
    remaining = total_cost - spent

    text = (
        f"📌 <b>{campaign.target_title_snapshot}</b>\n\n"
        f"📂 Категория: {campaign.category_name_snapshot}\n"
        f"🎯 Прогресс: <b>{campaign.completed}/{campaign.target}</b>\n"
        f"💰 Потрачено: {format_amount_plain(spent)} USDT\n"
        f"💵 Остаток в резерве: {format_amount_plain(remaining)} USDT\n"
        f"📊 Статус: {campaign.status}\n"
    )

    kb = InlineKeyboardBuilder()
    if campaign.status == "active":
        kb.button(text="⏸ Пауза", callback_data=f"campaign:pause:{campaign_id}")
        kb.button(text="🎯 Увеличить", callback_data=f"campaign:increase:{campaign_id}")
        if campaign.target > campaign.completed:
            kb.button(text="📉 Уменьшить", callback_data=f"campaign:decrease:{campaign_id}")
        kb.button(text="🔴 Отменить", callback_data=f"campaign:cancel:{campaign_id}")
    elif campaign.status == "paused":
        kb.button(text="▶️ Возобновить", callback_data=f"campaign:resume:{campaign_id}")
        if campaign.target > campaign.completed:
            kb.button(text="📉 Уменьшить", callback_data=f"campaign:decrease:{campaign_id}")
        kb.button(text="🔴 Отменить", callback_data=f"campaign:cancel:{campaign_id}")
    kb.button(text="◀️ Назад", callback_data="campaign:list")
    kb.adjust(2)

    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await callback.answer()


# ── Campaign actions ─────────────────────────────────────────────────────────

@buyer_router.callback_query(F.data.startswith("campaign:pause:"))
async def cb_campaign_pause(callback: CallbackQuery, session: AsyncSession) -> None:
    campaign_id = int(callback.data.split(":")[2])
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)
    from app.db.engine import run_atomic
    async def _pause_op(write_session: AsyncSession) -> bool:
        service_write = CampaignService(write_session)
        campaign = await service_write.campaign_repo.get_by_id(campaign_id)
        if not campaign or not user or campaign.user_id != user.id:
            return False
            
        return await service_write.pause_campaign(campaign_id)
        
    ok = await run_atomic(_pause_op)
    if not user:
        return await callback.answer("Ошибка доступа", show_alert=True)
    await callback.answer(
        "⏸ Кампания приостановлена" if ok else "Не удалось приостановить.",
        show_alert=not ok,
    )
    if ok:
        await cb_campaign_view(callback, session)


@buyer_router.callback_query(F.data.startswith("campaign:resume:"))
async def cb_campaign_resume(callback: CallbackQuery, session: AsyncSession) -> None:
    campaign_id = int(callback.data.split(":")[2])
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)
    from app.db.engine import run_atomic
    async def _resume_op(write_session: AsyncSession) -> bool:
        service_write = CampaignService(write_session)
        campaign = await service_write.campaign_repo.get_by_id(campaign_id)
        if not campaign or not user or campaign.user_id != user.id:
            return False
            
        return await service_write.resume_campaign(campaign_id)
        
    ok = await run_atomic(_resume_op)
    if not user:
        return await callback.answer("Ошибка доступа", show_alert=True)
    await callback.answer(
        "▶️ Кампания возобновлена" if ok else "Не удалось возобновить.",
        show_alert=not ok,
    )
    if ok:
        await cb_campaign_view(callback, session)


@buyer_router.callback_query(F.data.startswith("campaign:decrease:"))
async def cb_campaign_decrease(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    campaign_id = int(callback.data.split(":")[2])
    
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)
    if not user:
        return await callback.answer("Ошибка доступа", show_alert=True)
        
    campaign_service = CampaignService(session)
    campaign = await campaign_service.get_by_id(campaign_id)
    if not campaign or campaign.user_id != user.id:
        return await callback.answer("Кампания не найдена", show_alert=True)
        
    if campaign.target <= campaign.completed:
        return await callback.answer("Цель уже достигнута", show_alert=True)

    await state.update_data(campaign_id=campaign_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Отмена", callback_data=f"campaign:view:{campaign_id}")
    
    await callback.message.edit_text(
        f"Текущая цель: <b>{campaign.target}</b>\n"
        f"Уже выполнено: <b>{campaign.completed}</b>\n\n"
        f"Введите новую цель (от {campaign.completed} до {campaign.target - 1}):",
        reply_markup=kb.as_markup()
    )
    await state.set_state(CampaignDecrease.entering_target)
    await callback.answer()

@buyer_router.message(CampaignDecrease.entering_target)
async def msg_campaign_decrease_target(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(message.from_user.id)
    if not user:
        return

    data = await state.get_data()
    campaign_id = data.get("campaign_id")
    if not campaign_id:
        return await message.answer("Ошибка: ID кампании потерян.")

    try:
        new_target = int(message.text.strip())
    except ValueError:
        return await message.answer("Пожалуйста, введите целое число.")

    campaign_service = CampaignService(session)
    campaign = await campaign_service.get_by_id(campaign_id)
    if not campaign or campaign.user_id != user.id:
        await state.clear()
        return await message.answer("Кампания не найдена.")

    if new_target < campaign.completed or new_target >= campaign.target:
        return await message.answer(
            f"Новая цель должна быть от {campaign.completed} до {campaign.target - 1}. Введите еще раз:"
        )

    result = await campaign_service.decrease_target(campaign_id, user.id, new_target)
    if result is None:
        await message.answer("Не удалось уменьшить цель.")
    else:
        refunded = result.get("refunded", "0")
        await message.answer(
            f"✅ Цель уменьшена до {new_target}.\n"
            f"Сумма возврата: {format_amount_plain(refunded)} USDT"
        )
    await state.clear()


@buyer_router.callback_query(F.data.startswith("campaign:cancel:"))
async def cb_campaign_cancel(callback: CallbackQuery, session: AsyncSession) -> None:
    """Cancel campaign with refund."""
    campaign_id = int(callback.data.split(":")[2])
    
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)
    service = CampaignService(session)
    campaign = await service.campaign_repo.get_by_id(campaign_id)
    if not campaign or not user or campaign.user_id != user.id:
        return await callback.answer("Ошибка доступа", show_alert=True)

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, отменить", callback_data=f"campaign:cancel_confirm:{campaign_id}")
    kb.button(text="◀️ Нет", callback_data=f"campaign:view:{campaign_id}")
    kb.adjust(2)

    await callback.message.edit_text(
        "⚠️ <b>Отменить кампанию?</b>\n\n"
        "Остаток средств будет возвращён с удержанием комиссии 10%.",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@buyer_router.callback_query(F.data.startswith("campaign:cancel_confirm:"))
async def cb_campaign_cancel_confirm(callback: CallbackQuery, session: AsyncSession) -> None:
    campaign_id = int(callback.data.split(":")[2])
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)
    try:
        from app.db.engine import run_atomic
        async def _cancel_op(write_session: AsyncSession):
            service = CampaignService(write_session)
            if not user:
                return None
            return await service.cancel_campaign(campaign_id, user_id=user.id)
            
        result = await run_atomic(_cancel_op)
        if result is None:
            await callback.answer("Не удалось отменить.", show_alert=True)
            return
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}")
        return

    await callback.message.edit_text(
        f"🔴 Кампания отменена.\n\n"
        f"💰 Возврат: {result['refunded']} USDT\n"
        f"📊 Комиссия: {result['commission']} USDT\n"
        f"❌ Отменено заданий: {result['cancelled_tasks']}",
    )
    await callback.answer()



# ── Campaign increase target ─────────────────────────────────────────────────

@buyer_router.callback_query(F.data.startswith("campaign:increase:"))
async def cb_campaign_increase(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext,
) -> None:
    """Start increase target flow."""
    campaign_id = int(callback.data.split(":")[2])
    campaign_service = CampaignService(session)
    campaign = await campaign_service.get_by_id(campaign_id)

    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)
    if not campaign or not user or campaign.user_id != user.id:
        return await callback.answer("Ошибка доступа", show_alert=True)

    if campaign.status != "active":
        await callback.answer("Кампания не активна.", show_alert=True)
        return

    buyer_price = from_db(campaign.buyer_price_snapshot)
    remaining = campaign.target - campaign.completed

    await state.update_data(campaign_id=campaign_id, buyer_price=str(buyer_price))
    await callback.message.edit_text(
        f"🎯 <b>Увеличение цели кампании</b>\n\n"
        f"📌 {campaign.target_title_snapshot}\n"
        f"Текущая цель: {campaign.target} подписчиков\n"
        f"Выполнено: {campaign.completed}\n"
        f"Осталось: {remaining}\n"
        f"Цена: {format_amount_plain(buyer_price)} USDT/подп.\n\n"
        "Введите количество дополнительных подписчиков:",
    )
    await state.set_state(CampaignIncrease.entering_extra_count)
    await callback.answer()


@buyer_router.message(CampaignIncrease.entering_extra_count)
async def msg_campaign_increase_count(
    message: Message, session: AsyncSession, state: FSMContext,
) -> None:
    """Process extra count for campaign increase."""
    try:
        extra = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите целое число.")
        return

    if extra < 1:
        await message.answer("❌ Минимум 1 дополнительный подписчик.")
        return
    if extra > 100000:
        await message.answer("❌ Максимум 100 000 за раз.")
        return

    data = await state.get_data()
    buyer_price = from_db(data["buyer_price"])
    extra_cost = buyer_price * extra
    campaign_id = data["campaign_id"]

    # Check balance
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(message.from_user.id)
    balance_service = BalanceService(session)
    available, _, _ = await balance_service.get_balance(user.id)

    if available < extra_cost:
        await message.answer(
            f"❌ Недостаточно средств.\n"
            f"Нужно: {format_amount_plain(extra_cost)} USDT\n"
            f"Доступно: {format_amount_plain(available)} USDT"
        )
        return

    await state.update_data(extra=extra, extra_cost=str(extra_cost))

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=f"campaign:increase_confirm:{campaign_id}")
    kb.button(text="◀️ Отмена", callback_data=f"campaign:view:{campaign_id}")
    kb.adjust(2)

    await message.answer(
        f"📋 <b>Подтверждение увеличения</b>\n\n"
        f"➕ Добавить: {extra} подписчиков\n"
        f"💵 Стоимость: <b>{format_amount_plain(extra_cost)} USDT</b>",
        reply_markup=kb.as_markup(),
    )


@buyer_router.callback_query(F.data.startswith("campaign:increase_confirm:"))
async def cb_campaign_increase_confirm(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext,
) -> None:
    """Confirm campaign target increase — reserve funds and update target."""
    campaign_id = int(callback.data.split(":")[2])
    data = await state.get_data()
    extra = data.get("extra")
    extra_cost = from_db(data.get("extra_cost", "0"))

    campaign_service = CampaignService(session)
    campaign = await campaign_service.get_by_id(campaign_id)
    
    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)
    if not campaign or not user or campaign.user_id != user.id:
        return await callback.answer("Ошибка доступа", show_alert=True)

    if not extra:
        await callback.answer("Данные устарели. Начните заново.", show_alert=True)
        await state.clear()
        return

    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(callback.from_user.id)

    try:
        from app.db.engine import run_atomic
        
        async def _increase_op(write_session: AsyncSession):
            campaign_service_write = CampaignService(write_session)
            ok = await campaign_service_write.increase_target(
                campaign_id=campaign_id,
                additional=extra,
            )
            if not ok:
                return None
            return await campaign_service_write.get_by_id(campaign_id)
            
        campaign = await run_atomic(_increase_op)
        if not campaign:
            await callback.message.edit_text("❌ Кампания недоступна для увеличения цели.")
            await state.clear()
            await callback.answer()
            return
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}")
        await state.clear()
        await callback.answer()
        return

    await callback.message.edit_text(
        f"✅ Цель кампании увеличена!\n\n"
        f"📌 {campaign.target_title_snapshot}\n"
        f"🎯 Новая цель: {campaign.target} подписчиков\n"
        f"💵 Зарезервировано: {format_amount_plain(extra_cost)} USDT",
    )
    await state.clear()
    await callback.answer()


# ── Deposit flow ─────────────────────────────────────────────────────────────

@buyer_router.callback_query(F.data == "deposit:start")
async def cb_deposit_start(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext,
) -> None:
    """Start deposit flow — ask amount."""
    await callback.message.edit_text(
        "💳 <b>Пополнение баланса</b>\n\n"
        "Введите сумму в USDT (мин. 1.00):",
    )
    await state.set_state(DepositFlow.entering_amount)
    await callback.answer()


@buyer_router.message(DepositFlow.entering_amount)
async def msg_deposit_amount(
    message: Message, session: AsyncSession, state: FSMContext,
    crypto_pay: CryptoPayService | None,
) -> None:
    """Process deposit amount."""
    try:
        amount = round_down(Decimal(message.text.strip()))
    except Exception:
        await message.answer("❌ Введите корректную сумму.")
        return

    if not is_valid_amount(amount) or amount < Decimal("1.00"):
        await message.answer("❌ Минимальная сумма: 1.00 USDT")
        return

    user_service = UserService(session)
    user = await user_service.get_by_telegram_id(message.from_user.id)

    deposit_service = DepositService(session, crypto_pay)
    try:
        deposit = await deposit_service.create_deposit(user.id, amount)
    except Exception as e:
        await message.answer(f"❌ Ошибка создания платежа: {e}")
        await state.clear()
        return

    kb = InlineKeyboardBuilder()
    if deposit.pay_url:
        kb.button(text="💳 Оплатить", url=deposit.pay_url)
    kb.button(text="◀️ Меню", callback_data="main_menu")
    kb.adjust(1)

    text_repo = TextRepository(session)
    tpl = await text_repo.get_text("deposit_created")
    text = tpl.format(amount=format_amount_plain(amount))

    await message.answer(text, reply_markup=kb.as_markup())
    await state.clear()
