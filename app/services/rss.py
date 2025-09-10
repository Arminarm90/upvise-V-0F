# app/services/rss.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import re
import asyncio
import logging
import time
from typing import Iterable, List, Tuple, Optional
from urllib.parse import urljoin, urlparse
from urllib.parse import urlparse, parse_qs, quote
from datetime import datetime, timezone
from persiantools.jdatetime import JalaliDateTime
import humanize
import httpx
import feedparser
from bs4 import BeautifulSoup
from telegram.ext import Application
from urllib.parse import quote
import random

from ..utils.message_formatter import (
    format_entry,
    format_article,
    _fmt_date,
    render_premium,
    render_search_fallback,
    render_title_only,
)
from ..utils.text import ensure_scheme, root_url
from .summary import Summarizer
from .summary import _translate as _summary_translate
from ..storage.state import StateStore
from ..utils.message_formatter import format_entry, format_article
from ..utils.i18n import get_chat_lang
from ..utils.message_formatter import format_entry, format_article, _fmt_date
from ..utils.text import html_escape as esc, html_attr_escape as esc_attr
from ..utils.i18n import t as _t
# sites 
from sites import google_trends
# تنظیمات پروژه (fallback امن اگر کلیدها وجود نداشتند)
try:
    from app.config import settings  # type: ignore
except Exception:
    class _S:  # fallback منطقی
        ua = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125 Safari/537.36"
        )
        rss_timeout = 12
        fetcher_timeout = 12
        rss_max_items_per_feed = 5
        pagewatch_listing_limit = 30
        pagewatch_links_per_cycle = 3
        rss_ua = ua
        fetcher_ua = ua
    settings = _S()  # type: ignore

# واکشی متن مقاله برای Page‑Watch
try:
    from ..services.fetcher import fetch_article_text
except Exception:
    async def fetch_article_text(url: str, timeout: int = 12) -> str:  # type: ignore
        return ""

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(name)s:%(message)s",
)
LOG = logging.getLogger("rss")


