# app/handlers/feeds.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CommandHandler,
    filters,
    CallbackQueryHandler
)

from ..utils.i18n import t, get_chat_lang
from ..utils.text import ensure_scheme, canonicalize_url
from app.config import settings  # تنظیمات برای Ephemeral و ...
from . import basic
from .lang import cmd_lang
from .list import cmd_list
# --- State(s) for /add conversation
WAITING_FOR_URL = 1
WAITING_FOR_REMOVE_URL = 202

def _is_probably_url(s: str) -> bool:
    try:
        u = ensure_scheme((s or "").strip())
        p = urlparse(u)
        return bool(p.scheme in ("http", "https") and p.netloc)
    except Exception:
        return False


def _canon(url: str) -> str:
    """تضمین scheme و اعمال canonicalization (در حد امکان)."""
    try:
        return canonicalize_url(ensure_scheme(url), strip_query_tracking=True)  # type: ignore[arg-type]
    except Exception:
        return ensure_scheme(url)


async def _maybe_auto_delete(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    """اگر حالت Ephemeral فعال باشد، پیام را پس از زمان تنظیم‌شده حذف می‌کند."""
    try:
        if ctx.bot_data.get("ephemeral_mode", True):
            auto_delete = ctx.bot_data.get("auto_delete")
            if callable(auto_delete):
                ctx.application.create_task(auto_delete(ctx, chat_id, message_id))
    except Exception:
        pass


# ========== ADD (2-step) ==========
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Entry point for /add conversation.
    Step 1: ask user to send a site URL (not RSS).
    """
    chat_id = update.effective_chat.id
    lang = get_chat_lang(context.bot_data["store"], chat_id)

    sent = await update.effective_message.reply_text(t("add.ask_site", lang))
    await _maybe_auto_delete(context, chat_id, sent.message_id)
    return WAITING_FOR_URL


async def receive_site_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Step 2: receive user's site URL, try to discover RSS; if not found, enable Page-Watch.
    """
    store = context.bot_data["store"]
    rss = context.bot_data["rss"]
    search = context.bot_data.get("search")  # ممکن است در این لحظه هنوز کامل نباشد

    chat_id = update.effective_chat.id
    lang = get_chat_lang(store, chat_id)

    raw = (update.effective_message.text or "").strip()
    if not _is_probably_url(raw):
        msg = await update.effective_message.reply_text(t("add.invalid_url", lang))
        await _maybe_auto_delete(context, chat_id, msg.message_id)
        return WAITING_FOR_URL

    # Ack موقت: در حال بررسی لینک…
    ack_msg = await update.effective_message.reply_text(t("add.checking", lang))
    await _maybe_auto_delete(context, chat_id, ack_msg.message_id)

    site = _canon(raw)

    # 1) اگر خودِ ورودی RSS معتبر بود (کاربر حرفه‌ای)، مستقیم اضافه کن
    try:
        if await rss.is_valid_feed(site):
            if store.add_feed(chat_id, site):
                store.mark_action(chat_id)
                m = await update.effective_message.reply_text(t("add.added_feed", lang))
            else:
                m = await update.effective_message.reply_text(t("add.already_added", lang))
            await _maybe_auto_delete(context, chat_id, m.message_id)
            return ConversationHandler.END
    except Exception:
        # ادامهٔ مسیر دیسکاوری
        pass

    # 2) تلاش برای کشف RSS
    best = None
    try:
        if search and hasattr(search, "discover_rss"):
            best = await search.discover_rss(site)  # services/search.py
            if best:
                best = _canon(best)
    except Exception:
        best = None

    if best:
        try:
            if await rss.is_valid_feed(best):
                if store.add_feed(chat_id, best):
                    store.mark_action(chat_id)
                    m = await update.effective_message.reply_text(t("add.feed_found_added", lang))
                else:
                    m = await update.effective_message.reply_text(t("add.already_added", lang))
                await _maybe_auto_delete(context, chat_id, m.message_id)
                return ConversationHandler.END
        except Exception:
            # سقوط به Page‑Watch
            pass

    # 3) اگر RSS پیدا نشد یا معتبر نبود → Page-Watch روی خود سایت
    try:
        if store.add_feed(chat_id, site):
            store.mark_action(chat_id)
            m = await update.effective_message.reply_text(t("add.pagewatch_enabled", lang))
        else:
            m = await update.effective_message.reply_text(t("add.already_added", lang))
    except Exception:
        m = await update.effective_message.reply_text(t("add.error_generic", lang))

    await _maybe_auto_delete(context, chat_id, m.message_id)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    lang = get_chat_lang(context.bot_data["store"], chat_id)
    m = await update.effective_message.reply_text(t("add.cancelled", lang))
    await _maybe_auto_delete(context, chat_id, m.message_id)
    return ConversationHandler.END

async def silent_cancel_and_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Exits the /add conversation silently and immediately
    executes the new command entered by the user.
    """
    command = update.effective_message.text.split()[0] if update.effective_message.text else ""

    if command == "/list":
        await cmd_list(update, context)
    elif command == "/remove":
        await cmd_remove(update, context)        
    elif command == "/lang":
        await cmd_lang(update, context)
    elif command == "/help":
        await basic.cmd_help(update, context)
    # اینجا می‌توانید دستورات دیگری که نیاز دارید را اضافه کنید

    return ConversationHandler.END

def get_add_conversation_handler() -> ConversationHandler:
    """
    Helper to register in bot.py:
        app.add_handler(get_add_conversation_handler())
    """
    return ConversationHandler(
        entry_points=[
            CommandHandler("add", cmd_add),
            CallbackQueryHandler(cmd_add, pattern=r"^list:add$")
            ],
        states={
            WAITING_FOR_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_site_url)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(filters.COMMAND, silent_cancel_and_execute)
        ],
        name="add_conv",
        persistent=False,
    )

# ========== REMOVE (i18n + canonical + ephemeral) ==========
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Entry point for /remove conversation.
    Step 1: ask user to send a site URL (not RSS).
    """
    chat_id = update.effective_chat.id
    lang = get_chat_lang(context.bot_data["store"], chat_id)

    sent = await update.effective_message.reply_text(
        t("remove.ask_site", lang) if t("remove.ask_site", lang) != "remove.ask_site"
        else ("Send the site URL to remove:" if lang == "en" else "🔗 لینک سایت برای حذف بده:")
    )
    await _maybe_auto_delete(context, chat_id, sent.message_id)
    return WAITING_FOR_REMOVE_URL


async def handle_remove_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Step 2: user sends the URL → try to remove.
    """
    store = context.bot_data["store"]
    chat_id = update.effective_chat.id
    lang = get_chat_lang(store, chat_id)

    url = _canon(update.message.text.strip())
    try:
        ok = store.remove_feed(chat_id, url)
    except Exception:
        ok = False

    if ok:
        text = t("sys.removed", lang)
    else:
        nf = t("remove.not_found", lang)
        text = nf if nf != "remove.not_found" else ("Not found." if lang == "en" else "❌ پیدا نشد.")

    sent = await update.message.reply_text(text)
    await _maybe_auto_delete(context, chat_id, sent.message_id)
    return ConversationHandler.END

def get_remove_conversation_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("remove", cmd_remove),
            CallbackQueryHandler(cmd_remove, pattern=r"^list:remove$"),
            ],
        states={
            WAITING_FOR_REMOVE_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_remove_url),
            ],
        },
        fallbacks=[
            # هر کامند جدیدی بیاد → کانورسیشن تموم شه
            MessageHandler(filters.COMMAND, silent_cancel_and_execute),
        ],
        allow_reentry=True,  # بذار دوباره بشه همون لحظه شروع کرد
    )
# async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     store = context.bot_data["store"]
#     chat_id = update.effective_chat.id
#     lang = get_chat_lang(store, chat_id)

#     # پیام راهنما؛ اگر کلید i18n نبود، fallback هوشمند
#     usage = t("remove.usage", lang)
#     if usage == "remove.usage":
#         usage = "Usage: /remove <url>" if lang == "en" else "لینک بده: /remove <url>"

#     if not context.args:
#         sent = await update.message.reply_text(usage)
#         await _maybe_auto_delete(context, chat_id, sent.message_id)
#         return

#     url = _canon(context.args[0])
#     try:
#         ok = store.remove_feed(chat_id, url)
#     except Exception:
#         ok = False

#     if ok:
#         text = t("sys.removed", lang)
#     else:
#         nf = t("remove.not_found", lang)
#         text = nf if nf != "remove.not_found" else ("Not found." if lang == "en" else "❌ پیدا نشد.")

#     sent = await update.message.reply_text(text)
#     await _maybe_auto_delete(context, chat_id, sent.message_id)


# ========== (اختیاری/غیرمصرفی) لیست ساده داخل این فایل ==========
# /list اصلی در handlers/list.py پیاده‌سازی شده؛ این فقط برای سازگاری قدیمی است.
# -------------------------
# /list command
# -------------------------
async def list_feeds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store = context.bot_data["store"]
    chat_id = update.effective_chat.id
    lang = get_chat_lang(store, chat_id)

    feeds = store.list_feeds(chat_id)
    if not feeds:
        sent = await update.message.reply_text(t("list.empty", lang))
        await _maybe_auto_delete(context, chat_id, sent.message_id)
        return

    body = "\n".join(f"{i+1}. {u}" for i, u in enumerate(feeds))
    msg = f"{t('list.title', lang)}\n{body}"

    # دکمه‌ها با i18n
    keyboard = [
        [
            InlineKeyboardButton(t("btn.add", lang), callback_data="list:add"),
            InlineKeyboardButton(t("btn.remove", lang), callback_data="list:remove"),
        ],
        [
            InlineKeyboardButton(t("btn.clear", lang), callback_data="list:clear"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    sent = await update.message.reply_text(
        msg,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=reply_markup
    )
    await _maybe_auto_delete(context, chat_id, sent.message_id)


# -------------------------
# handle list buttons
# -------------------------
async def cb_list_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # جلوگیری از "Loading..."

    data = query.data
    chat_id = update.effective_chat.id
    store = context.bot_data["store"]
    lang = get_chat_lang(store, chat_id)

    if data == "list:clear":
        store.clear_feeds(chat_id)
        await query.edit_message_text(t("list.cleared", lang))

    elif data == "list:add":
        # اینجا همون ConversationHandler مربوط به /add وارد میشه
        return await cmd_add(update, context)

    elif data == "list:remove":
        # اینجا همون ConversationHandler مربوط به /remove وارد میشه
        return await cmd_remove(update, context)