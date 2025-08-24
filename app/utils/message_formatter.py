# app/utils/message_formatter.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from typing import Optional, List
import re
from bs4 import BeautifulSoup

# ---- i18n & settings (robust imports) ----------------------------------------
try:
    # مسیر استاندارد پروژه
    from app.utils.i18n import t  # type: ignore
except Exception:
    try:
        # اگر ساختار ماژول متفاوت بود (سازگاری عقب‌رو)
        from ..i18n import t  # type: ignore
    except Exception:
        # fallback حداقلی (نباید رخ دهد)
        def t(key: str, lang: Optional[str] = None, **kwargs) -> str:  # type: ignore
            return {
                "msg.source": "منبع",
                "msg.untitled": "بدون عنوان",
            }.get(key, key)

try:
    from app.config import settings  # type: ignore
except Exception:  # fallback امن برای تست‌های ایزوله
    class _S:  # type: ignore
        summary_strict = True
        summary_max_bullets = 4
        fetcher_timeout = 12
    settings = _S()  # type: ignore

# ---- fetcher برای متن کامل مقاله (RSS → متن صفحه) --------------------------
try:
    from ..services.fetcher import fetch_article_text  # type: ignore
except Exception:
    async def fetch_article_text(url: str, timeout: int = 12) -> str:  # type: ignore
        return ""

# ---- escape های HTML تلگرام -------------------------------------------------
from .text import html_escape as esc, html_attr_escape as esc_attr


# ==== قالب خروجی ====
TEMPLATE_TITLE  = "<b>{title}</b>"
TEMPLATE_META   = "<i>{source}</i> | <i>{date}</i>"
TEMPLATE_LEAD   = "🔰 {lead}"
TEMPLATE_BULLET = "✔️ {b}"


# ==== کمکی‌ها ====
def _fmt_date(entry) -> str:
    """تبدیل تاریخ RSS به YYYY-MM-DD (published → updated)"""
    try:
        if getattr(entry, "published_parsed", None):
            return datetime(*entry.published_parsed[:6]).strftime("%Y-%m-%d")
        if getattr(entry, "updated_parsed", None):
            return datetime(*entry.updated_parsed[:6]).strftime("%Y-%m-%d")
    except Exception:
        pass
    return ""


def _clean_html(s: str) -> str:
    if not s:
        return ""
    soup = BeautifulSoup(s, "html.parser")
    for tnode in soup(["script", "style", "noscript"]):
        tnode.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def _clean_bullet(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"^[•\-–—\u2022\*\+\s]+", "", s)
    s = re.sub(r"\s+", " ", s)
    s = s.rstrip("،,:;.")
    return s


def _author_of(entry) -> str:
    for k in ("author", "dc_creator", "creator"):
        try:
            v = getattr(entry, k, None)
            if v:
                return str(v)
        except Exception:
            pass
    return ""


async def _raw_from_entry(entry, link: str) -> str:
    """
    متن پایه برای خلاصه‌سازی:
      - اگر settings.summary_strict=True → اول متن صفحه، fallback به RSS تمیز
      - اگر False → اول RSS تمیز، درصورت کم‌بود متن صفحه
    """
    strict = bool(getattr(settings, "summary_strict", True))

    # RSS summary/content
    rss_raw = ""
    if getattr(entry, "summary", None):
        rss_raw = entry.summary
    elif getattr(entry, "content", None):
        try:
            rss_raw = " ".join([c.value for c in entry.content if getattr(c, "value", None)])
        except Exception:
            rss_raw = ""
    rss_clean = _clean_html(rss_raw)

    timeout = int(getattr(settings, "fetcher_timeout", 12))
    if strict:
        page_txt = await fetch_article_text(link, timeout=timeout) if link else ""
        if page_txt:
            return page_txt.strip()
        return rss_clean
    else:
        if not rss_clean or len(rss_clean) < 280:
            page_txt = await fetch_article_text(link, timeout=timeout) if link else ""
            return (page_txt or rss_clean).strip()
        return rss_clean


