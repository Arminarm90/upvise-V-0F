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
    """اگر حالت Ephemeral فعال باشد، پیام را پس از زمان تنظیم‌شده حذف می‌کند."""
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
        [
            [
                InlineKeyboardButton(t("btn.fa", lang), callback_data="lang:fa"),
                InlineKeyboardButton(t("btn.en", lang), callback_data="lang:en"),
            ]
        ]
    )
    sent = await update.effective_message.reply_text(
        t("lang.choose", lang),
        reply_markup=kb,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await _maybe_auto_delete(ctx, chat_id, sent.message_id)


async def cb_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    # استخراج مطمئن کد زبان
    data = (q.data or "lang:fa")
    parts = data.split(":", 1)
    code = parts[1].strip().lower() if len(parts) == 2 else "fa"
    if code not in SUPPORTED:
        code = "fa"

    store = ctx.bot_data["store"]
    chat_id = q.message.chat.id

    # ذخیره در StateStore
    set_chat_lang(store, chat_id, code)

    # بلافاصله Summarizer را هم روی همین زبان ست کن (برای خلاصه‌های بعدی)
    try:
        if "summarizer" in ctx.bot_data and ctx.bot_data["summarizer"]:
            ctx.bot_data["summarizer"].prompt_lang = code
    except Exception:
        pass

    # منوی همان چت به زبان جدید
    try:
        cmds = [
            BotCommand("start",  t("menu.start",  code)),
            BotCommand("add",    t("menu.add",    code)),
            BotCommand("list",   t("menu.list",   code)),
            BotCommand("remove", t("menu.remove", code)),
            BotCommand("lang",   t("menu.lang",   code)),
            BotCommand("help",   t("menu.help",   code)),
        ]
        await ctx.bot.set_my_commands(cmds, scope=BotCommandScopeChat(chat_id))
    except Exception:
        pass

    # بازسازی کیبورد با برچسب‌های زبان جدید
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t("btn.fa", code), callback_data="lang:fa"),
                InlineKeyboardButton(t("btn.en", code), callback_data="lang:en"),
            ]
        ]
    )
    msg_text = t("lang.set", code, lang_name=SUPPORTED[code])

    # تلاش برای ویرایش همان پیام؛ در صورت خطا، پیام جدید می‌فرستیم
    sent_msg_id = None
    try:
        edited = await q.edit_message_text(
            msg_text,
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        sent_msg_id = getattr(edited, "message_id", None)
    except Exception:
        try:
            sent = await q.message.reply_text(
                msg_text,
                reply_markup=kb,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            sent_msg_id = sent.message_id
        except Exception:
            pass

    if sent_msg_id:
        await _maybe_auto_delete(ctx, chat_id, sent_msg_id)
