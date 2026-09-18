"""
Handler layer — aiogram routers for all user interactions.

Routers:
- common_router: /start, /help, session init
- buyer_router: campaigns, deposits, balance
- seller_router: groups, tasks, withdrawals
- admin_router: admin panel, moderation, settings
"""

from app.handlers.common import common_router
from app.handlers.buyer import buyer_router
from app.handlers.seller import seller_router
from app.handlers.admin import admin_router

__all__ = [
    "common_router",
    "buyer_router",
    "seller_router",
    "admin_router",
]
