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
import logging
from app.services.rss import AI_FEEDS 

LOG = logging.getLogger("feeds")

# --- State(s) for /add conversation
WAITING_FOR_URL = 1
WAITING_FOR_TARGET = 2

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


# --- تغییرات لازم در بالای فایل: تغییر امضای ask_target و ذخیره نوع
async def ask_target(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str, kind: str = "feed"):
    """
    مرحله انتخاب مقصد ارسال (خود کاربر یا یکی از گروه‌ها/کانال‌هایش)
    """
    pass
    # store = context.bot_data["store"]
    # chat_id = update.effective_chat.id
    # lang = get_chat_lang(context.bot_data["store"], chat_id)

    # # گرفتن لیست گروه‌ها و کانال‌هایی که کاربر مالکشان است
    # with store._locked_cursor() as cur:
    #     cur.execute("SELECT chat_id, name FROM chats WHERE owner_id = ?", (chat_id,))
    #     rows = cur.fetchall()

    # buttons = [
    #     [InlineKeyboardButton(t("add.target.self", lang), callback_data="target:self")]
    # ]

    # # اگر گروه یا کانالی از قبل اضافه کرده بود، آن‌ها را هم نشان بده
    # if rows:
    #     buttons.append([InlineKeyboardButton(t("add.target.my_groups_title", lang), callback_data="noop")])
    #     for idx, r in enumerate(rows, start=1):
    #         gname = (r["name"] or "").strip()
    #         if not gname:
    #             gname = t("add.target.untitled_group", lang).replace("{n}", str(idx))

    #         label = f"{gname}"
    #         buttons.append([
    #             InlineKeyboardButton(label, callback_data=f"target:existing:{r['chat_id']}")
    #         ])

    # # گزینه برای افزودن گروه/کانال جدید
    # buttons.append([InlineKeyboardButton(t("add.target.add_new_group", lang), callback_data="target:other")])

    # markup = InlineKeyboardMarkup(buttons)

    # await update.effective_message.reply_text(
    #     t("add.target.ask_choose_target", lang),
    #     reply_markup=markup
    # )

    # # نگه‌داشتن داده‌های موقت
    # context.user_data["pending_payload"] = payload
    # context.user_data["pending_kind"] = kind
    # return WAITING_FOR_TARGET




