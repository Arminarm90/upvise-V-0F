# app/handlers/basic.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes
from app.utils.i18n import t, get_chat_lang


async def _maybe_auto_delete(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    """اگر حالت Ephemeral فعال باشد، پیام را پس از زمان تنظیم‌شده حذف می‌کند."""
    try:
        if ctx.bot_data.get("ephemeral_mode", True):
            # _auto_delete در bot.py در bot_data قرار داده شده است
            auto_delete = ctx.bot_data.get("auto_delete")
            if callable(auto_delete):
                ctx.application.create_task(auto_delete(ctx, chat_id, message_id))
    except Exception:
        # حذف‌نشدن پیام (مثلاً به‌علت زمان زیاد/دسترسی) خطاگیر نیست
        pass


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lang = get_chat_lang(ctx.bot_data["store"], chat_id)

    # بلوک منو ۱۰۰٪ از i18n
    lines = [
        t("start.hello", lang),
        "",
        f"/start — {t('menu.start', lang)}",
        f"/add — {t('menu.add', lang)}",
        f"/list — {t('menu.list', lang)}",
        f"/remove — {t('menu.remove', lang)}",
        f"/lang — {t('menu.lang', lang)}",
        f"/help — {t('menu.help', lang)}",
        "",
        t("start.commands_hint", lang),
    ]
    msg_text = "\n".join(lines)

    sent = await update.effective_message.reply_text(
        msg_text,
        # در این پیام‌ها HTML لازم نیست
        disable_web_page_preview=True,
    )
    await _maybe_auto_delete(ctx, chat_id, sent.message_id)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    نسخه امن: parse_mode را حذف کردیم تا اگر در i18n تگی مثل <url> وجود داشت،
    تلگرام آن را به‌عنوان HTML پارس نکند و خطای
    `BadRequest: unsupported start tag "url"` ندهد.
    """
    chat_id = update.effective_chat.id
    lang = get_chat_lang(ctx.bot_data["store"], chat_id)

    msg = t("help.text", lang)
    sent = await update.effective_message.reply_text(
        msg,
        # مهم: parse_mode را ست نکن! (نه "HTML" و نه "Markdown")
        disable_web_page_preview=True,
    )
    await _maybe_auto_delete(ctx, chat_id, sent.message_id)
