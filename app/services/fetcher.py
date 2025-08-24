# app/services/fetcher.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import logging
from typing import Optional
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urlunparse, urljoin

# --- تنظیمات و لاگ ---
try:
    from ..config import settings  # داخل پکیج app.services
except Exception:  # fallback امن اگر config در دسترس نباشد (نباید رخ دهد)
    class _S:
        fetcher_ua = "Mozilla/5.0 (TelegramBot; +https://core.telegram.org/bots)"
        ua = fetcher_ua
        botwall_pattern = r"(enable javascript|just a moment|cloudflare|access denied|verify you are a human)"
        fetcher_max_html_bytes = 500_000
        fetcher_timeout = 12
    settings = _S()  # type: ignore

LOG = logging.getLogger("fetcher")

# یک UA قابل‌قبول برای اکثر سایت‌ها (از config خوانده می‌شود)
UA = (getattr(settings, "fetcher_ua", None) or getattr(settings, "ua", None)
      or "Mozilla/5.0 (TelegramBot; +https://core.telegram.org/bots)")

# تشخیص صفحات bot-wall/JS-wall (از config خوانده و سپس کامپایل می‌شود)
try:
    _BOTWALL_PAT = re.compile(getattr(settings, "botwall_pattern", r"(enable javascript|just a moment|cloudflare|access denied|verify you are a human)"),
                              flags=re.I)
except Exception:
    _BOTWALL_PAT = re.compile(r"(enable javascript|just a moment|cloudflare|access denied|verify you are a human)", re.I)

# سقف امن برای متن HTML واکشی‌شده (برای محافظت از حافظه/کارایی)
try:
    _MAX_HTML_BYTES = int(getattr(settings, "fetcher_max_html_bytes", 500_000))
except Exception:
    _MAX_HTML_BYTES = 500_000


def _is_private_host(netloc: str) -> bool:
    """محافظت سبک در برابر SSRF: رد برخی IPهای خصوصی/localhost."""
    host = (netloc or "").split(":")[0].strip().lower()
    # localhost
    if host in {"localhost"}:
        return True
    # IPv4 ساده
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host):
        try:
            octs = [int(x) for x in host.split(".")]
        except Exception:
            return True
        if octs[0] == 127:
            return True
        if octs[0] == 10:
            return True
        if octs[0] == 192 and octs[1] == 168:
            return True
        if octs[0] == 172 and 16 <= octs[1] <= 31:
            return True
    # IPv6 loopback/Link-local (ساده)
    if host in {"::1"} or host.startswith("fe80:"):
        return True
    return False