def _cap_bullets(bullets: List[str], cap: int) -> List[str]:
    """تمیزسازی، حذف تکراری‌ها و اِعمال سقف تعداد بولت‌ها."""
    out: List[str] = []
    seen = set()
    for b in bullets or []:
        b2 = _clean_bullet(b)
        if not b2 or len(b2) < 8:
            continue
        k = b2.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(b2)
        if len(out) >= cap:
            break
    return out


# ==== قالب‌ساز واحد: RSS Entry ====
async def format_entry(
    feed_title: str,
    entry,
    summarizer,
    feed_url: str,
    lang: Optional[str] = None,
) -> str | None:
    """
    خروجی ۱۰۰٪ ثابت و یکدست (برای RSS):
      [عنوان]
      [منبع] | تاریخ

      🔰 لید
      ✔️ بولت‌ها...
      منبع(لینک)
    """
    # زبان خلاصه: پارامتر → زبان پرامپت summarizer → پیش‌فرض fa
    lang = (lang or getattr(summarizer, "prompt_lang", "fa") or "fa").lower()

    # 1) متادیتا
    title = _clean_html(getattr(entry, "title", "") or "") or t("msg.untitled", lang)
    link  = getattr(entry, "link", "") or ""
    source_label = (feed_title or t("msg.source", lang)).strip()
    date = _fmt_date(entry)
    meta_line = TEMPLATE_META.format(source=esc(source_label), date=esc(date))

    # 2) متن پایه
    raw = await _raw_from_entry(entry, link)
    if not raw:
        return None

    # 3) خلاصه‌سازی (fa/en بر اساس lang)
    author = _author_of(entry) or None
    tldr, bullets = await summarizer.summarize(title=title, text=raw, author=author)
    if not (tldr or bullets):
        return None

    # 4) بولت‌ها با سقف
    cap = int(getattr(settings, "summary_max_bullets", 4))
    final_bullets = _cap_bullets(bullets or [], cap)

    # 5) ساخت پیام نهایی (HTML تلگرام)
    parts = [
        TEMPLATE_TITLE.format(title=esc(title)),
        meta_line,
        "",
        TEMPLATE_LEAD.format(lead=esc(tldr or "")),
        "",
    ]
    if final_bullets:
        parts += [TEMPLATE_BULLET.format(b=esc(b)) for b in final_bullets]

    if link:
        parts.append(f'\n<a href="{esc_attr(link)}">{esc(t("msg.source", lang))}</a>')

    return "\n".join(parts).strip()


# ==== قالب‌ساز واحد: Page‑Watch Article ====
async def format_article(
    feed_title: str,
    title: str,
    link: str,
    text: str,
    summarizer,
    lang: Optional[str] = None,
) -> str | None:
    """
    خروجی یکسان با RSS برای حالت Page‑Watch (بدون تاریخ):
      [عنوان]
      [منبع] | (تاریخ خالی)

      🔰 لید
      ✔️ بولت‌ها...
      منبع(لینک)
    """
    lang = (lang or getattr(summarizer, "prompt_lang", "fa") or "fa").lower()

    safe_title = _clean_html(title or "") or t("msg.untitled", lang)
    source_label = (feed_title or t("msg.source", lang)).strip()
    date = ""  # در Page‑Watch معمولاً تاریخ RSS نداریم
    meta_line = TEMPLATE_META.format(source=esc(source_label), date=esc(date))

    raw = (text or "").strip()
    if not raw:
        return None

    tldr, bullets = await summarizer.summarize(title=safe_title, text=raw, author=None)
    if not (tldr or bullets):
        return None

    cap = int(getattr(settings, "summary_max_bullets", 4))
    final_bullets = _cap_bullets(bullets or [], cap)

    parts = [
        TEMPLATE_TITLE.format(title=esc(safe_title)),
        meta_line,
        "",
        TEMPLATE_LEAD.format(lead=esc(tldr or "")),
        "",
    ]
    if final_bullets:
        parts += [TEMPLATE_BULLET.format(b=esc(b)) for b in final_bullets]

    if link:
        parts.append(f'\n<a href="{esc_attr(link)}">{esc(t("msg.source", lang))}</a>')

    return "\n".join(parts).strip()
