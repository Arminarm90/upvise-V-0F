# app/config.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field
from dotenv import load_dotenv

# بارگذاری متغیرها از فایل .env (اگر موجود باشد)
load_dotenv()


def _get_bool(name: str, default: bool) -> bool:
    v = os.getenv(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def _get_list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return list(default)
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass
class Settings:
    # --- Bot / Core ---
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    gemini_keys: list[str] = field(default_factory=lambda: _get_list("GEMINI_API_KEYS", []))
    serper_key: str = os.getenv("SERPER_API_KEY", "").strip()

    search_lang: str = (os.getenv("SEARCH_DEFAULT_LANG", "fa") or "fa").lower()
    prompt_lang: str = (os.getenv("PROMPT_LANG", "fa") or "fa").lower()
    poll_sec: int = _get_int("POLL_SEC", 180)

    state_file: str = os.getenv("STATE_FILE", "subs.json")

    # مسیر پوشه ترجمه‌ها → به مسیر مطلق resolve می‌کنیم
    locales_dir: str = str(pathlib.Path(os.getenv("LOCALES_DIR", "app/i18n")).resolve())

    # --- Discovery (RSS) ---
    discovery_timeout: int = _get_int("DISCOVERY_TIMEOUT", 8)
    discovery_max_redirects: int = _get_int("DISCOVERY_MAX_REDIRECTS", 3)
    discovery_max_bytes: int = _get_int("DISCOVERY_MAX_BYTES", 131072)
    discovery_ua: str = os.getenv(
        "DISCOVERY_UA",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    )
    discovery_guess_paths: list[str] = field(default_factory=lambda: _get_list(
        "DISCOVERY_GUESS_PATHS",
        [
            "/feed",
            "/rss",
            "/rss.xml",
            "/atom.xml",
            "/feed.xml",
            "/index.xml",
            "/blog/feed",
            "/news/feed",
            "/posts/index.xml",
        ],
    ))

    # --- Fetcher (HTML Extraction) ---
    fetcher_timeout: int = _get_int("FETCHER_TIMEOUT", 12)
    fetcher_max_html_bytes: int = _get_int("FETCHER_MAX_HTML_BYTES", 500_000)
    fetcher_ua: str = os.getenv(
        "FETCHER_UA",
        "Mozilla/5.0 (TelegramBot; +https://core.telegram.org/bots)",
    )
    botwall_pattern: str = os.getenv(
        "BOTWALL_PATTERN",
        r"(enable javascript|just a moment|cloudflare|access denied|verify you are a human)",
    )

    # --- RSS ---
    rss_timeout: int = _get_int("RSS_TIMEOUT", 12)
    rss_max_redirects: int = _get_int("RSS_MAX_REDIRECTS", 5)
    rss_ua: str = os.getenv(
        "RSS_UA",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125 Safari/537.36",
    )
    rss_max_items_per_feed: int = _get_int("RSS_MAX_ITEMS_PER_FEED", 10)

    # --- UA جنریک ---
    ua: str = os.getenv("UA", "").strip()

    # --- Page-Watch ---
    pagewatch_listing_limit: int = _get_int("PAGEWATCH_LISTING_LIMIT", 30)
    pagewatch_links_per_cycle: int = _get_int(
        "PAGEWATCH_LINKS_PER_CYCLE",
        _get_int("MAX_PAGEWATCH_LINKS_PER_CYCLE", 3),
    )
    max_pagewatch_links_per_cycle: int = _get_int("MAX_PAGEWATCH_LINKS_PER_CYCLE", 3)

    # --- Summarizer / Summary Chain ---
    summary_model_name: str = os.getenv("SUMMARY_MODEL_NAME", "gemini-2.0-flash")
    summary_max_input_chars: int = _get_int("SUMMARY_MAX_INPUT_CHARS", 6000)
    summary_cb_errors: int = _get_int("SUMMARY_CB_ERRORS", 5)
    summary_cb_cooldown_sec: int = _get_int("SUMMARY_CB_COOLDOWN_SEC", 300)
    summary_lite_min_len: int = _get_int("SUMMARY_LITE_MIN_LEN", 120)
    summary_max_bullets: int = _get_int("SUMMARY_MAX_BULLETS", 4)
    summary_strict: bool = _get_bool("SUMMARY_STRICT", True)

    # --- Ephemeral Messages ---
    ephemeral_mode: bool = _get_bool("EPHEMERAL_MODE", True)
    ephemeral_delete_sec: int = _get_int("EPHEMERAL_DELETE_SEC", 5)

    # --- Search defaults ---
    ddg_region_default: str = os.getenv("DDG_REGION_DEFAULT", "us-en")

    # --- URL canonicalization ---
    allow_strip_www: bool = _get_bool("ALLOW_STRIP_WWW", False)
    canonical_remove_trailing_slash: bool = _get_bool("CANONICAL_REMOVE_TRAILING_SLASH", True)

    def __post_init__(self) -> None:
        if not self.ua:
            self.ua = self.rss_ua or self.fetcher_ua
        if not self.pagewatch_links_per_cycle and self.max_pagewatch_links_per_cycle:
            self.pagewatch_links_per_cycle = self.max_pagewatch_links_per_cycle


settings = Settings()
