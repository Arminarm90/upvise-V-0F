# app/bot.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
from telegram import BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

from .config import settings
from .storage.state import StateStore, SQLiteStateStore
from .services.summary import Summarizer, get_gemini_key
from .services.search import SearchService
from .services.rss import RSSService

from .handlers import basic, feeds  # /discover حذف شده است
from .handlers.feeds import get_add_conversation_handler, get_remove_conversation_handler, cb_list_actions, list_feeds  # ConversationHandler برای /add
from .handlers.lang import cmd_lang, cb_lang
from .handlers.list import cmd_list, cb_list_nav
from .utils.i18n import load_locales, t

# sub
from telegram.ext import CommandHandler
from app.handlers.payment import cmd_buy
from app.handlers.payment import get_payment_handlers

LOG = logging.getLogger(__name__)


def _commands_for_lang(code: str) -> list[BotCommand]:
    """لیست دستورات براساس i18n برای زبان مشخص."""
    return [
        BotCommand("start",  t("menu.start",  code)),
        BotCommand("add",    t("menu.add",    code)),
        BotCommand("list",   t("menu.list",   code)),
        BotCommand("remove", t("menu.remove", code)),
        BotCommand("lang",   t("menu.lang",   code)),
        BotCommand("help",   t("menu.help",   code)),
    ]


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Error handler سراسری:
      - خطاهای رایج و بی‌خطر را بی‌صدا نادیده می‌گیرد (برای کاهش نویز لاگ)
      - سایر خطاها را همراه با سرنخ‌های مفید لاگ می‌کند
    """
    err = context.error
    msg = str(err or "").lower()

    # موارد پرتکرار و بی‌خطر
    try:
        from telegram.error import BadRequest, Forbidden
        if isinstance(err, BadRequest):
            if "message is not modified" in msg:
                LOG.debug("Ignored benign error: message is not modified")
                return
            if "chat not found" in msg:
                LOG.warning("Chat not found (ignored). Update=%s", getattr(update, "to_dict", lambda: update)())
                return
        if isinstance(err, Forbidden) and "bot was blocked by the user" in msg:
            LOG.warning("Bot blocked by user (ignored).")
            return
    except Exception:
        # اگر ایمپورت یا تشخیص خطا شکست خورد، ادامه می‌دهیم تا لاگ استثناء ثبت شود
        pass

    # لاگ کامل برای سایر خطاها
    try:
        upd_repr = getattr(update, "to_dict", lambda: update)()
    except Exception:
        upd_repr = repr(update)
    LOG.exception("Unhandled error | update=%s", upd_repr, exc_info=err)


def build_app() -> Application:
    """
    ساخت و سیم‌کشی کامل اپ تلگرام
    - بارگذاری i18n (از مسیر settings.locales_dir)
    - ایجاد سرویس‌ها و قرار دادن در bot_data
    - ثبت هندلرها (از جمله ConversationHandler مربوط به /add)
    - ثبت جاب پولینگ
    - تنظیم BotCommands به‌صورت per-language (fa/en) + fallback عمومی
    """
    # i18n (از مسیر مطلقی که در config.py resolve شده)
    try:
        load_locales(settings.locales_dir)
    except Exception as ex:
        LOG.error("failed to load locales from %s: %s", settings.locales_dir, ex)

    # ساخت اپ
    app: Application = Application.builder().token(settings.telegram_token).build()

    # ---- سرویس‌ها
    store = SQLiteStateStore(getattr(settings, "state_db", "state.db"))
    summarizer = Summarizer(
        api_key=get_gemini_key(),
        prompt_lang=getattr(settings, "prompt_lang", "fa"),
    )
    search = SearchService(
        serper_key=getattr(settings, "serper_key", None),
        default_lang=getattr(settings, "search_lang", "fa"),
    )
    rss = RSSService(
        store=store,
        summarizer=summarizer,
        search_service=search,
        poll_sec=getattr(settings, "poll_sec", 120),
    )

    # در دسترس‌گذاری سرویس‌ها و تنظیمات برای هندلرها
    app.bot_data["store"] = store
    app.bot_data["summarizer"] = summarizer
    app.bot_data["search"] = search
    app.bot_data["rss"] = rss
    # حالت پیام‌های موقتی
    app.bot_data["ephemeral_mode"] = getattr(settings, "ephemeral_mode", True)
    app.bot_data["ephemeral_delete_sec"] = getattr(settings, "ephemeral_delete_sec", 5)

    # ---- ثبت هندلرها

    # /start و /help
    app.add_handler(CommandHandler("start", basic.cmd_start))
    app.add_handler(CommandHandler("help", basic.cmd_help))

    # /add — Conversation دو مرحله‌ای
    app.add_handler(get_add_conversation_handler())

    # /remove — تک‌مرحله‌ای
    app.add_handler(feeds.get_remove_conversation_handler())


    # /list + ناوبری صفحه‌بندی
    app.add_handler(CommandHandler("list", list_feeds))
    app.add_handler(CallbackQueryHandler(cb_list_actions, pattern=r"^list:(add|remove|clear)$"))

    # /lang + تغییر زبان با دکمه‌ها
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CallbackQueryHandler(cb_lang, pattern=r"^lang:"))
    
    app.add_handler(CommandHandler("buy", get_payment_handlers()[0][1]))
    app.add_handler(get_payment_handlers()[1])

    # ---- Error handler سراسری
    app.add_error_handler(on_error)

    # ---- Job: پولینگ دوره‌ای
    async def poll_job(ctx):
        # یک دور پولینگ: بررسی منابع و ارسال آیتم‌های تازه
        await app.bot_data["rss"].poll_once(app)

    app.job_queue.run_repeating(
        poll_job,
        interval=getattr(settings, "poll_sec", 120),
        first=5,
    )

    # ---- Bot Commands (نمایش در منوی تلگرام) — per-language + fallback
    async def post_init(a: Application):
        try:
            # برای فارسی
            await a.bot.set_my_commands(_commands_for_lang("fa"), language_code="fa")
            # برای انگلیسی
            await a.bot.set_my_commands(_commands_for_lang("en"), language_code="en")
            # fallback عمومی (اگر کلاینت زبان دیگری داشت)
            await a.bot.set_my_commands(_commands_for_lang("en"))
        except Exception as ex:
            LOG.warning("set_my_commands failed: %s", ex)

    app.post_init = post_init
    return app
