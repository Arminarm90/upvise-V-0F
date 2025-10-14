# app/handlers/payment.py
# -*- coding: utf-8 -*-
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, CallbackQueryHandler
from app.sub.payment_service import start_payment
from app.utils.i18n import t, get_chat_lang
from app.sub.payments_db import PaymentsDB # <- اضافه شدن این ایمپورت
import logging

LOG = logging.getLogger("payment")
db = PaymentsDB() 

# لیست پلن‌ها (همانند قبل)
PLANS = {
    "plan30": {"days": 30, "price": 1_000, "title_key": "payment.plan30_title"},
    "plan90": {"days": 90, "price": 250_000, "title_key": "payment.plan90_title"},
}

async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    lang = get_chat_lang(context.bot_data["store"], chat_id)

    sub_info = db.get_subscription_info(chat_id)

    if sub_info and sub_info["is_active"]:
        status_text = t("payment.subscription_status_active", lang).format(
            date=sub_info["end_date"].split("T")[0],
            days=sub_info["remaining_days"]
        )
    else:
        status_text = t("payment.subscription_status_inactive", lang)

    text = f"**{status_text}**\n\n{t('payment.plans_title', lang)}\n"

    currency = t("payment.currency", lang)
    for pid, plan in PLANS.items():
        text += f"▫️ {t(plan['title_key'], lang)} — **{plan['price']:,}** {currency}\n"

    buttons = [
        [InlineKeyboardButton(t(plan["title_key"], lang), callback_data=f"buy:{pid}")]
        for pid, plan in PLANS.items()
    ]
    reply_markup = InlineKeyboardMarkup(buttons)

    await update.effective_chat.send_message(text, reply_markup=reply_markup, parse_mode='Markdown')


async def cb_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = str(update.effective_chat.id)
    lang = get_chat_lang(context.bot_data["store"], chat_id)

    plan_id = query.data.replace("buy:", "")
    plan = PLANS.get(plan_id)
    if not plan:
        await query.edit_message_text(t("payment.invalid_plan", lang))
        return

    res = await start_payment(
        chat_id,
        amount=plan["price"] * 10,
        days=plan["days"],
        description=t(plan["title_key"], lang)
    )

    if res["ok"]:
        msg = t("payment.success_link", lang).split("\n")[0]  # فقط متن ساده
        button_text = t("payment.success_button", lang)

        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton(button_text, url=res["url"])]
        ])
        await query.edit_message_text(msg, reply_markup=buttons)
    else:
        LOG.error("Zarinpal payment error | chat=%s plan=%s error=%s", chat_id, plan_id, res.get("error"))
        msg = t("payment.error_transaction", lang)
        await query.edit_message_text(msg)




def get_payment_handlers():
    return [
        ("buy", cmd_buy),
        CallbackQueryHandler(cb_buy, pattern=r"^buy:"),
    ]
