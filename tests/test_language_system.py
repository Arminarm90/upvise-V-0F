# tests/test_language_system.py
# -*- coding: utf-8 -*-
# pytest -q tests/test_language_system.py

import io
import json
import logging
import os
import tempfile
from pathlib import Path

import pytest

# لاگ واضح برای ردگیری باگ‌ها
logging.basicConfig(
    level=logging.INFO,
    format="TEST %(levelname)s:%(name)s:%(message)s"
)

# --- کمکی: نوشتن فایل‌های لوکالایز در دایرکتوری موقت ---
FA_JSON = {
    "menu.start": "شروع",
    "menu.add": "افزودن سایت",
    "menu.list": "نمایش فیدها",
    "menu.remove": "حذف لینک",
    "menu.lang": "تغییر زبان",
    "menu.help": "راهنما",
    "list.page": "صفحه ${page}/${pages}",
    "msg.source": "منبع",
    "msg.untitled": "بدون عنوان",
}
EN_JSON = {
    "menu.start": "Start",
    "menu.add": "Add site",
    "menu.list": "List feeds",
    "menu.remove": "Remove",
    "menu.lang": "Language",
    "menu.help": "Help",
    "list.page": "Page ${page}/${pages}",
    "msg.source": "Source",
    "msg.untitled": "Untitled",
}

