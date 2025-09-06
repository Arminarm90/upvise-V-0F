# app/utils/message_formatter.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from typing import Optional, List, Dict, Any
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
      (🔺 فرصت‌ها…)
      (🔻 ریسک‌ها…)
      (📊 سیگنال…)
      منبع(لینک)

    اصل جدید: حتی اگر خلاصه‌سازی یا متن خام نداشتیم، حداقل «عنوان + لینک» ارسال می‌شود.
    """
    # زبان خلاصه: پارامتر → زبان پرامپت summarizer → پیش‌فرض fa
    lang = (lang or getattr(summarizer, "prompt_lang", "fa") or "fa").lower()

    # 1) متادیتا
    title = _clean_html(getattr(entry, "title", "") or "") or t("msg.untitled", lang)
    link  = getattr(entry, "link", "") or ""
    source_label = (feed_title or t("msg.source", lang)).strip()
    date = _fmt_date(entry)
    meta_line = TEMPLATE_META.format(source=esc(source_label), date=esc(date))

    # 2) متن پایه (در صورت نبود، خلاصه‌سازی را جا می‌اندازیم اما پیام را می‌فرستیم)
    raw = await _raw_from_entry(entry, link)

    tldr = ""
    bullets: List[str] = []
    opps: List[str] = []
    risks: List[str] = []
    signal = ""

    if raw:
        # 3) خلاصه‌سازی منعطف
        author = _author_of(entry) or None
        data = await _summarize_flexible(summarizer, title=title, text=raw, author=author)
        tldr = data.get("tldr") or ""
        bullets = data.get("bullets") or []
        opps = data.get("opportunities") or []
        risks = data.get("risks") or []
        signal = data.get("signal") or ""

    # 4) اِعمال سقف‌ها
    cap = int(getattr(settings, "summary_max_bullets", 4))
    final_bullets = _cap_bullets(bullets, cap)
    final_opps = _cap_section(opps, max(1, min(cap, 4)))
    final_risks = _cap_section(risks, max(1, min(cap, 4)))
    final_signal = (signal or "").strip()

    # 5) ساخت پیام نهایی (HTML تلگرام) — همیشه حداقل عنوان+لینک
    parts: List[str] = [TEMPLATE_TITLE.format(title=esc(title)), meta_line]

    # اگر چیزی برای نمایش هست، سکشن‌ها را اضافه کن
    if tldr:
        parts += ["", TEMPLATE_LEAD.format(lead=esc(tldr)), ""]
    if final_bullets:
        parts += [TEMPLATE_BULLET.format(b=esc(b)) for b in final_bullets]

    # برچسب‌های i18n (fallback ملایم)
    lbl_opp = t("msg.opportunities", lang)
    lbl_risk = t("msg.risks", lang)
    lbl_signal = t("msg.signal", lang)

    if final_opps:
        parts.append("")
        parts.append(TEMPLATE_HEAD_OPP.format(label=esc(lbl_opp)))
        parts += [TEMPLATE_BULLET.format(b=esc(b)) for b in final_opps]

    if final_risks:
        parts.append("")
        parts.append(TEMPLATE_HEAD_RISK.format(label=esc(lbl_risk)))
        parts += [TEMPLATE_BULLET.format(b=esc(b)) for b in final_risks]

    if final_signal:
        parts.append("")
        parts.append(TEMPLATE_HEAD_SIGNAL.format(label=esc(lbl_signal)))
        parts.append(TEMPLATE_SIGNAL_TEXT.format(text=esc(final_signal)))

    if link:
        parts.append(f'\n<a href="{esc_attr(link)}">{esc(t("msg.source", lang))}</a>')

    return "\n".join([p for p in parts if isinstance(p, str)]).strip()


# ==== قالب‌ساز واحد: Page-Watch Article ====
async def format_article(
    feed_title: str,
    title: str,
    link: str,
    text: str,
    summarizer,
    lang: Optional[str] = None,
) -> str | None:
    """
    خروجی یکسان با RSS برای حالت Page-Watch (بدون تاریخ):
      [عنوان]
      [منبع] | (تاریخ خالی)

      🔰 لید
      ✔️ بولت‌ها...
      (🔺 فرصت‌ها…)
      (🔻 ریسک‌ها…)
      (📊 سیگنال…)
      منبع(لینک)

    اصل جدید: حتی اگر خلاصه‌سازی یا متن خام نداشتیم، حداقل «عنوان + لینک» ارسال می‌شود.
    """
    lang = (lang or getattr(summarizer, "prompt_lang", "fa") or "fa").lower()

    safe_title = _clean_html(title or "") or t("msg.untitled", lang)
    source_label = (feed_title or t("msg.source", lang)).strip()
    date = ""  # در Page-Watch معمولاً تاریخ RSS نداریم
    meta_line = TEMPLATE_META.format(source=esc(source_label), date=esc(date))

    raw = (text or "").strip()

    tldr = ""
    bullets: List[str] = []
    opps: List[str] = []
    risks: List[str] = []
    signal = ""

    if raw:
        data = await _summarize_flexible(summarizer, title=safe_title, text=raw, author=None)
        tldr = data.get("tldr") or ""
        bullets = data.get("bullets") or []
        opps = data.get("opportunities") or []
        risks = data.get("risks") or []
        signal = data.get("signal") or ""

    cap = int(getattr(settings, "summary_max_bullets", 4))
    final_bullets = _cap_bullets(bullets, cap)
    final_opps = _cap_section(opps, max(1, min(cap, 4)))
    final_risks = _cap_section(risks, max(1, min(cap, 4)))
    final_signal = (signal or "").strip()

    parts: List[str] = [TEMPLATE_TITLE.format(title=esc(safe_title)), meta_line]

    if tldr:
        parts += ["", TEMPLATE_LEAD.format(lead=esc(tldr)), ""]
    if final_bullets:
        parts += [TEMPLATE_BULLET.format(b=esc(b)) for b in final_bullets]

    lbl_opp = t("msg.opportunities", lang)
    lbl_risk = t("msg.risks", lang)
    lbl_signal = t("msg.signal", lang)

    if final_opps:
        parts.append("")
        parts.append(TEMPLATE_HEAD_OPP.format(label=esc(lbl_opp)))
        parts += [TEMPLATE_BULLET.format(b=esc(b)) for b in final_opps]

    if final_risks:
        parts.append("")
        parts.append(TEMPLATE_HEAD_RISK.format(label=esc(lbl_risk)))
        parts += [TEMPLATE_BULLET.format(b=esc(b)) for b in final_risks]

    if final_signal:
        parts.append("")
        parts.append(TEMPLATE_HEAD_SIGNAL.format(label=esc(lbl_signal)))
        parts.append(TEMPLATE_SIGNAL_TEXT.format(text=esc(final_signal)))

    if link:
        parts.append(f'\n<a href="{esc_attr(link)}">{esc(t("msg.source", lang))}</a>')

    return "\n".join([p for p in parts if isinstance(p, str)]).strip()
