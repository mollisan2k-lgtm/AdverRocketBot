import asyncio
from sqlalchemy import update
from app.db.engine import session_factory
from app.db.models import BotText

async def update_texts():
    async with session_factory() as session:
        await session.execute(
            update(BotText).where(BotText.key == "balance_info").values(
                text="💰 <b>Ваш баланс</b>\n\nДоступно: <code>{available}</code> USDT\nВ резерве: <code>{reserved}</code> USDT\nВ холде: <code>{held}</code> USDT\nИтого: <code>{total}</code> USDT",
                default_text="💰 <b>Ваш баланс</b>\n\nДоступно: <code>{available}</code> USDT\nВ резерве: <code>{reserved}</code> USDT\nВ холде: <code>{held}</code> USDT\nИтого: <code>{total}</code> USDT"
            )
        )
        await session.execute(
            update(BotText).where(BotText.key == "help").values(
                text="❓ <b>Справка</b>\n\n• /start — Главное меню\n• Все остальные действия (Баланс, Кампании, Ввод/Вывод) доступны через кнопки в главном меню.\n\nПо вопросам: @{support}",
                default_text="❓ <b>Справка</b>\n\n• /start — Главное меню\n• Все остальные действия (Баланс, Кампании, Ввод/Вывод) доступны через кнопки в главном меню.\n\nПо вопросам: @{support}"
            )
        )
        await session.commit()
        print("Updated texts!")

if __name__ == "__main__":
    asyncio.run(update_texts())
