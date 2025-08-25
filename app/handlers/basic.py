# app/handlers/basic.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from app.utils.i18n import t, get_chat_lang

def _maybe_auto_delete(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    try:
        if ctx.bot_data.get("ephemeral_mode", True):
            auto_delete = ctx.bot_data.get("auto_delete")
            if callable(auto_delete):
                ctx.application.create_task(auto_delete(ctx, chat_id, message_id))
    except Exception:
        pass

def render_welcome(lang: str) -> tuple[str, InlineKeyboardMarkup]:
    """
    متن کامل خوش‌آمد + منو + دکمه‌های تغییر زبان که callback_data آن‌ها
    دارای suffix ':start' است تا cb_lang بفهمد باید همین پیام را ادیت کند.
    """
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
    text = "\n".join(lines)

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t("btn.fa", lang), callback_data="lang:fa:start"),
                InlineKeyboardButton(t("btn.en", lang), callback_data="lang:en:start"),
            ]
        ]
    )
    return text, kb

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lang = get_chat_lang(ctx.bot_data["store"], chat_id)

    text, kb = render_welcome(lang)
    sent = await update.effective_message.reply_text(
        text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True
    )
    await _maybe_auto_delete(ctx, chat_id, sent.message_id)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lang = get_chat_lang(ctx.bot_data["store"], chat_id)

    msg = t("help.text", lang)
    sent = await update.effective_message.reply_text(
        msg, parse_mode="HTML", disable_web_page_preview=True
    )
    await _maybe_auto_delete(ctx, chat_id, sent.message_id)