def _clean_html(s: str) -> str:
    if not s:
        return ""
    soup = BeautifulSoup(s, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    txt = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", txt).strip()


def _extract_main_text(html: str) -> str:
    """برجسته‌ترین متنِ مقاله را برمی‌گرداند (article > section/div/main > p ها)."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")

    # 1) ناحیه <article>
    art = soup.find("article")
    if art:
        return _clean_html(art.get_text(" ", strip=True))

    # 2) پرپاراگراف‌ترین container
    best_text, best_score = "", 0
    for tag in soup.find_all(["main", "section", "div"]):
        ps = tag.find_all("p")
        if not ps:
            continue
        text = " ".join(p.get_text(" ", strip=True) for p in ps)
        score = len(text)
        if score > best_score:
            best_text, best_score = text, score

    if best_text:
        return _clean_html(best_text)

    # 3) fallback: کل متن تمیز
    return _clean_html(html)


async def _try_amp_or_mobile(client: httpx.AsyncClient, url: str) -> str:
    """نسخه AMP یا موبایل را امتحان می‌کند و در صورت موفقیت HTML می‌دهد."""
    # /amp
    try:
        amp_url = url.rstrip("/") + "/amp"
        r = await client.get(amp_url)
        if r.status_code < 400 and "html" in (r.headers.get("content-type") or "").lower():
            html = r.text or ""
            if len(html) > _MAX_HTML_BYTES:
                html = html[:_MAX_HTML_BYTES]
            if not _BOTWALL_PAT.search(html):
                return html
            else:
                LOG.debug("fetcher: botwall on /amp variant url=%s", amp_url)
        elif r.status_code >= 400:
            LOG.debug("fetcher: non-2xx on /amp variant code=%s url=%s", r.status_code, amp_url)
    except Exception:
        pass

    # m.<host>
    try:
        pu = urlparse(url)
        if not pu.netloc.startswith("m."):
            m_pu = pu._replace(netloc="m." + pu.netloc)
            m_url = urlunparse(m_pu)
            r = await client.get(m_url)
            if r.status_code < 400 and "html" in (r.headers.get("content-type") or "").lower():
                html = r.text or ""
                if len(html) > _MAX_HTML_BYTES:
                    html = html[:_MAX_HTML_BYTES]
                if not _BOTWALL_PAT.search(html):
                    return html
                else:
                    LOG.debug("fetcher: botwall on m. variant url=%s", m_url)
            elif r.status_code >= 400:
                LOG.debug("fetcher: non-2xx on m. variant code=%s url=%s", r.status_code, m_url)
    except Exception:
        pass

    # mobile./touch. (اختیاری)
    try:
        pu = urlparse(url)
        for prefix in ("mobile.", "touch."):
            if pu.netloc.startswith(prefix):
                continue
            alt_pu = pu._replace(netloc=prefix + pu.netloc)
            alt_url = urlunparse(alt_pu)
            r = await client.get(alt_url)
            if r.status_code < 400 and "html" in (r.headers.get("content-type") or "").lower():
                html = r.text or ""
                if len(html) > _MAX_HTML_BYTES:
                    html = html[:_MAX_HTML_BYTES]
                if not _BOTWALL_PAT.search(html):
                    return html
                else:
                    LOG.debug("fetcher: botwall on %s variant url=%s", prefix, alt_url)
            elif r.status_code >= 400:
                LOG.debug("fetcher: non-2xx on %s variant code=%s url=%s", prefix, r.status_code, alt_url)
    except Exception:
        pass

    return ""


def _append_meta_description(html: str, current_text: str) -> str:
    """اگر متن کم بود، توضیح og:description/meta[name=description] به ابتدای متن افزوده می‌شود."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        og = soup.find("meta", attrs={"property": "og:description"})
        md = soup.find("meta", attrs={"name": "description"})
        desc = (og.get("content") if og else "") or (md.get("content") if md else "")
        desc = (desc or "").strip()
        if desc and desc.lower() not in (current_text or "").lower():
            return (desc + "\n" + (current_text or "")).strip()
    except Exception:
        pass
    return current_text


def _effective_timeout(passed: Optional[int]) -> int:
    """اگر timeout معتبری پاس نشده بود، از settings.fetcher_timeout استفاده کن."""
    try:
        t = int(passed) if passed is not None else int(getattr(settings, "fetcher_timeout", 12))
        return t if t > 0 else int(getattr(settings, "fetcher_timeout", 12))
    except Exception:
        return 12


async def fetch_article_text(url: str, timeout: int = 12) -> str:
    """
    متن نسبتاً کامل مقاله را واکشی و استخراج می‌کند.
    ترتیب تلاش:
      1) صفحه اصلی
      2) AMP واقعی از <link rel="amphtml"> (اگر بود)، وگرنه /amp
      3) m.<host> / mobile. / touch.
      4) افزودن meta description اگر متن کوتاه بود

    محافظت‌ها:
      - SSRF سبک: فقط http/https و رد netlocهای خصوصی/localhost
      - محدودسازی اندازهٔ محتوا
      - تشخیص bot-wall در همهٔ شاخه‌ها

    خروجی خالی "" اگر محتوای قابل‌اتکا به دست نیاید.
    """
    if not url:
        LOG.debug("fetcher: empty url")
        return ""

    # SSRF سبک
    try:
        pu = urlparse(url)
        if pu.scheme not in ("http", "https") or not pu.netloc:
            LOG.debug("fetcher: invalid scheme or netloc url=%s", url)
            return ""
        if _is_private_host(pu.netloc):
            LOG.debug("fetcher: private/localhost blocked url=%s", url)
            return ""
    except Exception:
        LOG.debug("fetcher: url parse failed url=%s", url)
        return ""

    eff_timeout = _effective_timeout(timeout)

    try:
        async with httpx.AsyncClient(timeout=eff_timeout, headers={"User-Agent": UA}, follow_redirects=True) as s:
            # 1) صفحه اصلی
            r = await s.get(url)
            if r.status_code >= 400:
                LOG.debug("fetcher: non-2xx main code=%s url=%s", r.status_code, url)
                return ""
            ct = (r.headers.get("content-type") or "").lower()
            if "html" not in ct:
                LOG.debug("fetcher: non-html content-type=%s url=%s", ct, url)
                return ""
            html = r.text or ""
            if len(html) > _MAX_HTML_BYTES:
                html = html[:_MAX_HTML_BYTES]
            if _BOTWALL_PAT.search(html):
                LOG.debug("fetcher: botwall detected on main url=%s", url)
                return ""

            text = _extract_main_text(html)

            # 2) AMP واقعی از <link rel="amphtml"> (در اولویت)
            amp_html: Optional[str] = None
            if len(text) < 300:
                try:
                    soup = BeautifulSoup(html, "html.parser")
                    # یافتن amphtml با انواع rel
                    amp_link = soup.find(
                        "link",
                        rel=lambda v: v
                        and ("amphtml" in ([x.lower() for x in v] if isinstance(v, list) else [str(v).lower()])),
                    )
                    if amp_link and amp_link.get("href"):
                        amp_url = urljoin(url, amp_link["href"])
                        ra = await s.get(amp_url)
                        if ra.status_code < 400 and "html" in (ra.headers.get("content-type") or "").lower():
                            amp_html = ra.text or ""
                            if len(amp_html) > _MAX_HTML_BYTES:
                                amp_html = amp_html[:_MAX_HTML_BYTES]
                            if _BOTWALL_PAT.search(amp_html):
                                LOG.debug("fetcher: botwall on real amp url=%s", amp_url)
                                amp_html = None
                        else:
                            LOG.debug("fetcher: non-2xx real amp code=%s url=%s", ra.status_code, amp_url)
                except Exception:
                    amp_html = None

            # اگر AMP واقعی نبود یا متن هنوز کوتاه بود → /amp و سپس mobile variants
            if len(text) < 300 and not amp_html:
                alt_html = await _try_amp_or_mobile(s, url)
                if alt_html:
                    amp_html = alt_html

            if amp_html:
                amp_text = _extract_main_text(amp_html)
                if len(amp_text) > len(text):
                    text = amp_text
                    html = amp_html  # برای متا

            # 3) اگر هنوز کوتاه است، توضیح متا را اضافه کن
            if len(text) < 250:
                text = _append_meta_description(html, text)

            # خروجی تمیز
            text = (text or "").strip()
            if len(text) < 120:
                LOG.debug("fetcher: too short after extraction url=%s len=%d", url, len(text))
                return ""
            return text
    except Exception as ex:
        LOG.debug("fetcher: exception url=%s err=%s", url, ex)
        return ""