async def receive_site_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    store = context.bot_data["store"]
    rss = context.bot_data["rss"]
    search = context.bot_data.get("search")

    chat_id = update.effective_chat.id
    lang = get_chat_lang(store, chat_id)
    raw = (update.effective_message.text or "").strip()

    # ----------- Keyword Mode -----------
    if not _is_probably_url(raw):
        store.add_keyword(chat_id, raw)
        store.mark_action(chat_id)
        

        msg = t("add.keyword_added", lang)
        if msg == "add.keyword_added":
            msg = "✅ Keyword added!" if lang == "en" else "✅ کلمه کلیدی اضافه شد!"
        sent = await update.effective_message.reply_text(msg)
        
        # 🚀 اجرای خودکار هوش مصنوعی برای پیدا کردن RSS ها
        try:
            added_count = await rss.find_and_add_ai_feeds(raw)
            LOG.info("AI added %d feeds for keyword '%s'", added_count, raw)
        except Exception as e:
            LOG.error("AI feed discovery failed for keyword '%s': %s", raw, e)

        await _maybe_auto_delete(context, chat_id, sent.message_id)
        return ConversationHandler.END
        
        # تا زمان انتخاب مقصد، فقط payload رو نگه دار (نوع: keyword)
        # msg = t("add.keyword_added", lang)
        # if msg == "add.keyword_added":
        #     msg = "✅ Keyword noted — choose target." if lang == "en" else "✅ کلمهٔ کلیدی ثبت شد — مقصد را انتخاب کنید."
        # sent = await update.effective_message.reply_text(msg)
        # await _maybe_auto_delete(context, chat_id, sent.message_id)
        # return await ask_target(update, context, raw, kind="keyword")

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
    
    if "divar.ir/s/" in site.lower():
        if store.add_feed(chat_id, site):
            store.mark_action(chat_id)
            await update.effective_message.reply_text(t("add.added_feed", lang))
        else: 
            await update.effective_message.reply_text(t("add.already_added", lang))
        return ConversationHandler.END
    
    if "https://www.khanoumi.com/tags/takhfif50" in site.lower():
        if store.add_feed(chat_id, site):
            store.mark_action(chat_id)
            await update.effective_message.reply_text(t("add.added_feed", lang))
        else: 
            await update.effective_message.reply_text(t("add.already_added", lang))
        return ConversationHandler.END

    if "takhfifan.com" in site.lower():
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

    # Group and Channel add
    
    # # provider shortcuts — but do NOT add to store here, فقط payload را تعیین کن
    # if "/vip/goldir" in site.lower() or "divar.ir/s/" in site.lower() or "takhfifan.com" in site.lower() or "khanoumi.com" in site.lower():
    #     # برای این providerها معمولا خود site یک شناسه‌ی special است — استفاده از site به عنوان payload
    #     info_msg = t("add.added_feed", lang)
    #     # if info_msg == "add.added_feed":
    #     #     info_msg = "✅ Feed detected — choose target." if lang == "en" else "✅ فید شناسایی شد — مقصد را انتخاب کنید."
    #     # await update.effective_message.reply_text(info_msg)
    #     # return await ask_target(update, context, site, kind="feed")

    # # اگر خودِ site یک فید واقعی است:
    # try:
    #     if await rss.is_valid_feed(site):
    #         # site یک فید است — استفاده کن
    #         info_msg = t("add.added_feed", lang)
    #         # if info_msg == "add.added_feed":
    #         #     info_msg = "✅ Feed found — choose target." if lang == "en" else "✅ فید یافت شد — مقصد را انتخاب کنید."
    #         # await update.effective_message.reply_text(info_msg)
    #         # return await ask_target(update, context, site, kind="feed")
    # except Exception:
    #     pass

    # # تلاش برای پیدا کردن فید از طریق search.discover_rss
    # best = None
    # try:
    #     if search and hasattr(search, "discover_rss"):
    #         best = await search.discover_rss(site)
    #         if best:
    #             best = _canon(best)
    # except Exception:
    #     best = None

    # if best:
    #     try:
    #         # info_msg = t("add.feed_found_added", lang)
    #         # if info_msg == "add.feed_found_added":
    #         #     info_msg = "✅ Feed discovered — choose target." if lang == "en" else "✅ فید کشف شد — مقصد را انتخاب کنید."
    #         # await update.effective_message.reply_text(info_msg)
    #         # return await ask_target(update, context, best, kind="feed")
    #     except Exception:
    #         pass

    # # fallback: pagewatch (store page URL as "feed" to be page-watched)
    # try:
    #     # info_msg = t("add.pagewatch_enabled", lang)
    #     # if info_msg == "add.pagewatch_enabled":
    #     #     info_msg = "✅ Page-watch enabled — choose target." if lang == "en" else "✅ پایش صفحه فعال شد — مقصد را انتخاب کنید."
    #     # await update.effective_message.reply_text(info_msg)
    #     # return await ask_target(update, context, site, kind="feed")
    # except Exception:
    #     await update.effective_message.reply_text(t("add.error_generic", lang))
    #     return ConversationHandler.END




async def receive_target_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pass
#     query = update.callback_query
#     await query.answer()
#     data = query.data
#     chat_id = update.effective_chat.id
#     store = context.bot_data["store"]
#     rss = context.bot_data["rss"]
#     lang = get_chat_lang(context.bot_data["store"], chat_id)

#     if data == "target:self":
#         payload = context.user_data.get("pending_payload")
#         kind = context.user_data.get("pending_kind", "feed")
#         if not payload:
#             # await query.edit_message_text("⛔ موردی برای ثبت پیدا نشد.")
#             return ConversationHandler.END

#         if kind == "keyword":
#             context.bot_data["store"].add_keyword(chat_id, payload)
#             store.add_keyword(chat_id, payload)
            
#             # --- منطق جدید: کشف فید AI ---
#             try:
#                 # ⛔️ حتماً 'payload' را به عنوان آرگومان ارسال کنید
#                 added_count = await rss.find_and_add_ai_feeds(payload) 
#                 if added_count > 0:
#                     LOG.info("AI added %d feeds for keyword '%s'", added_count, payload)
#             except Exception as e:
#                 LOG.error("AI feed discovery failed for keyword '%s': %s", payload, e)
            
#         else:
#             context.bot_data["store"].add_feed(chat_id, payload)

#         await query.edit_message_text(t("add.target.add_to_self", lang))
#         context.user_data.clear()
#         return ConversationHandler.END

#     elif data.startswith("target:existing:"):
#         target_id = data.split(":")[2]
#         payload = context.user_data.get("pending_payload")
#         kind = context.user_data.get("pending_kind", "feed")
#         # store = context.bot_data["store"] # حذف این خط چون بالا تعریف شد

#         if not payload:
#             # await query.edit_message_text("⛔ چیزی برای افزودن پیدا نشد.")
#             return ConversationHandler.END