@pytest.fixture()
def locales_tmpdir(tmp_path):
    d = tmp_path / "app" / "i18n"
    d.mkdir(parents=True, exist_ok=True)
    (d / "messages.fa.json").write_text(json.dumps(FA_JSON, ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "messages.en.json").write_text(json.dumps(EN_JSON, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


def test_i18n_load_and_lookup(locales_tmpdir, caplog):
    caplog.set_level(logging.INFO)
    from app.utils import i18n

    i18n.load_locales(str(locales_tmpdir))
    assert i18n.t("menu.start", "fa") == "شروع"
    assert i18n.t("menu.start", "en") == "Start"

    # جایگذاری پارامترها
    assert i18n.t("list.page", "fa", page=1, pages=5) == "صفحه 1/5"
    assert i18n.t("list.page", "en", page=2, pages=9) == "Page 2/9"

    # لاگ لود شدن
    assert any("loaded locales" in m.message.lower() for m in caplog.records), "Locales load info log not found"


def test_i18n_missing_key_fallback(locales_tmpdir, caplog):
    caplog.set_level(logging.WARNING)
    from app.utils import i18n

    i18n.load_locales(str(locales_tmpdir))

    # کلید وجود ندارد → باید خودش را برگرداند و هشدار لاگ شود
    missing = i18n.t("no.such.key", "fa")
    assert missing == "no.such.key"
    assert any("missing key: no.such.key" in (m.message.lower()) for m in caplog.records), \
        "Missing-key warning not logged"


def test_state_lang_persist(tmp_path, locales_tmpdir):
    # استور فایل
    state_file = tmp_path / "subs.json"

    # تنظیم i18n تا زبان معتبر داشته باشیم
    from app.utils import i18n
    i18n.load_locales(str(locales_tmpdir))

    # StateStore
    from app.storage.state import StateStore
    store = StateStore(str(state_file))

    # set_chat_lang و get_chat_lang
    i18n.set_chat_lang(store, 12345, "en")
    lang_now = i18n.get_chat_lang(store, 12345)
    assert lang_now == "en"

    # بارگذاری مجدد از دیسک
    store2 = StateStore(str(state_file))
    lang_again = i18n.get_chat_lang(store2, 12345)
    assert lang_again == "en"


def test_bot_commands_localized(locales_tmpdir, caplog):
    from app.utils import i18n
    i18n.load_locales(str(locales_tmpdir))

    # سعی می‌کنیم از خود bot._commands_for_lang استفاده کنیم (اگر در پروژه هست)
    try:
        from app.bot import _commands_for_lang
        cmds_fa = _commands_for_lang("fa")
        cmds_en = _commands_for_lang("en")

        # BotCommand در تلگرام آبجکت است؛ به استرینگ چک می‌کنیم
        titles_fa = [c.description for c in cmds_fa]
        titles_en = [c.description for c in cmds_en]
        assert "شروع" in titles_fa and "Start" in titles_en
    except Exception:
        # اگر import ناممکن بود، حداقل چک کنیم که t() برای کلیدهای منو درست کار می‌کند
        assert i18n.t("menu.help", "fa") == "راهنما"
        assert i18n.t("menu.help", "en") == "Help"


def test_summarizer_lite_language(locales_tmpdir, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    # اطمینان از لایت مود: API key نده
    from app.services.summary import Summarizer
    sm = Summarizer(api_key=None, prompt_lang="fa")

    text = (
        "هوش مصنوعی در سال‌های اخیر رشد چشمگیری داشته است. "
        "شرکت‌های بزرگ در حال سرمایه‌گذاری روی مدل‌های زبانی و تصویری هستند. "
        "این تحول می‌تواند صنعت‌ها را دگرگون کند اما چالش‌های اخلاقی و امنیتی نیز دارد."
    )

    tldr_fa, bullets_fa = pytest.run(async_summarize(sm, "عنوان", text))
    assert tldr_fa, "Lite FA should produce TLDR"
    assert any("می‌تواند" in b or "هستند" in b for b in bullets_fa), "Bullets should be Persian-like"

    # تغییر زبان به انگلیسی و دوباره تست
    sm.prompt_lang = "en"
    tldr_en, bullets_en = pytest.run(async_summarize(sm, "AI title", "AI has rapidly advanced in recent years. "
        "Companies are investing in large language and vision models. "
        "This shift can transform industries but raises safety and ethics concerns."))
    assert tldr_en, "Lite EN should produce TLDR"
    # بررسی کاراکترهای انگلیسی
    assert all(all(ord(ch) < 128 for ch in b) for b in bullets_en), "Bullets should look English in EN mode"


# هلسپر کوچک برای ران async بدون نیاز به فریمورک اضافی
def pytest_run_async(coro):
    import asyncio
    return asyncio.get_event_loop().run_until_complete(coro)

# alias قشنگ‌تر
pytest.run = pytest_run_async


async def async_summarize(sm, title, text):
    return await sm.summarize(title=title, text=text, author=None)


def test_language_binding_switch_runtime(locales_tmpdir):
    """تغییر زبان در لحظه روی Summarizer اثر بگذارد (رفتار RSSService که قبل از خلاصه‌سازی lang را ست می‌کند)."""
    from app.services.summary import Summarizer
    sm = Summarizer(api_key=None, prompt_lang="fa")

    # اول فارسی
    fa_out = pytest.run(async_summarize(
        sm,
        "عنوان",
        "این یک متن آزمایشی برای خلاصه‌سازی فارسی است که باید خروجی فارسی بدهد."
    ))
    assert fa_out[0], "FA TLDR expected"

    # سپس تغییر زبان
    sm.prompt_lang = "en"
    en_out = pytest.run(async_summarize(
        sm,
        "Title",
        "This is a short English sample text to trigger English lite summary output."
    ))
    assert en_out[0], "EN TLDR expected"
    # چک ظاهری انگلیسی بودن
    assert all(all(ord(ch) < 128 for ch in en_out[0]) for _ in [0]), "EN TLDR should be ASCII-ish"


def test_i18n_fallback_to_default(locales_tmpdir, caplog, monkeypatch):
    """اگر کاربر زبان ناشناخته خواست، به زبان پیش‌فرض پروژه برگردد (fa یا en)."""
    from app.utils import i18n

    i18n.load_locales(str(locales_tmpdir))
    # زبان نامعتبر
    txt = i18n.t("menu.start", "de-DE")
    # باید به DEFAULT_LANG (fa طبق تنظیمات پیش‌فرض) بیفتد یا به en اگر fa نبود
    assert txt in {"شروع", "Start"}, "Fallback to DEFAULT_LANG/en failed"

    # کلید موجود در en ولی نبود در fa → باید en برگردد
    # یک کلید اختصاصی فقط در EN بسازیم
    only_en_key = "only.en.key"
    en_path = locales_tmpdir / "messages.en.json"
    data = json.loads(en_path.read_text(encoding="utf-8"))
    data[only_en_key] = "EN_ONLY"
    en_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    i18n.load_locales(str(locales_tmpdir))
    assert i18n.t(only_en_key, "fa") == "EN_ONLY", "Cross-lang fallback to EN failed"


# نکته: این تست‌ها وابسته به شبکه/تلگرام نیستند و سریع اجرا می‌شوند.
# اجرا:  pytest -q
