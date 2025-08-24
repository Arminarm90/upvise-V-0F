# app/bot.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
from telegram import BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler

from .config import settings
from .storage.state import StateStore
from .services.summary import Summarizer
from .services.search import SearchService
from .services.rss import RSSService

from .handlers import basic, feeds  # /discover حذف شده است
from .handlers.feeds import get_add_conversation_handler  # ConversationHandler برای /add
from .handlers.lang import cmd_lang, cb_lang
from .handlers.list import cmd_list, cb_list_nav
from .utils.i18n import load_locales, t

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
    store = StateStore(getattr(settings, "state_file", "subs.json"))
    summarizer = Summarizer(
        api_key=getattr(settings, "gemini_key", None),
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
    app.add_handler(CommandHandler("remove", feeds.cmd_remove))

    # /list + ناوبری صفحه‌بندی
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CallbackQueryHandler(cb_list_nav, pattern=r"^list:"))

    # /lang + تغییر زبان با دکمه‌ها
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CallbackQueryHandler(cb_lang, pattern=r"^lang:"))

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