#         # افزودن فید یا کلیدواژه به گروه/کانال انتخاب‌شده
#         if kind == "keyword":
#             store.add_keyword(target_id, payload)
#             # --- منطق جدید: کشف فید AI ---
#             try:
#                 # ⛔️ حتماً 'payload' را به عنوان آرگومان ارسال کنید
#                 added_count = await rss.find_and_add_ai_feeds(payload) 
#                 if added_count > 0:
#                     LOG.info("AI added %d feeds for keyword '%s' (via target:existing)", added_count, payload)
#             except Exception as e:
#                 LOG.error("AI feed discovery failed for keyword '%s': %s", payload, e)
#             # ---------------------------
#         # else:
#         #     store.add_feed(target_id, payload)

#         await query.edit_message_text(t("add.target.add_to_gp", lang))
#         context.user_data.clear()
#         return ConversationHandler.END

# async def handle_add_new_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     """وقتی کاربر دکمه «افزودن گروه یا کانال جدید» رو می‌زنه."""
#     query = update.callback_query
#     await query.answer()
#     chat_id = update.effective_chat.id
#     lang = get_chat_lang(context.bot_data["store"], chat_id)

#     context.user_data["awaiting_group_join"] = True

#     await query.edit_message_text(t("add.target.now_add", lang))

# async def confirm_added_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     query = update.callback_query
#     await query.answer()
#     chat_id = update.effective_chat.id
#     lang = get_chat_lang(context.bot_data["store"], chat_id)
    
#     if context.user_data.get("awaiting_group_join"):
#         await query.edit_message_text(t("add.target.waiting", lang))
#     # else:
#     #     await query.edit_message_text("❌ چیزی برای افزودن یافت نشد.")


# async def receive_target_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
#     target = (update.message.text or "").strip()
#     payload = context.user_data.get("pending_payload")
#     kind = context.user_data.get("pending_kind", "feed")
#     store = context.bot_data["store"]
#     rss = context.bot_data["rss"]
#     chat_id = update.effective_chat.id
#     lang = get_chat_lang(context.bot_data["store"], chat_id)
    
#     if not payload:
#         # await update.message.reply_text("⛔ لینک/کلمه‌ای برای ثبت پیدا نشد، لطفاً دوباره /add کنید.")
#         return ConversationHandler.END

#     # ممکنه کاربر username (@name) یا numeric id فرستاده باشه.
#     # ما همین رشته رو به عنوان chat_id ذخیره می‌کنیم؛ اگر نیاز داری numeric تبدیل بشه،
#     # می‌تونی validate/convert کنی (مثلاً اگر رشته startswith("@") باشه نگه دار).
#     target_id = target

#     if kind == "keyword":
#         store.add_keyword(target_id, payload)
        
#         # --- منطق جدید: کشف فید AI ---
#         try:
#             # ⛔️ حتماً 'payload' را به عنوان آرگومان ارسال کنید
#             added_count = await rss.find_and_add_ai_feeds(payload) 
#             if added_count > 0:
#                 LOG.info("AI added %d feeds for keyword '%s' (via receive_target_chat)", added_count, payload)
#         except Exception as e:
#             LOG.error("AI feed discovery failed for keyword '%s': %s", payload, e)
#         # ---------------------------
        
#     # else:
#     #     store.add_feed(target_id, payload)

