# app/handlers/list.py
# -*- coding: utf-8 -*-
from typing import List, Iterable, Any
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.error import BadRequest  # <-- NEW: برای مدیریت دقیق خطای ادیت پیام
from app.utils.i18n import t, get_chat_lang

PAGE_SIZE = 10


def _uniq_strings(items: Iterable[Any]) -> List[str]:
    """یونیک‌سازی با حفظ ترتیب و فقط رشته‌ها."""
    seen = set()
    out: List[str] = []
    for it in items or []:
        if isinstance(it, str):
            s = it.strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
    return out


def _read_feeds(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> List[str]:
    """
    تلاش چندمرحله‌ای برای خواندن فیدها، با سازگاری با انواع StateStore:
      1) اگر API سطح‌بالا وجود دارد: list_feeds(chat_id)
      2) ساختار get_chat(...)->dict['feeds']
      3) دسترسی مستقیم به state داخلی (_state/state)
    """
    store = context.bot_data.get("store")
    if not store:
        return []

    candidates: List[str] = []

    # 1) API رسمی list_feeds
    for cid in (chat_id, str(chat_id)):
        for attr in ("list_feeds", "get_feeds"):
            if hasattr(store, attr):
                try:
                    feeds = getattr(store, attr)(cid)
                    if feeds:
                        candidates.extend(list(feeds))
                        return _uniq_strings(candidates)
                except Exception:
                    pass

    # 2) get_chat(...)->dict['feeds']
    for cid in (chat_id, str(chat_id)):
        if hasattr(store, "get_chat"):
            try:
                st = store.get_chat(cid) or {}
                feeds = st.get("feeds", []) if isinstance(st, dict) else []
                if feeds:
                    candidates.extend(list(feeds))
                    return _uniq_strings(candidates)
            except Exception:
                pass

    # 3) دسترسی مستقیم به state داخلی
    for state_attr in ("_state", "state", "data"):
        try:
            state = getattr(store, state_attr, {}) or {}
            chat_state = state.get(str(chat_id)) or state.get(chat_id) or {}
            feeds = chat_state.get("feeds", []) if isinstance(chat_state, dict) else []
            candidates.extend(list(feeds))
            break
        except Exception:
            pass

    return _uniq_strings(candidates)


def _page_count(n: int) -> int:
    return max(1, (n + PAGE_SIZE - 1) // PAGE_SIZE)


def _render_page(feeds: List[str], page: int, lang: str) -> tuple[str, InlineKeyboardMarkup]:
    total = len(feeds)
    pages = _page_count(total)
    page = max(1, min(page, pages))
    start = (page - 1) * PAGE_SIZE
    end = min(start + PAGE_SIZE, total)

    if total == 0:
        body = t("list.empty", lang)
    else:
        body = "\n".join(f"{i + 1}. {feeds[i]}" for i in range(start, end))

    text = f"{t('list.title', lang)}\n{body}\n\n{t('list.page', lang, page=page, pages=pages)}"

    buttons: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton(t("btn.prev", lang), callback_data=f"list:{page - 1}"))
    if page < pages:
        nav.append(InlineKeyboardButton(t("btn.next", lang), callback_data=f"list:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(t("btn.close", lang), callback_data="list:close")])

    return text, InlineKeyboardMarkup(buttons)


async def _maybe_auto_delete(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    """اگر حالت Ephemeral فعال باشد، پیام را پس از زمان تنظیم‌شده حذف می‌کند."""
    try:
        if ctx.bot_data.get("ephemeral_mode", True):
            auto_delete = ctx.bot_data.get("auto_delete")
            if callable(auto_delete):
                ctx.application.create_task(auto_delete(ctx, chat_id, message_id))
    except Exception:
        pass


# /list
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    store = context.bot_data.get("store")
    lang = get_chat_lang(store, chat_id) if store else "fa"
    store.mark_action(chat_id)
    feeds = _read_feeds(context, chat_id)
    text, kb = _render_page(feeds, page=1, lang=lang)

    sent = await update.effective_message.reply_text(
        text,
        reply_markup=kb,
        disable_web_page_preview=True,
    )
    await _maybe_auto_delete(context, chat_id, sent.message_id)


# پیمایش
async def cb_list_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    chat_id = q.message.chat.id

    store = context.bot_data.get("store")
    lang = get_chat_lang(store, chat_id) if store else "fa"

    if data == "list:close":
        try:
            await q.message.delete()
        except Exception:
            await q.edit_message_reply_markup(reply_markup=None)
        return

    try:
        _, page_str = data.split(":", 1)
        page = int(page_str)
    except Exception:
        page = 1

    feeds = _read_feeds(context, chat_id)
    text, kb = _render_page(feeds, page=page, lang=lang)
    try:
        edited = await q.edit_message_text(
            text,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
        # اگر تلگرام Message برگرداند، شناسه‌اش را بگیریم تا حذف خودکار هم اعمال شود
        msg_id = getattr(edited, "message_id", None)
        if msg_id:
            await _maybe_auto_delete(context, chat_id, msg_id)
    except BadRequest as e:  # <-- NEW: مدیریت خاص خطاهای ویرایش پیام
        msg = str(e).lower()
        if "message is not modified" in msg:
            # هیچ تغییری لازم نبود؛ بی‌صدا خروج
            return
        # سایر خطاهای BadRequest: به پیام جدید fallback کن (رفتار قبلی)
        sent = await q.message.reply_text(
            text,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
        await _maybe_auto_delete(context, chat_id, sent.message_id)
    except Exception:
        # هر خطای دیگر: همان fallback قبلی
        sent = await q.message.reply_text(
            text,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
        await _maybe_auto_delete(context, chat_id, sent.message_id)
