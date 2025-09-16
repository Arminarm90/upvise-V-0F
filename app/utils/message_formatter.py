# app/utils/message_formatter.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
import re
from bs4 import BeautifulSoup
import html
import html as _html_mod
from ..services.summary import _translate as translate_fn  # تابع ترجمه از summary.py
# from ..services.summary import _lite_summary_short
try:
    from deep_translator import GoogleTranslator as _GT
except Exception:
    _GT = None
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
                "msg.source": "منبع" if (lang or "fa").startswith("fa") else "Source",
                "msg.untitled": "بدون عنوان" if (lang or "fa").startswith("fa") else "Untitled",
                "msg.opportunities": "فرصت‌ها" if (lang or "fa").startswith("fa") else "Opportunities",
                "msg.risks": "ریسک‌ها" if (lang or "fa").startswith("fa") else "Risks",
                "msg.signal": "سیگنال" if (lang or "fa").startswith("fa") else "Signal",
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

import logging
LOG = logging.getLogger("message_formatter")

# ==== قالب خروجی ====
TEMPLATE_TITLE  = "<b>{title}</b>"
TEMPLATE_META   = "<i>{source}</i> | <i>{date}</i>"
TEMPLATE_LEAD   = "🔰 {lead}"
TEMPLATE_BULLET = "✔️ {b}"

# سکشن‌های 2x Premium (برچسب‌ها را از i18n می‌گیریم)
TEMPLATE_HEAD_OPP    = "🔺 {label}"
TEMPLATE_HEAD_RISK   = "🔻 {label}"
TEMPLATE_HEAD_SIGNAL = "📊 {label}"
TEMPLATE_SIGNAL_TEXT = "• {text}"  # سیگنال را به صورت یک خط ساده نمایش می‌دهیم


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


def _clean_html(raw: str) -> str:
    if not raw:
        return ""
    try:
        s = _html_mod.unescape(str(raw))
    except Exception:
        s = str(raw)

    try:
        soup = BeautifulSoup(s, "html.parser")
        for tnode in soup(["script", "style", "noscript", "iframe", "embed", "svg", "img"]):
            try:
                tnode.decompose()
            except Exception:
                pass
        text = soup.get_text(separator=" ", strip=True)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", s) 

    text = re.sub(r"\s+", " ", (text or "")).strip()

    text = re.sub(r"<\s*a\s+[^>]*>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"</\s*a\s*>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:5000]  # cap