#     await update.message.reply_text(f"{t("add.target.add_to_self", lang)} {target} ✅")
#     context.user_data.pop("pending_payload", None)
#     context.user_data.pop("pending_kind", None)
#     return ConversationHandler.END



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
        await list_feeds(update, context)
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
                
            # Group and Channel add
            # WAITING_FOR_TARGET: [
            #     CallbackQueryHandler(receive_target_choice, pattern=r"^target:(self|existing:)"),
            #     CallbackQueryHandler(handle_add_new_group, pattern=r"^target:other$"),
            #     MessageHandler(filters.TEXT & ~filters.COMMAND, receive_target_chat),
            #     CallbackQueryHandler(confirm_added_callback, pattern="^target:confirm_added$"),

            # ],            
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

    # ✅ چت‌هایی که متعلق به این کاربر هستن
    owned_chat_ids = [chat_id]
    with store._locked_cursor() as cur:
        cur.execute("SELECT chat_id FROM chats WHERE owner_id = ?", (chat_id,))
        owned_chat_ids.extend([r["chat_id"] for r in cur.fetchall()])

    ok = False
    if _is_probably_url(raw):
        url = _canon(raw)
        # حذف از همه‌ی چت‌های مالک
        for cid in owned_chat_ids:
            if store.remove_feed(cid, url):
                ok = True
    else:
        # برای کلیدواژه
        for cid in owned_chat_ids:
            keywords = store.list_keywords(cid)
            for idx, k in enumerate(keywords, start=1):
                if k["keyword"].lower() == raw.lower():
                    ok = store.remove_keyword(cid, idx)
                    break
            if ok:
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

    # فیدها و کلیدواژه‌های خود کاربر
    feeds = store.list_feeds(chat_id)
    keywords = store.list_keywords(chat_id)

    # ✅ اضافه کردن فیدها و کلیدواژه‌های گروه‌ها/کانال‌هایی که owner_id == chat_id
    with store._locked_cursor() as cur:
        cur.execute("SELECT chat_id, name FROM chats WHERE owner_id = ?", (chat_id,))
        owned_chats = cur.fetchall()

    for r in owned_chats:
        gid = r["chat_id"]
        # gname = r["name"] or f"ID {gid}"
        g_feeds = store.list_feeds(gid)
        g_keywords = store.list_keywords(gid)

        # هر آیتم رو با نام چنل/گروه اضافه می‌کنیم (برچسب‌دار)
        for f in g_feeds:
            feeds.append(f"{f}")
        for k in g_keywords:
            keywords.append({"keyword": f"{k['keyword']}"})

    # ✅ فیلتر کردن فیدهای سیستمی مثل divar_seen::
    # feeds = [
    #     f for f in feeds
    #     if not f.startswith("divar_seen::")
    #     and f not in AI_FEEDS
    # ]
    
    # --- فیدهای سیستمی و هوش‌مصنوعی نباید به کاربر نمایش داده شوند ---
    system_patterns = [
        "divar_seen::",
        "/vip/goldir",
        "divar.ir/s/",
        "takhfifan.com",
        "khanoumi.com/tags/takhfif50"
    ]

    system_ai_feeds = set(AI_FEEDS.keys()) if isinstance(AI_FEEDS, dict) else set(AI_FEEDS)

    # فیلتر فیدهای غیرمجاز
    filtered_feeds = []
    for f in feeds:
        ff = f.lower()

        # حذف فیدهای AI
        if f in system_ai_feeds:
            continue

        # حذف فیدهای ادمینی / سیستمی
        if any(p in ff for p in system_patterns):
            continue

        filtered_feeds.append(f)

    feeds = filtered_feeds

    # ✅ اگر هیچ موردی ثبت نشده بود
    if not feeds and not keywords:
        msg = t("list.empty", lang)
        sent = await update.message.reply_text(msg)
        await _maybe_auto_delete(context, chat_id, sent.message_id)
        return

    # ✅ ساخت متن نهایی با رعایت ترجمه
    msg_parts = []
    if feeds:
        msg_parts.append(
            t("list.feeds", lang)
            + ":\n"
            + "\n".join(f"{i+1}. {f}" for i, f in enumerate(feeds))
        )
    if keywords:
        msg_parts.append(
            t("list.keywords", lang)
            + ":\n"
            + "\n".join(f"{i+1}. {k['keyword']}" for i, k in enumerate(keywords))
        )

    msg = "\n\n".join(msg_parts)

    # ✅ دکمه‌ها هم مثل قبل
    keyboard = [
        [
            InlineKeyboardButton(t("btn.add", lang), callback_data="list:add"),
            InlineKeyboardButton(t("btn.remove", lang), callback_data="list:remove"),
        ],
        [InlineKeyboardButton(t("btn.clear", lang), callback_data="list:clear")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    sent = await update.message.reply_text(
        msg, reply_markup=reply_markup, disable_web_page_preview=True
    )
    await _maybe_auto_delete(context, chat_id, sent.message_id)


# -------------------------
# handle list buttons (اصلاح‌شده)
# -------------------------
async def cb_list_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # جلوگیری از "Loading..."

    data = query.data
    chat_id = update.effective_chat.id
    store = context.bot_data["store"]
    lang = get_chat_lang(store, chat_id)

    # ✅ لیست تمام چت‌هایی که مالکیت‌شون با این کاربره (برای پاکسازی گروه/کانال هم)
    owned_chat_ids = [chat_id]
    with store._locked_cursor() as cur:
        cur.execute("SELECT chat_id FROM chats WHERE owner_id = ?", (chat_id,))
        owned_chat_ids.extend([r["chat_id"] for r in cur.fetchall()])

    if data == "list:clear":
        for cid in owned_chat_ids:
            store.clear_feeds(cid)
            store.clear_keywords(cid) if hasattr(store, "clear_keywords") else None
        await query.edit_message_text(t("list.cleared", lang))

    elif data == "list:add":
        # ConversationHandler مربوط به /add
        return await cmd_add(update, context)

    elif data == "list:remove":
        # ConversationHandler مربوط به /remove
        return await cmd_remove(update, context)