class RSSService:
    """
    سرویس پایش RSS و Page‑Watch (نسخه‌ی یکپارچه با خلاصه‌سازی)
    - اگر URL ذخیره‌شده یک فید معتبر باشد → با feedparser خوانده می‌شود و خلاصه ارسال می‌گردد.
    - اگر فید معتبر نباشد → صفحه‌ی خانه/لیست بررسی می‌شود، لینک‌های مقاله پیدا و خلاصه می‌شوند.
    """

    def __init__(self, store: StateStore, summarizer: Summarizer, search_service, poll_sec: int):
        self.store = store
        self.summarizer = summarizer
        self.search = search_service
        self.poll_sec = poll_sec
        self.stats = {"sent": 0, "skipped": 0, "reasons": {}}
         # نگهداری ایندکس (cursor) برای هر چت در runtime
        self._fallback_cache: dict[tuple[int,str], float] = {}   # key = (chat_id, entry_id)
        self._cursor_per_chat: dict[int,int] = {}
    # ------------------------------------------------------------------ #
    # Feeds
    # ------------------------------------------------------------------ #
    async def _fetch_feed(self, url: str):
        try:
            ua = getattr(settings, "rss_ua", None) or getattr(settings, "ua", None) or "Mozilla/5.0"
            async with httpx.AsyncClient(
                timeout=int(getattr(settings, "rss_timeout", 12)),
                headers={"User-Agent": ua},
                follow_redirects=True,
            ) as c:
                r = await c.get(url)
            if r.status_code >= 400:
                return None
            # feedparser.parse روی thread تا event loop بلاک نشود
            return await asyncio.to_thread(feedparser.parse, r.content)
        except Exception:
            LOG.debug("fetch_feed failed for %s", url, exc_info=True)
            return None

    async def is_valid_feed(self, u: str) -> bool:
        f = await self._fetch_feed(u)
        return bool(f and getattr(f, "entries", None))

    async def feed_title(self, u: str) -> str:
        f = await self._fetch_feed(u)
        if f:
            return getattr(getattr(f, "feed", object()), "title", "") or u
        return u

    # ------------------------------------------------------------------ #
    # HTML helpers (shared)
    # ------------------------------------------------------------------ #
    async def _get_html(self, url: str) -> str:
        try:
            ua = getattr(settings, "fetcher_ua", None) or getattr(settings, "ua", None) or "Mozilla/5.0"
            async with httpx.AsyncClient(
                timeout=int(getattr(settings, "fetcher_timeout", 12)),
                headers={"User-Agent": ua},
                follow_redirects=True,
            ) as c:
                r = await c.get(url)
            if r.is_success and "html" in (r.headers.get("content-type") or "").lower():
                text = r.text or ""
                if len(text) > 300_000:
                    return text[:300_000]
                return text
        except Exception:
            LOG.debug("_get_html failed for %s", url, exc_info=True)
        return ""

    async def _extract_meta(self, url: str) -> List[str]:
        """
        کشف لینک‌های فید از داخل HTML (link rel=alternate و <a> های رایج)
        """
        html = await self._get_html(url)
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        out: List[str] = []
        # <link rel="alternate" ... type="application/rss+xml">
        for link in soup.find_all("link"):
            rel = " ".join(link.get("rel", [])).lower()
            typ = (link.get("type") or "").lower()
            href = (link.get("href") or "").strip()
            if "alternate" in rel and any(t in typ for t in ("rss", "atom", "xml")) and href:
                out.append(urljoin(url, href))
        # <a href="...rss|feed|.xml">
        for a in soup.find_all("a", href=True):
            h = a["href"]
            hl = h.lower()
            if any(k in hl for k in ("rss", "feed")) or hl.endswith(".xml"):
                out.append(urljoin(url, h))
        # dedup حفظ ترتیب
        uniq, seen = [], set()
        for u in out:
            if u not in seen:
                seen.add(u)
                uniq.append(u)
        return uniq

    async def discover_feeds(self, site_url: str) -> List[Tuple[str, str]]:
        """
        کشف چند فید و عنوان آن‌ها (برای سازگاری باقی می‌ماند).
        """
        site_url = ensure_scheme(site_url)
        root = root_url(site_url)
        shortcuts = [
            "/feed/",
            "/feed",
            "/?feed=rss",
            "/?feed=rss2",
            "/rss.xml",
            "/atom.xml",
            "/index.xml",
        ]

        candidates: List[str] = []
        # از HTML
        try:
            candidates.extend(await self._extract_meta(site_url))
        except Exception:
            pass
        # مسیرهای حدسی
        for p in shortcuts:
            candidates.append(urljoin(root + "/", p.lstrip("/")))

        results: List[Tuple[str, str]] = []
        seen: set[str] = set()
        for cu in candidates:
            if cu in seen:
                continue
            seen.add(cu)
            try:
                if await self.is_valid_feed(cu):
                    results.append((cu, await self.feed_title(cu)))
            except Exception:
                continue

        # تلاش جستجو روی دامنه (ممکن است در SearchService پیاده نشده باشد؛ امن try/except)
        if not results:
            try:
                domain = urlparse(root).netloc
                urls = await self.search.feeds_for_domain(domain)  # ممکن است وجود نداشته باشد
                for u in urls[:30]:
                    ul = u.lower()
                    if any(k in ul for k in ("rss", "feed")) or ul.endswith(".xml"):
                        if await self.is_valid_feed(u):
                            results.append((u, await self.feed_title(u)))
            except Exception:
                pass

        uniq, used = [], set()
        for u, t in results:
            if u not in used:
                used.add(u)
                uniq.append((u, t))
        return uniq

    # ------------------------------------------------------------------ #
    # Entry identity (for dedup on RSS)
    # ------------------------------------------------------------------ #
    def entry_id(self, e) -> str:
        return (
            getattr(e, "id", None)
            or getattr(e, "link", None)
            or f"{getattr(e,'title','')}_{int(time.mktime(e.published_parsed)) if getattr(e,'published_parsed',None) else ''}"
        )

    # ------------------------------------------------------------------ #
    # Page‑Watch helpers
    # ------------------------------------------------------------------ #
    def _extract_listing_links(self, page_url: str, html: str, limit: int = 30) -> List[str]:
        """
        لینک‌های «احتمالاً مقاله/خبر» را از یک صفحه‌ی لیستی/خانه استخراج می‌کند.
        """
        if not html:
            return []
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return []

        base_host = urlparse(page_url).netloc.lower()
        out: List[str] = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href:
                continue
            u = urljoin(page_url, href)
            pu = urlparse(u)
            if pu.scheme not in ("http", "https") or not pu.netloc:
                continue
            host = pu.netloc.lower()
            if not (host == base_host or host.endswith("." + base_host)):
                continue
            path = pu.path or "/"
            path_l = path.lower()
            looks_article = (
                "/news/" in path_l
                or "/article" in path_l
                or "/post" in path_l
                or "/blog/" in path_l
                or "/stories/" in path_l
                or "/202" in path_l  # 202x
                or "/201" in path_l  # 201x (قدیمی)
                or path.count("/") >= 2
            )
            if looks_article:
                out.append(u)

        # dedup + محدودیت
        uniq, seen = [], set()
        for u in out:
            if u not in seen:
                seen.add(u)
                uniq.append(u)
            if len(uniq) >= limit:
                break
        return uniq

    def _page_title(self, html: str, fallback: str) -> str:
        try:
            soup = BeautifulSoup(html, "html.parser")
            if soup.title and soup.title.string:
                return soup.title.string.strip()
            h1 = soup.find("h1")
            if h1 and h1.get_text(strip=True):
                return h1.get_text(strip=True)
        except Exception:
            pass
        return fallback
    
    # ---------- helpers for fallback & summarizer wrapper ----------

    # def _fmt_date(self, entry) -> str:
    #     """تبدیل تاریخ RSS به YYYY-MM-DD (published → updated). اگر نبود رشته خالی برمی‌گرداند."""
    #     try:
    #         if getattr(entry, "published_parsed", None):
    #             return datetime(*entry.published_parsed[:6]).strftime("%Y-%m-%d")
    #         if getattr(entry, "updated_parsed", None):
    #             return datetime(*entry.updated_parsed[:6]).strftime("%Y-%m-%d")
    #     except Exception:
    #         pass
    #     return ""

    async def _search_related(self, query: str, max_results: int = 3) -> list:
        """
        Wrapper که تلاش می‌کند از self.search استفاده کند تا نتایج مرتبط بگیرد.
        خروجی: لیستی از dict با حداقل کلید 'link' و ترجیحاً 'title' و 'snippet'.
        """
        if not query:
            return []
        try:
            # تلاشی برای تطبیق API های مختلف SearchService
            if self.search:
                # try common method names
                for name in ("search", "search_web", "web_search", "search_google", "search_serper", "query"):
                    fn = getattr(self.search, name, None)
                    if callable(fn):
                        try:
                            res = await fn(query, max_results) if asyncio.iscoroutinefunction(fn) else fn(query, max_results)
                            # Normalize: expect list of dicts or list of tuples/strings
                            out = []
                            for it in (res or [])[:max_results]:
                                if isinstance(it, dict):
                                    out.append(it)
                                elif isinstance(it, (list, tuple)) and len(it) >= 1:
                                    out.append({"link": it[0], "title": it[1] if len(it) > 1 else ""})
                                elif isinstance(it, str):
                                    out.append({"link": it})
                            if out:
                                return out
                        except Exception:
                            continue
        except Exception:
            LOG.debug("_search_related failed", exc_info=True)

        # اگر هیچ SearchService ای در دسترس نبود یا ناموفق بود، برگردون خالی
        return []

    async def _build_text_from_search(self, items: list, max_chars: int = 3500) -> str:
        """
        با گرفتن نتایج جستجو (لیستی از dict که حداقل 'link' دارند)،
        صفحات مرتبط را fetch می‌کند (با concurrency محدود) و متن‌ها را concat می‌کند.
        خروجی: متن تجمیع شده تا max_chars.
        """
        if not items:
            return ""
        sem = asyncio.Semaphore(int(getattr(settings, "fetcher_concurrency", 4)))
        async def _fetch_text(url):
            async with sem:
                try:
                    html = await self._get_html(url)
                    if not html:
                        return ""
                    soup = BeautifulSoup(html, "html.parser")
                    for t in soup(["script","style","noscript"]):
                        t.decompose()
                    text = soup.get_text(" ", strip=True)
                    # پاک‌سازی اضافه
                    text = re.sub(r"\s+", " ", text).strip()
                    return text
                except Exception:
                    return ""

        tasks = []
        # محدود کن به چند لینک اول
        for it in items[:6]:
            link = (it.get("link") or it.get("url") or "").strip()
            if link:
                tasks.append(asyncio.create_task(_fetch_text(link)))
        if not tasks:
            return ""

        results = await asyncio.gather(*tasks, return_exceptions=True)
        parts = []
        total = 0
        for r in results:
            if isinstance(r, Exception) or not r:
                continue
            parts.append(r)
            total += len(r)
            if total >= max_chars:
                break
        if not parts:
            return ""
        agg = "\n\n".join(parts)
        return agg[:max_chars]

    async def _ai_summarize_full(self, title: str, text: str) -> dict:
        """
        Wrapper که خروجی استاندارد dict می‌دهد:
        {tldr, bullets, opportunities, risks, signal}
        استفاده: fallback web text -> این رو فراخوان کن.
        """
        try:
            if not self.summarizer:
                return {"tldr":"", "bullets":[], "opportunities":[], "risks":[], "signal":""}

            # اگر summarizer متد summarize_full داره ازش استفاده کن
            sf = getattr(self.summarizer, "summarize_full", None)
            if callable(sf):
                try:
                    res = await sf(title=title, text=text, author=None)
                except TypeError:
                    # در صورت signature متفاوت
                    res = await sf(title, text)
                # res ممکنه tuple یا dict باشه
                if isinstance(res, dict):
                    return {
                        "tldr": (res.get("tldr") or "") if isinstance(res.get("tldr",""), str) else "",
                        "bullets": list(res.get("bullets") or []),
                        "opportunities": list(res.get("opportunities") or []),
                        "risks": list(res.get("risks") or []),
                        "signal": (res.get("signal") or "") if isinstance(res.get("signal",""), str) else "",
                    }
                if isinstance(res, (list, tuple)):
                    tldr, bullets, opportunities, risks, signal = (list(res) + ["", [], [], [], ""])[:5]
                    return {
                        "tldr": tldr or "",
                        "bullets": list(bullets or []),
                        "opportunities": list(opportunities or []),
                        "risks": list(risks or []),
                        "signal": signal or "",
                    }

            # اگر summarize_full موجود نیست یا خالی داد، fallback به summarize (tl;dr + bullets)
            sfn = getattr(self.summarizer, "summarize", None)
            if callable(sfn):
                try:
                    tldr, bullets = await sfn(title=title, text=text, author=None)
                except TypeError:
                    tldr, bullets = await sfn(title, text)
                return {"tldr": tldr or "", "bullets": list(bullets or []), "opportunities":[], "risks":[], "signal":""}
        except Exception:
            LOG.exception("_ai_summarize_full failed", exc_info=True)

        return {"tldr":"", "bullets":[], "opportunities":[], "risks":[], "signal":""}

    def _build_message_from_full(self, title: str, feed_title: str, date: str, parts: dict, src_link: str, lang: str = "fa") -> str:
        """
        ساخت پیام HTML یکنواخت از خروجی full summary (parts dict).
        قالب: Title / Feed | date / TLDR / bullets / opportunities / risks / signal / منبع
        """
        # header
        safe_title = esc(title or "")
        safe_feed = esc(feed_title or "")
        meta = esc(date or "")
        header = f"<b>{safe_title}</b>\n<i>{safe_feed}</i> | <i>{meta}</i>\n\n"

        body_parts = []
        tldr = (parts.get("tldr") or "").strip()
        if tldr:
            body_parts.append(f"🔰 {esc(tldr)}\n")

        bullets = parts.get("bullets") or []
        for b in bullets:
            body_parts.append(f"✔️ {esc(b)}")

        # opportunities
        opps = parts.get("opportunities") or []
        if opps:
            body_parts.append("\n🔺 " + esc(_t("msg.opportunities", lang)))
            for o in opps:
                body_parts.append(f"✔️ {esc(o)}")

        # risks
        risks = parts.get("risks") or []
        if risks:
            body_parts.append("\n🔻 " + esc(_t("msg.risks", lang)))
            for r in risks:
                body_parts.append(f"✔️ {esc(r)}")

        # signal
        sig = (parts.get("signal") or "").strip()
        if sig:
            body_parts.append("\n📊 " + esc(_t("msg.signal", lang)))
            body_parts.append(f"• {esc(sig)}")

        # source link
        if src_link:
            body_parts.append(f'\n<a href="{esc_attr(src_link)}">{esc(_t("msg.source", lang))}</a>')

        return header + "\n".join(body_parts).strip()

    async def _process_feed(self, app: Application, cid_int: int, url: str, f, chat_lang: str, reporter):
        """
        پردازش یک فید (فید از قبل با _fetch_feed گرفته شده و پارس شده).
        این تابع مسئول ارسال پیام‌ها، آپدیت seen و reporter/stat است.
        """
        try:
            # Google Trends
            if "trends.google.com/trending/rss" in url:
                html = await google_trends.process_google_trends(f, self.store, cid_int, url)
                if html:
                    await app.bot.send_message(chat_id=cid_int, text=html, parse_mode="HTML", disable_web_page_preview=True)
                    self.stats["sent"] += 1
                    if reporter:
                        try:
                            reporter.record(url, "sent")
                        except Exception:
                            pass
                return

            # معمولی: بررسی ورودی‌ها و ارسال پیام‌ها
            seen = set(self.store.get_seen(cid_int, url))
            cap = int(getattr(settings, "rss_max_items_per_feed", 10))
            new_entries = []
            for e in (getattr(f, "entries", []) or [])[:cap]:
                eid = self.entry_id(e) if "trends.google.com" not in url else f"trend:{(getattr(e,'title','') or '').strip()}"
                if not eid or eid in seen:
                    continue
                new_entries.append((eid, e))

            feed_title = getattr(getattr(f, "feed", object()), "title", "") or urlparse(url).netloc

            for eid, e in reversed(new_entries):
                title_text = (getattr(e, "title", "") or "").strip()
                link = getattr(e, "link", "") or ""
                date = _fmt_date(e)

                sent_ok = False  # <-- پرچم اینکه آیا چیزی ارسال شد یا نه

                # 1) خلاصه AI — تلاش مستقیم برای summarize_full
                entry_text = (getattr(e, "summary", "") or getattr(e, "description", "") or "").strip()
                try:
                    parts_tup = await self.summarizer.summarize_full(title_text, entry_text)
                except Exception as ex:
                    parts_tup = ("", [], [], [], "")
                    
                parts_dict = {
                    "tldr": parts_tup[0] or "",
                    "bullets": parts_tup[1] or [],
                    "opportunities": parts_tup[2] or [],
                    "risks": parts_tup[3] or [],
                    "signal": parts_tup[4] or "",
                }

                has_meaningful = bool(parts_dict["tldr"] or parts_dict["bullets"])

                if has_meaningful:
                    msg = render_premium(title_text, feed_title, date, parts_dict, link, lang=chat_lang)
                    try:
                        await app.bot.send_message(chat_id=cid_int, text=msg, parse_mode="HTML", disable_web_page_preview=True)
                        self.stats["sent"] += 1
                        seen.add(eid)
                        sent_ok = True
                        if reporter:
                            reporter.record(url, "sent", extra="ai_premium")
                        self.store.set_seen(cid_int, url, seen)
                    except Exception:
                        LOG.debug("send_message failed for ai_premium (cid=%s)", cid_int, exc_info=True)

                # 2) اگر مرحله اول چیزی نداد → fallback سرچ
                if not sent_ok and title_text:
                    throttle_sec = int(getattr(settings, "search_fallback_throttle_sec", 600))
                    last = self._fallback_cache.get((cid_int, eid), 0)
                    if time.time() - last >= throttle_sec:
                        self._fallback_cache[(cid_int, eid)] = time.time()
                        try:
                            search_items = await self._search_related(title_text, max_results=int(getattr(settings, "search_fallback_max_results", 3)))
                            if search_items:
                                agg = await self._build_text_from_search(search_items, max_chars=int(getattr(settings, "search_fallback_max_chars", 3500)))
                                LOG.debug("search fallback: agg length=%d for title=%r", len(agg or ""), title_text)
                                if agg:
                                    parts_search = await self._ai_summarize_full(title_text, agg)
                                    has_content = any([
                                        parts_search.get("tldr"),
                                        parts_search.get("bullets"),
                                        parts_search.get("opportunities"),
                                        parts_search.get("risks"),
                                        parts_search.get("signal"),
                                    ])
                                    if has_content:
                                        src_link = link or (search_items[0].get("link") or "")
                                        msg = render_search_fallback(title_text, feed_title, date, parts_search, src_link, lang=chat_lang)
                                        try:
                                            await app.bot.send_message(chat_id=cid_int, text=msg, parse_mode="HTML", disable_web_page_preview=True)
                                            self.stats["sent"] += 1
                                            seen.add(eid)
                                            sent_ok = True
                                            if reporter:
                                                reporter.record(url, "sent", extra="web_fallback")
                                            self.store.set_seen(cid_int, url, seen)
                                        except Exception:
                                            LOG.debug("send_message failed for web_fallback (cid=%s)", cid_int, exc_info=True)
                        except Exception:
                            LOG.debug("search fallback failed for title=%r", title_text, exc_info=True)

                # 3) اگر مرحله دوم هم چیزی نداد → title-only
                if not sent_ok:
                    try:
                        draft_html = await format_entry(feed_title, e, self.summarizer, url, lang=chat_lang)
                    except Exception:
                        draft_html = None

                    if draft_html and draft_html.strip():
                        try:
                            await app.bot.send_message(chat_id=cid_int, text=draft_html, parse_mode="HTML", disable_web_page_preview=True)
                            self.stats["sent"] += 1
                            seen.add(eid)
                            sent_ok = True
                            if reporter:
                                reporter.record(url, "sent", extra="title_only_from_formatter")
                            self.store.set_seen(cid_int, url, seen)
                        except Exception:
                            LOG.debug("send_message failed for title_only (cid=%s)", cid_int, exc_info=True)
                    else:
                        msg = render_title_only(title_text, feed_title, date, link, lang=chat_lang, translate_fn=_summary_translate,)
                        try:
                            await app.bot.send_message(chat_id=cid_int, text=msg, parse_mode="HTML", disable_web_page_preview=True)
                            self.stats["sent"] += 1
                            seen.add(eid)
                            sent_ok = True
                            if reporter:
                                reporter.record(url, "sent", extra="title_only_min")
                            self.store.set_seen(cid_int, url, seen)
                        except Exception:
                            LOG.debug("send_message failed for minimal title_only (cid=%s)", cid_int, exc_info=True)

                # اگر هیچ‌کدوم جواب نداد → skip
                if not sent_ok:
                    self.stats["skipped"] += 1
                    self.stats["reasons"]["ai_empty_output"] = self.stats["reasons"].get("ai_empty_output", 0) + 1

        except Exception as ex:
            LOG.exception("process_feed error for %s (cid=%s): %s", url, cid_int, ex)
            

    # ------------------------------------------------------------------ #
    # Main poll
    # ------------------------------------------------------------------ #
    # async def poll_once(self, app: Application):
    #     self.stats = {"sent": 0, "skipped": 0, "reasons": {}}

    #     reporter = app.bot_data.get("reporter")
    #     for cid, st in self.store.iter_chats():
    #         try:
    #             cid_int = int(cid)
    #         except Exception:
    #             try:
    #                 cid_int = int(st.get("chat_id") or 0)
    #             except Exception:
    #                 continue

    #         # زبان چت و تنظیم برای Summarizer
    #         try:
    #             chat_lang = get_chat_lang(self.store, cid_int)
    #             try:
    #                 self.summarizer.prompt_lang = chat_lang
    #             except Exception:
    #                 pass
    #         except Exception:
    #             chat_lang = "fa"

    #         feeds: Iterable[str] = list(st.get("feeds", []))
    #         random.shuffle(feeds)   # ✅ ترتیب فیدها هر بار رندوم میشه

    #         for url in feeds:
    #             print("💣this is the target ====",url)
    #             url = ensure_scheme(url)
    #             try:
    #                 # مسیر RSS
    #                 f = await self._fetch_feed(url)
    #                 if "trends.google.com/trending/rss" in url:
    #                     html = await google_trends.process_google_trends(f, self.store, cid_int, url)
    #                     if html:
    #                         await app.bot.send_message(
    #                             chat_id=cid_int,
    #                             text=html,
    #                             parse_mode="HTML",
    #                             disable_web_page_preview=True,
    #                         )
    #                     continue

    #                 if f and getattr(f, "entries", None):
    #                     seen = set(self.store.get_seen(cid_int, url))
    #                     new_entries = []
    #                     cap = int(getattr(settings, "rss_max_items_per_feed", 10))
    #                     for e in f.entries[:cap]:
    #                         if "trends.google.com" in url:
    #                             eid = f"trend:{getattr(e, 'title', '').strip()}"
    #                         else:
    #                             eid = self.entry_id(e)                            
    #                         if not eid or eid in seen:
    #                             continue
    #                         new_entries.append((eid, e))

    #                     feed_title = getattr(getattr(f, "feed", object()), "title", "") or urlparse(url).netloc
    #                     for eid, e in reversed(new_entries):
    #                         html = await format_entry(feed_title, e, self.summarizer, url, lang=chat_lang)
    #                         if not html or not str(html).strip():
    #                             reason = "ai_empty_output"
    #                             self.stats["reasons"][reason] = self.stats["reasons"].get(reason, 0) + 1
    #                             self.stats["skipped"] += 1
    #                             continue

    #                         await app.bot.send_message(
    #                             chat_id=cid_int,
    #                             text=html,
    #                             parse_mode="HTML",
    #                             disable_web_page_preview=True,
    #                         )
    #                         self.stats["sent"] += 1
    #                         seen.add(eid)

    #                     self.store.set_seen(cid_int, url, seen)
    #                     continue  # RSS مسیر کامل شد؛ به URL بعدی برو

    #                 # --- مسیر Page‑Watch (خلاصه‌ساز یکپارچه) ---
    #                 page_html = await self._get_html(url)
    #                 if not page_html:
    #                     continue

    #                 listing_limit = int(getattr(settings, "pagewatch_listing_limit", 30))
    #                 links = self._extract_listing_links(url, page_html, limit=listing_limit)
    #                 if not links:
    #                     continue

    #                 seen = set(self.store.get_seen(cid_int, url))
    #                 per_cycle = int(getattr(settings, "pagewatch_links_per_cycle", 3))
    #                 new_links = [u for u in links if u not in seen][:per_cycle]
    #                 if not new_links:
    #                     continue

    #                 feed_title = urlparse(url).netloc or url
    #                 for link in reversed(new_links):
    #                     # برای عنوان و متن مقاله
    #                     title_html = await self._get_html(link)
    #                     title = self._page_title(title_html, fallback=urlparse(link).path or link)

    #                     # متن مقاله: اول از fetcher (تمیز و آماده‌ی خلاصه‌سازی)
    #                     article_text = await fetch_article_text(
    #                         link, timeout=int(getattr(settings, "fetcher_timeout", 12))
    #                     )
    #                     if not article_text:
    #                         # اگر نشد، حداقل متن خام صفحه را به Summarizer بدهیم تا Lite بسازد
    #                         try:
    #                             soup = BeautifulSoup(title_html or "", "html.parser")
    #                             for tnode in soup(["script", "style", "noscript"]):
    #                                 tnode.decompose()
    #                             article_text = (soup.get_text(" ", strip=True) or title).strip()
    #                         except Exception:
    #                             article_text = title or link

    #                     html = await format_article(
    #                         feed_title=feed_title,
    #                         title=title,
    #                         link=link,
    #                         text=article_text,
    #                         summarizer=self.summarizer,
    #                         lang=chat_lang,
    #                     )
    #                     if not html or not str(html).strip():
    #                         reason = "article_empty_output"
    #                         self.stats["reasons"][reason] = self.stats["reasons"].get(reason, 0) + 1
    #                         self.stats["skipped"] += 1
    #                     try:
    #                         await app.bot.send_message(
    #                             chat_id=cid_int,
    #                             text=html,
    #                             parse_mode="HTML",
    #                             disable_web_page_preview=True,
    #                         )
    #                         self.stats["sent"] += 1
    #                         seen.add(link)
    #                     except Exception:
    #                         LOG.debug("send_message failed for %s", link, exc_info=True)

    #                 self.store.set_seen(cid_int, url, seen)

    #             except Exception as ex:
    #                 LOG.exception("poll_once error for %s: %s", url, ex)

    #     # ---- لاگ آمار یک‌بار در انتهای poll_once
    #     total = self.stats["sent"] + self.stats["skipped"]
    #     if total:
    #         ratio = round(100 * self.stats["skipped"] / total, 1)
    #         LOG.info(
    #             "[SUMMARY][STATS] sent=%d skipped=%d (%.1f%%) reasons=%s",
    #             self.stats["sent"],
    #             self.stats["skipped"],
    #             ratio,
    #             self.stats["reasons"],
    #         )

    async def poll_once(self, app: Application):
        reporter = app.bot_data.get("reporter")
        # reset stats for this run (اختیاری ولی مفید برای گزارش)
        self.stats = {"sent": 0, "skipped": 0, "reasons": {}}

        for cid, st in self.store.iter_chats():
            try:
                cid_int = int(cid)
            except Exception:
                try:
                    cid_int = int(st.get("chat_id") or 0)
                except Exception:
                    continue

            # زبان چت
            try:
                chat_lang = get_chat_lang(self.store, cid_int)
                try:
                    self.summarizer.prompt_lang = chat_lang
                except Exception:
                    pass
            except Exception:
                chat_lang = "fa"

            feeds: list[str] = list(st.get("feeds", []))
            if not feeds:
                continue

            # shuffle order to avoid bias toward some feeds
            random.shuffle(feeds)

            # batch cursor (in-memory). اگر بخوای persistent کن می‌تونی اینو در StateStore ذخیره کنی.
            start = self._cursor_per_chat.get(cid_int, 0)
            batch_size = int(getattr(settings, "rss_batch_size", 20))
            if start >= len(feeds):
                start = 0
            end = min(len(feeds), start + batch_size)
            batch = feeds[start:end]
            next_index = end if end < len(feeds) else 0
            self._cursor_per_chat[cid_int] = next_index

            LOG.info("Polling chat=%s feeds_total=%d batch=%d (start=%d next=%d)",
                     cid_int, len(feeds), len(batch), start, next_index)

            # concurrency limiter برای fetch ها
            concurrency = int(getattr(settings, "rss_fetch_concurrency", 6))
            sem = asyncio.Semaphore(concurrency)

            async def _fetch_with_sem(u):
                async with sem:
                    try:
                        return await self._fetch_feed(u)
                    except Exception as ex:
                        LOG.debug("fetch failed for %s: %s", u, ex)
                        return None

            # fetch همهٔ فیدهای batch به صورت concurrency-limited
            fetch_tasks = [asyncio.create_task(_fetch_with_sem(u)) for u in batch]
            results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

            # برای هر فید که با موفقیت گرفته شده، یک task پردازش ایجاد کن
            proc_tasks = []
            for url, res in zip(batch, results):
                if isinstance(res, Exception) or not res:
                    LOG.debug("No feed parsed for %s (chat=%s): %s", url, cid_int, res)
                    # می‌تونی اینجا reporter.record(url,'skipped', 'fetch_failed') بزنی اگر خواستی
                    continue
                # پردازش را موازی اجرا کن (هر پردازش خودش ارسال را انجام می‌دهد)
                proc_tasks.append(asyncio.create_task(self._process_feed(app, cid_int, url, res, chat_lang, reporter)))

            # منتظر بمون که همه پردازش‌ها تموم بشن (اگر proc_tasks خالی بود همین خط سریع رد میشه)
            if proc_tasks:
                await asyncio.gather(*proc_tasks, return_exceptions=True)

        # لاگ کلی آمار
        total = self.stats["sent"] + self.stats["skipped"]
        if total:
            ratio = round(100 * self.stats["skipped"] / total, 1)
            LOG.info("[SUMMARY][STATS] sent=%d skipped=%d (%.1f%%) reasons=%s",
                     self.stats["sent"], self.stats["skipped"], ratio, self.stats["reasons"])
