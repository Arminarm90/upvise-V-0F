# app/utils/decorators.py
# -*- coding: utf-8 -*-
from functools import wraps
from telegram import Update
from telegram.ext import ContextTypes
from app.sub.payments_db import PaymentsDB 
from app.handlers.payment import cmd_buy # <-- ایمپورت cmd_buy
import logging
from app.utils.i18n import t, get_chat_lang

LOG = logging.getLogger("decorators")
db = PaymentsDB()

# تابع کمکی t (برای دکوریتور)
# def t(key):
#     if key == "premium_required_message":
#         return "🔒 **این قابلیت فقط برای کاربران اشتراکی فعال است.**\n\nلطفاً برای استفاده از این امکان، اشتراک تهیه کنید:"
#     return key


def premium_only():
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            chat_id = str(update.effective_chat.id)
            lang = get_chat_lang(context.bot_data["store"], chat_id)

            is_active = db.check_active_subscription(chat_id)

            if is_active:
                return await func(update, context, *args, **kwargs)
            else:
                text = t("payment.premium_required_message", lang)

                try:
                    if update.callback_query:
                        await update.callback_query.answer(text=t("payment.premium_required_message", lang), show_alert=True)
                        await update.effective_chat.send_message(text, parse_mode="Markdown")
                    elif update.message:
                        await update.message.reply_text(text, parse_mode="Markdown")
                    else:
                        await update.effective_chat.send_message(text, parse_mode="Markdown")
                except Exception as e:
                    LOG.warning("Failed to send premium required message to %s: %s", chat_id, e)

                await cmd_buy(update, context)
                return
        return wrapper
    return decorator