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
from app.utils.decorators import premium_only

# --- State(s) for /add conversation
WAITING_FOR_URL = 1
WAITING_FOR_REMOVE_URL = 202

def _is_probably_url(s: str) -> bool:
    """
    Checks if input looks like a real URL.
    Avoids treating plain words (like 'apple' or '') as URLs.
    """
    s = (s or "").strip()
    if not s:
        return False

    # اگر کاربر فقط کلمه نوشته بدون نقطه، این URL نیست
    if "." not in s:
        return False

    try:
        u = ensure_scheme(s)
        p = urlparse(u)
        # scheme باید http/https باشه و netloc حداقل شامل نقطه باشه
        return bool(p.scheme in ("http", "https") and "." in (p.netloc or ""))
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
    text = t("add.ask_input", lang)
    if text == "add.ask_input":
        text = "🔗 Send a website link or a keyword to track:" if lang == "en" else "🔗 لینک سایت یا کلمه‌ای برای پیگیری بفرست:"
    sent = await update.effective_message.reply_text(text)
    await _maybe_auto_delete(context, chat_id, sent.message_id)
    return WAITING_FOR_URL


async def receive_site_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    store = context.bot_data["store"]
    rss = context.bot_data["rss"]
    search = context.bot_data.get("search")

    chat_id = update.effective_chat.id
    lang = get_chat_lang(store, chat_id)
    raw = (update.effective_message.text or "").strip()

    # ----------- Keyword Mode -----------
    if not _is_probably_url(raw):
        # اگر فقط متن ساده بود → به‌عنوان keyword ذخیره کن
        store.add_keyword(chat_id, raw)
        store.mark_action(chat_id)
        msg = t("add.keyword_added", lang)
        if msg == "add.keyword_added":
            msg = "✅ Keyword added!" if lang == "en" else "✅ کلمه کلیدی اضافه شد!"
        sent = await update.effective_message.reply_text(msg)
        await _maybe_auto_delete(context, chat_id, sent.message_id)
        return ConversationHandler.END

    # ----------- URL Mode -----------
    ack_msg = await update.effective_message.reply_text(t("add.checking", lang))
    await _maybe_auto_delete(context, chat_id, ack_msg.message_id)

    site = _canon(raw)

    # Our links 
    if "/vip/goldir" in site.lower():
        if store.add_feed(chat_id, site):
            store.mark_action(chat_id)
            await update.effective_message.reply_text(t("add.added_feed", lang))
        else: 
            await update.effective_message.reply_text(t("add.already_added", lang))
        return ConversationHandler.END

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
        pass

    best = None
    try:
        if search and hasattr(search, "discover_rss"):
            best = await search.discover_rss(site)
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
            pass

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
    msg = t("remove.ask_input", lang)
    if msg == "remove.ask_input":
        msg = "Send the site URL or keyword to remove:" if lang == "en" else "🔗 لینک سایت یا کلمه‌ای که می‌خوای حذف کنی بفرست:"
    sent = await update.effective_message.reply_text(msg)
    await _maybe_auto_delete(context, chat_id, sent.message_id)
    return WAITING_FOR_REMOVE_URL


async def handle_remove_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Step 2: user sends the URL → try to remove.
    """
    store = context.bot_data["store"]
    chat_id = update.effective_chat.id
    lang = get_chat_lang(store, chat_id)
    raw = (update.effective_message.text or "").strip()

    ok = False
    if _is_probably_url(raw):
        url = _canon(raw)
        ok = store.remove_feed(chat_id, url)
    else:
        keywords = store.list_keywords(chat_id)
        for idx, k in enumerate(keywords, start=1):
            if k["keyword"].lower() == raw.lower():
                ok = store.remove_keyword(chat_id, idx)
                break

    if ok:
        msg = t("sys.removed", lang)
        if msg == "sys.removed":
            msg = "✅ Removed." if lang == "en" else "✅ حذف شد."
    else:
        msg = t("remove.not_found", lang)
        if msg == "remove.not_found":
            msg = "❌ Not found." if lang == "en" else "❌ پیدا نشد."

    sent = await update.effective_message.reply_text(msg)
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
    keywords = store.list_keywords(chat_id)
    if not feeds and not keywords:
        msg = t("list.empty", lang)
        sent = await update.message.reply_text(msg)
        await _maybe_auto_delete(context, chat_id, sent.message_id)
        return

    msg_parts = []
    if feeds:
        msg_parts.append(t("list.feeds", lang) + ":\n" + "\n".join(f"{i+1}. {f}" for i, f in enumerate(feeds)))
    if keywords:
        msg_parts.append(t("list.keywords", lang) + ":\n" + "\n".join(f"{i+1}. {k['keyword']}" for i, k in enumerate(keywords)))

    msg = "\n\n".join(msg_parts)
    keyboard = [
        [InlineKeyboardButton(t("btn.add", lang), callback_data="list:add"),
         InlineKeyboardButton(t("btn.remove", lang), callback_data="list:remove")],
        [InlineKeyboardButton(t("btn.clear", lang), callback_data="list:clear")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    sent = await update.message.reply_text(msg, reply_markup=reply_markup, disable_web_page_preview=True)
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