def _strip_noise_from_feed_text(text: str) -> str:
    if not text:
        return ""
    s = text
    s = re.split(r"View Full Coverage on Google News|View full coverage|View Full Coverage", s, flags=re.IGNORECASE)[0]
    s = re.split(r"Read more|Read the full story|Full Coverage|View Full Story", s, flags=re.IGNORECASE)[0]
    s = re.sub(r"(?i)view full coverage.*", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.strip(" \n\r\t-–—:;,.")
    return s

async def extract_content_from_entry(entry, link: str = "") -> str:
    raw = ""
    try:
        if getattr(entry, "summary_detail", None) and getattr(entry.summary_detail, "value", None):
            raw = entry.summary_detail.value or ""
        if not raw and getattr(entry, "summary", None):
            raw = entry.summary or ""
        if not raw and getattr(entry, "description", None):
            raw = entry.description or ""
        if not raw and getattr(entry, "content", None):
            c = entry.content
            if isinstance(c, (list, tuple)) and c:
                vals = []
                for it in c:
                    try:
                        if hasattr(it, "value"):
                            vals.append(it.value or "")
                        elif isinstance(it, dict):
                            vals.append(it.get("value", ""))
                        else:
                            vals.append(str(it))
                    except Exception:
                        continue
                raw = " ".join([v for v in vals if v])
            elif isinstance(c, str):
                raw = c

        if not raw:
            try:
                raw = entry.get("content_encoded", "") or entry.get("content:encoded", "") or raw
            except Exception:
                raw = getattr(entry, "content_encoded", "") or getattr(entry, "content:encoded", "") or raw

        if not raw:
            try:
                links = getattr(entry, "links", None) or []
                for l in links:
                    href = l.get("href") if isinstance(l, dict) else getattr(l, "href", None)
                    title = l.get("title") if isinstance(l, dict) else getattr(l, "title", None)
                    rel = (l.get("rel") if isinstance(l, dict) else getattr(l, "rel", None)) or ""
                    if title and not raw:
                        raw = title
                        break
                media = getattr(entry, "media_content", None) or []
                if not raw and isinstance(media, (list, tuple)) and media:
                    if isinstance(media[0], dict):
                        raw = media[0].get("description") or media[0].get("title") or raw
            except Exception:
                pass
    except Exception:
        raw = ""

    cleaned = _clean_html(raw)
    cleaned = _strip_noise_from_feed_text(cleaned)

    if (not cleaned or len(cleaned) < 80) and link:
        try:
            page_txt = await fetch_article_text(link, timeout=int(getattr(settings, "fetcher_timeout", 12)))
            if page_txt:
                cleaned_page = _clean_html(page_txt)
                cleaned_page = _strip_noise_from_feed_text(cleaned_page)
                # prefer page text if it's meaningfully longer than feed snippet
                if cleaned_page and (len(cleaned_page) > len(cleaned) or len(cleaned) > 200):
                    cleaned = cleaned_page
        except Exception:
            pass

    if cleaned and len(cleaned) > 5000:
        cleaned = cleaned[:5000]

    LOG.debug("extract_content_from_entry length=%d for title=%r", len(cleaned or ""), getattr(entry, "title", "") or "")
    return cleaned


def _lite_summary_short(title: str, text: str) -> Tuple[str, List[str]]:
    src = (text or "").strip()
    if not src:
        return "", []

    src = re.sub(r"\s+", " ", src).strip()
    src = re.split(r"View Full Coverage on Google News|View full coverage|View Full Coverage|Read more", src, flags=re.IGNORECASE)[0]

    sentences = re.split(r"(?<=[.!؟\?])\s+", src)

    title_short = (title or "").strip()
    if len(title_short) > 60:
        title_short = title_short[:60]
    title_short_lower = title_short.lower()

    clean_sents = []
    for s in sentences:
        ss = s.strip()
        if not ss:
            continue
        # اگر جمله مستقیماً همان عنوان یا شامل بخشی از عنوان است، رد کن
        if title_short_lower and title_short_lower in ss.lower():
            continue
        if len(ss) < 25:
            continue
        clean_sents.append(ss)
        if len(clean_sents) >= 2:
            break

    if clean_sents:
        tldr = " ".join(clean_sents[:2]).strip()
        if len(tldr) > 220:
            tldr = tldr[:200].rsplit(" ", 1)[0] + "…"
        return tldr, []

    snippet = src.strip()
    if not snippet:
        return "", []
    if title_short_lower and title_short_lower in snippet.lower():
        return "", []
    if len(snippet) > 220:
        snippet = snippet[:200].rsplit(" ", 1)[0] + "…"
    return snippet, []

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


def _cap_section(items: List[str], cap: int) -> List[str]:
    """
    تمیزسازی عمومی برای سکشن‌های فرصتها/ریسکها: مشابه بولت‌ها.
    """
    return _cap_bullets(items or [], cap)


async def _summarize_flexible(
    summarizer,
    title: str,
    text: str,
    author: Optional[str],
) -> Dict[str, Any]:
    """
    مسیر منعطف:
      - اگر summarizer متدی به نام summarize_full داشته باشد: آن را صدا می‌زنیم.
        * پشتیبانی از هر دو خروجی: tuple (tldr, bullets, opportunities, risks, signal) یا dict {...}.
      - در غیر این صورت: summarize معمولی را صدا می‌زنیم و فقط tldr/bullets را پر می‌کنیم.
    خروجی همیشه دیکشنری با کلیدهای: tldr, bullets, opportunities, risks, signal
    """
    result = {
        "tldr": "",
        "bullets": [],
        "opportunities": [],
        "risks": [],
        "signal": "",
    }
    # تلاش برای API کامل (اختیاری)
    try:
        summarize_full = getattr(summarizer, "summarize_full", None)
        if callable(summarize_full):
            data = await summarize_full(title=title, text=text, author=author)
            # --- NEW: پشتیبانی از tuple (نسخهٔ جدید summary.py) ---
            if isinstance(data, (tuple, list)) and len(data) >= 5:
                tldr, bullets, opportunities, risks, signal = data[:5]
                result["tldr"] = (tldr or "").strip()
                result["bullets"] = [x for x in (bullets or []) if isinstance(x, str)]
                result["opportunities"] = [x for x in (opportunities or []) if isinstance(x, str)]
                result["risks"] = [x for x in (risks or []) if isinstance(x, str)]
                result["signal"] = (signal or "").strip()
                return result
            # --- حالت قدیمی: dict ---
            if isinstance(data, dict):
                result.update({k: data.get(k, result[k]) for k in result.keys()})
                # اطمینان از تایپ‌ها
                result["tldr"] = (result["tldr"] or "").strip()
                result["bullets"] = [x for x in (result["bullets"] or []) if isinstance(x, str)]
                result["opportunities"] = [x for x in (result["opportunities"] or []) if isinstance(x, str)]
                result["risks"] = [x for x in (result["risks"] or []) if isinstance(x, str)]
                result["signal"] = (result["signal"] or "").strip()
                return result
    except Exception:
        # اگر API کامل خطا داد، بی‌سروصدا به مسیر ساده برگردیم
        pass

    # مسیر سازگار قبلی (دوخروجی)
    tldr, bullets = await summarizer.summarize(title=title, text=text, author=author)
    result["tldr"] = (tldr or "").strip()
    result["bullets"] = [x for x in (bullets or []) if isinstance(x, str)]
    return result


def _maybe_section(parts: List[str], items: List[str], label: str, lang: str, cap: int) -> None:
    """
    در صورت وجود آیتم، سکشن را به parts اضافه می‌کند.
    """
    cleaned = _cap_section(items, cap)
    if not cleaned:
        return
    # عنوان سکشن
    parts.append(TEMPLATE_HEAD_OPP.format(label=esc(label)) if label == t("msg.opportunities", lang)
                 else TEMPLATE_HEAD_RISK.format(label=esc(label)) if label == t("msg.risks", lang)
                 else TEMPLATE_HEAD_SIGNAL.format(label=esc(label)))
    # آیتم‌ها (برای سیگنال، یک خطه)
    if label == t("msg.signal", lang):
        parts.append(TEMPLATE_SIGNAL_TEXT.format(text=esc(cleaned[0])))
    else:
        parts.extend([TEMPLATE_BULLET.format(b=esc(b)) for b in cleaned])

# helper escapes 
def html_escape(s: str) -> str:
    if s is None:
        return ""
    return html.escape(str(s), quote=False)

def html_attr_escape(s: str) -> str:
    if s is None:
        return ""
    return html.escape(str(s), quote=True)

# ---------- renderers ----------
def _labels_for_lang(lang: str) -> Dict[str, str]:
    if (lang or "").lower().startswith("fa"):
        return {
            "premium_head": "💎 Insight+ Premium Edition Exclusive Access",
            "key_points": "📊 نکات کلیدی",
            "opportunities": "🔺 فرصت‌ها",
            "risks": "🔻 ریسک‌ها",
            "signal": "📊 سیگنال",
            "source": "منبع",
            "flash": "⚡ Flash | Quick View",
            "anchor_source": "🔗",
            "bullet_prefix": "✔️",
        }
    # default english labels
    return {
        "premium_head": "💎 Insight+ Premium Edition Exclusive Access",
        "key_points": "📊 Key points",
        "opportunities": "🔺 Opportunities",
        "risks": "🔻 Risks",
        "signal": "📊 Signal",
        "source": "Source",
        "flash": "⚡ Flash | Quick View",
        "anchor_source": "🔗",
        "bullet_prefix": "✔️",
    }

def render_premium(title: str, feed_title: str, date: str, parts: dict, src_link: str, lang: str = "fa") -> str:
    L = _labels_for_lang(lang)
    head = L["premium_head"]
    safe_title = html_escape(title or "")
    safe_feed = html_escape(feed_title or "")
    meta = html_escape(date or "")
    header = f"<b>{safe_title}</b>\n<i>{safe_feed}</i> | <i>{meta}</i>\n\n"


    body_lines: List[str] = []
    tldr = (parts.get("tldr") or "").strip()
    if tldr:
        body_lines.append(f"🔰 {html_escape(tldr)}\n")

    bullets = parts.get("bullets") or []
    if bullets:
        body_lines.append(f"\n{L['key_points']}")
        for b in bullets:
            body_lines.append(f"{L['bullet_prefix']} {html_escape(b)}")

    opps = parts.get("opportunities") or []
    if opps:
        body_lines.append(f"\n{L['opportunities']}")
        for o in opps:
            body_lines.append(f"• {html_escape(o)}")

    risks = parts.get("risks") or []
    if risks:
        body_lines.append(f"\n{L['risks']}")
        for r in risks:
            body_lines.append(f"• {html_escape(r)}")

    sig = (parts.get("signal") or "").strip()
    if sig:
        body_lines.append(f"\n{L['signal']}")
        body_lines.append(f"• {html_escape(sig)}")

    if src_link:
        body_lines.append(f'\n<a href="{html_attr_escape(src_link)}">{L["anchor_source"]} {html_escape(L["source"])}</a>')

    # combine
    return head + "\n" + "\n" + header + "\n".join([ln for ln in body_lines if ln is not None and str(ln).strip()]).strip() + "\n" + "\n" + head

def render_search_fallback(title: str, feed_title: str, date: str, parts: dict, src_link: str, lang: str = "fa") -> str:
    """
    قالب برای حالت فالبک سرچ (شبیه پرمیوم ولی ساده‌تر)
    """
    L = _labels_for_lang(lang)
    safe_title = html_escape(title or "")
    safe_feed = html_escape(feed_title or "")
    meta = html_escape(date or "")

    header = f"<b>{safe_title}</b>\n<i>{safe_feed}</i> | <i>{meta}</i>\n\n"

    lines: List[str] = []

    tldr = (parts.get("tldr") or "").strip()
    if tldr:
        lines.append(f"🔰 {html_escape(tldr)}\n")

    bullets = parts.get("bullets") or []
    for b in bullets:
        lines.append(f"📌 {html_escape(b)}")

    sig = (parts.get("signal") or "").strip()
    if sig:
        lines.append(f"\n📊 {L['signal']}")
        lines.append(f"• {html_escape(sig)}")

    if src_link:
        lines.append(f'\n🔗 <a href="{html_attr_escape(src_link)}">{L["source"]}</a>')

    lines.append(f"\n📝 KeyNotes | Executive Summary")

    return header + "\n".join([ln for ln in lines if ln and str(ln).strip()]).strip()

# translate for title only
def _clean_title_for_translate(title: str) -> str:
    if not title:
        return ""
    if " - " in title:
        return title.split(" - ")[0].strip()
    return title.strip()



def _smart_translate_title(title: str, lang: str) -> str:
    """ترجمه بهتر عنوان: کوتاه‌سازی → ترجمه → پاکسازی"""
    if not title or not lang or not _GT:
        return title or ""
    try:
        # --- مرحله ۱: کوتاه‌سازی به 100 کاراکتر یا جمله اول ---
        sentences = re.split(r"[.!؟\?]", title)
        base = sentences[0].strip() if sentences else title.strip()
        if len(base) > 100:
            base = base[:100]

        # --- مرحله ۲: ترجمه ---
        base = _clean_title_for_translate(title)
        translated = _GT(source="auto", target=lang).translate(base).strip()

        # --- مرحله ۳: پاکسازی ---
        translated = re.sub(r"\s+", " ", translated)
        if not translated or translated.lower() == base.lower():
            return title  # اگر ترجمه بی‌کیفیت بود، متن اصلی را نگه دار

        return translated
    except Exception:
        return title or ""

def render_title_only(
    title: str,
    feed_title: str,
    date: str,
    src_link: str,
    lang: str = "fa",
    content: str = "",
) -> str:    
    """
    قالب حالت ۳ — عنوان + منبع + خلاصه لایت (نه AI).
    """

    # --- labels ---
    if (lang or "").lower().startswith("fa"):
        L = {
            "source": "منبع",
            "flash_footer": "⚡ Flash | Quick View",
            "anchor_source": "🔗",
        }
    else:
        L = {
            "source": "Source",
            "flash_footer": "⚡ Flash | Quick View",
            "anchor_source": "🔗",
        }

    safe_title = html.escape(title or "")
    safe_feed = html.escape(feed_title or "")
    safe_meta = html.escape(date or "")

    header = f"<b>{safe_title}</b>\n<i>{safe_feed}</i> | <i>{safe_meta}</i>\n\n"
    # --- خلاصه لایت ---
    summary_block = ""
    tldr, bullets = _lite_summary_short(title, content or "")
    if tldr:
        if title and tldr.strip().lower() == title.strip().lower():
            tldr = ""
    if tldr:
        summary_block += f"📌 {html.escape(tldr)}\n"
        for b in bullets:
            summary_block += f"🔹 {html.escape(b)}\n"
        if summary_block:
            summary_block += "\n"


    # --- لینک منبع ---
    src_line = ""
    if src_link:
        src_line = f'<a href="{html.escape(src_link)}">{html.escape(L["anchor_source"])} {html.escape(L["source"])}</a>\n'

    footer = L["flash_footer"]

    return header + summary_block + src_line + "\n" + footer


# ---------- convenience small-format helpers used by RSS as fallback ----------
async def format_entry(feed_title: str, entry: Any, summarizer, url: str, lang: str = "fa") -> Optional[str]:
    try:
        title = (getattr(entry, "title", "") or "").strip()
        link = getattr(entry, "link", "") or url

        raw_content = await extract_content_from_entry(entry, link)

        text_for_summary = raw_content or (getattr(entry, "summary", "") or getattr(entry, "description", "") or "").strip() or title

        parts = None
        sfn = getattr(summarizer, "summarize", None)
        if callable(sfn):
            try:
                tldr, bullets = await sfn(title=title, text=text_for_summary)
                parts = {"tldr": tldr or "", "bullets": bullets or []}
            except Exception:
                parts = {"tldr": "", "bullets": []}

        date = _fmt_date(entry)
        link = getattr(entry, "link", "") or url

        if parts and (parts.get("tldr") or parts.get("bullets")):
            return render_search_fallback(title, feed_title, date, parts, link, lang=lang)

        return render_title_only(title, feed_title, date, link, lang=lang, content=raw_content)
    except Exception:
        LOG.exception("format_entry failed", exc_info=True)
        return None



async def format_article(feed_title: str, title: str, link: str, text: str, summarizer, lang: str = "fa") -> Optional[str]:
    try:
        sfn = getattr(summarizer, "summarize_full", None)
        parts = None
        if callable(sfn):
            try:
                tup = await sfn(title=title, text=text)
                parts = {
                    "tldr": tup[0] or "",
                    "bullets": tup[1] or [],
                    "opportunities": tup[2] or [],
                    "risks": tup[3] or [],
                    "signal": tup[4] or "",
                }
            except Exception:
                parts = {"tldr": "", "bullets": [], "opportunities": [], "risks": [], "signal": ""}

        date = ""
        if getattr(title, "published_parsed", None):
            date = _fmt_date(title)  

        if parts and (parts.get("tldr") or parts.get("bullets")):
            return render_premium(title, feed_title, date, parts, link, lang=lang)

        return render_title_only(title, feed_title, date, link, lang=lang)
    except Exception:
        return None