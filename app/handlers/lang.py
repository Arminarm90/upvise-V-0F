# app/handlers/lang.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
    BotCommandScopeChat,
)
from telegram.ext import ContextTypes
from app.utils.i18n import t, get_chat_lang, set_chat_lang

SUPPORTED = {"fa": "فارسی", "en": "English"}

async def _maybe_auto_delete(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    try:
        if ctx.bot_data.get("ephemeral_mode", True):
            auto_delete = ctx.bot_data.get("auto_delete")
            if callable(auto_delete):
                ctx.application.create_task(auto_delete(ctx, chat_id, message_id))
    except Exception:
        pass

async def cmd_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    store = ctx.bot_data["store"]
    chat_id = update.effective_chat.id
    lang = get_chat_lang(store, chat_id)

    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(t("btn.fa", lang), callback_data="lang:fa"),
            InlineKeyboardButton(t("btn.en", lang), callback_data="lang:en"),
        ]]
    )
    sent = await update.effective_message.reply_text(
        t("lang.choose", lang), reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True
    )
    await _maybe_auto_delete(ctx, chat_id, sent.message_id)

async def cb_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    # الگوهای قابل قبول:
    # "lang:fa" یا "lang:en"  ← صفحه مستقل تنظیم زبان
    # "lang:fa:start" یا "lang:en:start" ← دکمه‌های زیر پیام خوش‌آمد
    parts = (q.data or "lang:fa").split(":")
    code = parts[1] if len(parts) >= 2 else "fa"
    origin = parts[2] if len(parts) >= 3 else ""   # ممکن است 'start' باشد

    if code not in SUPPORTED:
        code = "fa"

    store = ctx.bot_data["store"]
    chat_id = q.message.chat.id
    set_chat_lang(store, chat_id, code)

    # منوی همان چت به زبان جدید
    try:
        cmds = [
            BotCommand("start",  t("menu.start", code)),
            BotCommand("add",    t("menu.add", code)),
            BotCommand("list",   t("menu.list", code)),
            BotCommand("remove", t("menu.remove", code)),
            BotCommand("lang",   t("menu.lang", code)),
            BotCommand("help",   t("menu.help", code)),
        ]
        await ctx.bot.set_my_commands(cmds, scope=BotCommandScopeChat(chat_id))
    except Exception:
        pass

    # اگر منبع انتخاب زبان «پیام خوش‌آمد» است، همان پیام را ادیت کن
    if origin == "start":
        try:
            # import محلی برای جلوگیری از حلقه‌ی import
            from .basic import render_welcome
            text, kb = render_welcome(code)
            edited = await q.edit_message_text(
                text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True
            )
            # پیام خوش‌آمد معمولاً ماندگار است؛ اگر دوست دارید، حذف موقت نکنید.
            # await _maybe_auto_delete(ctx, chat_id, edited.message_id)
            return
        except Exception:
            pass  # اگر ادیت نشد، مسیر عادی ادامه یابد

    # مسیر عادی: نمایش پیام «زبان تغییر کرد» + کیبورد انتخاب زبان
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(t("btn.fa", code), callback_data="lang:fa"),
            InlineKeyboardButton(t("btn.en", code), callback_data="lang:en"),
        ]]
    )
    msg_text = t("lang.set", code, lang_name=SUPPORTED[code])

    try:
        await q.edit_message_text(
            msg_text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True
        )
    except Exception:
        await q.message.reply_text(
            msg_text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True
        )
