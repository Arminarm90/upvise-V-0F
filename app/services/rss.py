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
from provider import google_trends, remoteok
from provider.vipgold import process_gold, process_news, collect_gold, process_gold_and_news

import yaml
from pathlib import Path

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

# Macher list 
PROVIDERS = [
    (lambda u: "xminit.com/vip/goldir" in (u or "").lower(), process_gold_and_news),
]

# Admin sites 
ADMIN_SITES_FILE = Path(__file__).resolve().parent.parent / "admin_sites/admin_sites.yaml"
try:
    with open(ADMIN_SITES_FILE, "r", encoding="utf-8") as f:
        ADMIN_FEEDS = yaml.safe_load(f).get("admin_feeds", [])
except Exception:
    ADMIN_FEEDS = []
    
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
        self._keyword_global_matches = {}  # key=chat_id → {kw: [(eid,e,f,url)]}
        self._keyword_seen_global = {}     # key=chat_id → set(eid)
        self._admin_seen_cache: dict[tuple[int,str], set] = {}  # key = (chat_id, feed_url)

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

    def _get_seen_safe(self, cid_int: int, url: str) -> set:
        """
        خواندن seen برای یک feed به‌صورت امن:
        - اگر url در ADMIN_FEEDS باشد و کاربر آن را در list_feeds نداشته باشد،
          از کش محلی استفاده کن (تا به دیتابیس فید اضافه نشود).
        - در غیر این صورت از store.get_seen استفاده کن.
        """
        try:
            user_has = url in set(self.store.list_feeds(cid_int))
        except Exception:
            user_has = False

        if url in ADMIN_FEEDS and not user_has:
            return set(self._admin_seen_cache.get((cid_int, url), set()))
        try:
            return set(self.store.get_seen(cid_int, url))
        except Exception:
            return set()

    def _set_seen_safe(self, cid_int: int, url: str, seen: set) -> None:
        """
        نوشتن seen به‌صورت امن (مشابه توضیح بالا).
        """
        try:
            user_has = url in set(self.store.list_feeds(cid_int))
        except Exception:
            user_has = False

        if url in ADMIN_FEEDS and not user_has:
            # فقط داخل کش محلی نگهدار تا به دیتابیس کاربر اضافه نشه
            self._admin_seen_cache[(cid_int, url)] = set(seen)
        else:
            try:
                self.store.set_seen(cid_int, url, seen)
            except Exception:
                LOG.debug("set_seen failed for %s (cid=%s)", url, cid_int, exc_info=True)



    async def _collect_matches_from_feed(self, f, url: str, cid_int: int, keywords: List[str]):
        """
        فقط برای admin-feeds (یا هر فیدی که می‌خواهیم صرفاً برای keyword scan بخوانیم).
        آیتم‌هایی که با keywords منطبقند را در self._keyword_global_matches[cid] جمع می‌کند.
        (این تابع تغییری در seen دیتابیس ایجاد نمی‌کند؛ ثبت در seen بعد از ارسال aggregate انجام می‌شود.)
        """
        if not f or not getattr(f, "entries", None):
            return

        if cid_int not in self._keyword_global_matches:
            self._keyword_global_matches[cid_int] = {}
            self._keyword_seen_global[cid_int] = set()

        global_kw = self._keyword_global_matches[cid_int]
        seen_global = self._keyword_seen_global[cid_int]

        cap = int(getattr(settings, "rss_max_items_per_feed", 10))
        entries = (getattr(f, "entries", []) or [])[:cap]

        for e in entries:
            eid = self.entry_id(e) if "trends.google.com" not in url else f"trend:{(getattr(e,'title','') or '').strip()}"
            if not eid or eid in seen_global:
                continue

            # Skip if DB already marked seen for this feed (avoid collecting duplicates)
            try:
                db_seen = self._get_seen_safe(cid_int, url)
                if eid in db_seen:
                    seen_global.add(eid)
                    continue
            except Exception:
                db_seen = set()

            title = getattr(e, "title", "") or ""
            desc = getattr(e, "summary", "") or getattr(e, "description", "") or ""
            text = f"{title}\n{desc}"

            for k in keywords:
                if re.search(rf"(?<!\w){re.escape(k)}(?!\w)", text, re.IGNORECASE):
                    global_kw.setdefault(k, []).append((eid, e, f, url))
                    seen_global.add(eid)
                    # mark seen in safe storage (for admin feeds goes to admin cache, for user feeds to store)
                    try:
                        db_seen.add(eid)
                        self._set_seen_safe(cid_int, url, db_seen)
                    except Exception:
                        LOG.debug("failed to set admin cache seen for %s (cid=%s)", url, cid_int, exc_info=True)
                    break


    async def _process_feed(self, app: Application, cid_int: int, url: str, f, chat_lang: str, reporter):
        """
        پردازش یک فید (فید از قبل با _fetch_feed گرفته شده و پارس شده).
        این تابع مسئول ارسال پیام‌ها، آپدیت seen و reporter/stat است.
        """
        # ⏩ چک کن ببین هنوز feed برای این کاربر هست یا نه
        current_feeds = set(self.store.list_feeds(cid_int))
        keywords_exist = bool(self.store.list_keywords(cid_int))


        # ✅ اگر فید در دیتابیس نیست و نه در admin_feeds است و نه keyword داریم → skip
        if url not in current_feeds and url not in ADMIN_FEEDS and not keywords_exist:
            LOG.info("⏩ skipping %s for chat=%s because feed was removed", url, cid_int)
            return


        try:
            
            keywords = [k["keyword"].lower() for k in self.store.list_keywords(cid_int)]
            seen = set(self.store.get_seen(cid_int, url))
            cap = int(getattr(settings, "rss_max_items_per_feed", 10))

            # build list of new entries ONCE (fix: avoid nested reinit bug)
            # build list of new entries ONCE (fix: avoid nested reinit bug)
            cap = int(getattr(settings, "rss_max_items_per_feed", 10))
            entries = (getattr(f, "entries", []) or [])[:cap]
            new_entries: List[Tuple[str, object]] = []
            for e in entries:
                eid = self.entry_id(e) if "trends.google.com" not in url else f"trend:{(getattr(e,'title','') or '').strip()}"
                if not eid:
                    continue
                # check DB/cached seen safely
                seen_db = self._get_seen_safe(cid_int, url)
                if eid in seen_db:
                    continue
                new_entries.append((eid, e))

            # اگر keywords تعریف شده — موارد مرتبط را در cache جمع کن (برای ارسال aggregate بعداً)
            if keywords:
                if cid_int not in self._keyword_global_matches:
                    self._keyword_global_matches[cid_int] = {}
                    self._keyword_seen_global[cid_int] = set()

                global_kw = self._keyword_global_matches[cid_int]
                seen_global = self._keyword_seen_global[cid_int]

                is_admin_feed = url in ADMIN_FEEDS
                is_user_feed = url in current_feeds

                for eid, e in new_entries:
                    if eid in seen_global:
                        continue

                    title = getattr(e, "title", "") or ""
                    desc = getattr(e, "summary", "") or getattr(e, "description", "") or ""
                    text = f"{title}\n{desc}"

                    # check against keywords (use re.escape to be safe)
                    for k in keywords:
                        if re.search(rf"(?<!\w){re.escape(k)}(?!\w)", text, re.IGNORECASE):
                            # also skip if DB already has this entry marked seen (avoid duplicates)
                            db_seen = self._get_seen_safe(cid_int, url)
                            if eid in db_seen:
                                seen_global.add(eid)
                                break
                            global_kw.setdefault(k, []).append((eid, e, f, url))
                            seen_global.add(eid)
                            break

                # If this is an admin feed — collect only, do not continue to send per-entry messages
                if is_admin_feed and not is_user_feed:
                    return


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
            
            # RemoteOK
            if "remoteok.com/api" in url or "remoteok.com" in url:
                html = await remoteok.process_remoteok(f, self.store, cid_int, url)
                if html:
                    await app.bot.send_message(
                        chat_id=cid_int,
                        text=html,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    self.stats["sent"] += 1
                    if reporter:
                        try:
                            reporter.record(url, "sent")
                        except Exception:
                            pass
                return

            # --- provider check (custom providers like gold/news) ---            
            for matcher, fn in PROVIDERS:
                try:
                    if matcher(url):
                        res = await fn(self.store, cid_int, url, chat_lang)

                        if res:
                            await app.bot.send_message(
                                chat_id=cid_int,
                                text=res,
                                parse_mode="Markdown",
                                disable_web_page_preview=True,
                            )
                            self.stats["sent"] += 1
                        return
                except Exception:
                    LOG.exception("provider processing failed for %s (cid=%s)", url, cid_int)
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
                        raw_content = ""
                        if getattr(e, "summary", None):
                            raw_content = e.summary
                        elif getattr(e, "description", None):
                            raw_content = e.description
                        elif getattr(e, "content", None):
                            c = e.content
                            if isinstance(c, list) and c:
                                raw_content = c[0].get("value", "")
                            elif isinstance(c, str):
                                raw_content = c     
                        # msg = render_title_only(title_text, feed_title, date, link, lang=chat_lang, translate_fn=_summary_translate,)
                        msg = render_title_only(title_text, feed_title, date, link, lang=chat_lang, content=raw_content)
                       
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

            # --- آماده سازی فیدهای کاربر و کاندیدهای ادمین (ادمین فقط برای اسکن کی‌ورد) ---
            user_feeds: list[str] = list(self.store.list_feeds(cid_int))
            keywords = [k["keyword"].lower() for k in self.store.list_keywords(cid_int)]
            admin_candidates: list[str] = ADMIN_FEEDS.copy() if (keywords and ADMIN_FEEDS) else []

            # اگر نه فید کاربر داریم و نه کی‌ورد، رد شو
            if not user_feeds and not keywords:
                continue

            # ترتیب و cursor فقط روی user_feeds اعمال می‌شود
            user_feeds = sorted(user_feeds)
            start = self._cursor_per_chat.get(cid_int, 0)
            batch_size = int(getattr(settings, "rss_batch_size", 20))
            if start >= len(user_feeds):
                start = 0
            end = min(len(user_feeds), start + batch_size)
            batch_user = user_feeds[start:end]
            next_index = end if end < len(user_feeds) else 0
            self._cursor_per_chat[cid_int] = next_index

            LOG.info("Polling chat=%s user_feeds_total=%d batch=%d (start=%d next=%d) admin_candidates=%d",
                     cid_int, len(user_feeds), len(batch_user), start, next_index, len(admin_candidates))

            # concurrency limiter
            concurrency = int(getattr(settings, "rss_fetch_concurrency", 6))
            sem = asyncio.Semaphore(concurrency)

            async def _fetch_with_sem(u):
                async with sem:
                    try:
                        return await self._fetch_feed(u)
                    except Exception as ex:
                        LOG.debug("fetch failed for %s: %s", u, ex)
                        return None

            # fetch user feeds (برای پردازش عادی)
            fetch_tasks = [asyncio.create_task(_fetch_with_sem(u)) for u in batch_user]
            results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

            proc_tasks = []
            current_feeds = set(self.store.list_feeds(cid_int))

            # پردازش فیدهای کاربر:
            # - اگر parse شد: _process_feed با f
            # - اگر parse نشد: چک کن providerها را؛ اگر one matches -> call _process_feed(..., None)
            for url, res in zip(batch_user, results):
                if isinstance(res, Exception) or not res:
                    # ممکنه فید نباشه؛ بررسی provider ها (مثال: custom providers مثل vipgold)
                    matched = False
                    for matcher, fn in PROVIDERS:
                        try:
                            if matcher(url):
                                proc_tasks.append(asyncio.create_task(self._process_feed(app, cid_int, url, None, chat_lang, reporter)))
                                matched = True
                                break
                        except Exception:
                            LOG.exception("provider matcher failed for %s", url)
                    if not matched:
                        LOG.debug("No feed parsed and no provider matched for %s (chat=%s): %s", url, cid_int, res)
                    continue

                # normal processing for user feeds (this will both send messages and collect keyword matches for user feeds)
                proc_tasks.append(asyncio.create_task(self._process_feed(app, cid_int, url, res, chat_lang, reporter)))

            # --- اگر کی‌ورد هست، اسکن ادمین‌ها برای کی‌وردها (فقط جمع‌آوری matches، نه ارسال per-entry) ---
            admin_scan_tasks = []
            if keywords and admin_candidates:
                admin_fetch_tasks = [asyncio.create_task(_fetch_with_sem(u)) for u in admin_candidates]
                admin_results = await asyncio.gather(*admin_fetch_tasks, return_exceptions=True)
                for url, res in zip(admin_candidates, admin_results):
                    if isinstance(res, Exception) or not res:
                        LOG.debug("No admin feed parsed for %s (chat=%s): %s", url, cid_int, res)
                        continue
                    # collect matches from admin feeds (do NOT call _process_feed on them)
                    admin_scan_tasks.append(asyncio.create_task(self._collect_matches_from_feed(res, url, cid_int, keywords)))

            # منتظر بمون که همه پردازش‌های فید کاربر تموم بشن
            if proc_tasks:
                await asyncio.gather(*proc_tasks, return_exceptions=True)

            # منتظر بمون که اسکن ادمین‌ها تموم شه
            if admin_scan_tasks:
                await asyncio.gather(*admin_scan_tasks, return_exceptions=True)

            # --- ارسال نهایی همه‌ی نتایج keywordها پس از پردازش همه‌ی فیدها (کاربر + admin scans) ---
            if cid_int in self._keyword_global_matches:
                from bs4 import BeautifulSoup
                
                def is_farsi(text: str) -> bool:
                    return bool(re.search(r"[\u0600-\u06FF]", text))
                
                global_kw = self._keyword_global_matches[cid_int]
                for kw, matches in list(global_kw.items()):
                    if not matches:
                        continue

                    # filter out items already marked seen in DB (to avoid duplicates)
                    filtered = list(matches)

                    if not filtered:
                        continue

                    # دسته‌بندی برای جلوگیری از طول زیاد پیام
                    chunks = [filtered[i:i+10] for i in range(0, len(filtered), 10)]
                    for chunk in chunks:
                        # 🈯️ دو زبانه: بسته به زبان کلیدواژه
                        if is_farsi(kw):
                            header = f"{len(chunk)} نتیجه جدید برای #{kw}\n\n"
                        else:
                            header = f"{len(chunk)} new results for #{kw.capitalize()}\n\n"     
                                               
                        parts = []
                        for i, (eid, e, f, url) in enumerate(chunk, start=1):
                            title = getattr(e, "title", "") or ""
                            link = getattr(e, "link", "") or ""
                            feed_title = getattr(getattr(f, "feed", object()), "title", "") or urlparse(url).netloc
                            date = _fmt_date(e)

                            raw_snippet = getattr(e, "summary", "") or getattr(e, "description", "") or ""
                            clean_snippet = BeautifulSoup(raw_snippet, "html.parser").get_text(" ", strip=True)
                            clean_snippet = re.sub(r"\s+", " ", clean_snippet).strip()[:400]

                            # زمان نسبی انتشار
                            published_dt = None
                            if getattr(e, "published_parsed", None):
                                published_dt = datetime.fromtimestamp(time.mktime(e.published_parsed), tz=timezone.utc)
                            elif getattr(e, "updated_parsed", None):
                                published_dt = datetime.fromtimestamp(time.mktime(e.updated_parsed), tz=timezone.utc)

                            if published_dt:
                                delta = datetime.now(timezone.utc) - published_dt
                                rel_time = humanize.naturaltime(delta).replace("from now", "ago")
                                time_str = f"🕒 {rel_time}"
                            else:
                                time_str = ""
 
                            clean_snippet = BeautifulSoup(raw_snippet, "html.parser").get_text(" ", strip=True)
                            clean_snippet = re.sub(r"\s+", " ", clean_snippet).strip()[:400]  
                                                          
                            if clean_snippet:
                                snippet_part = f"📌 {esc(clean_snippet)}\n"
                            else:
                                snippet_part = ""
                            part = (
                                f"{i}\u20e3 <b>{esc(title)}</b>\n"
                                f"{esc(feed_title)} | {esc(date)}\n\n"
                                f"{snippet_part}"
                                f"🔗 <a href=\"{esc_attr(link)}\">Source</a>   {time_str}\n\n"
                            )
                            parts.append(part)

                        msg = header + "\n".join(parts)
                        try:
                            await app.bot.send_message(
                                chat_id=cid_int,
                                text=msg,
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                            )
                        except Exception:
                            LOG.debug("send keyword aggregate failed for cid=%s kw=%s", cid_int, kw, exc_info=True)

                        # بعد از ارسال، همه‌ی آیتم‌های chunk رو در seen ثبت کن تا دورِ بعدی تکراری نیاد
                        # بعد از ارسال، فقط برای فیدهایی که کاربر واقعاً آنها را دارد در DB ثبت کن.
                        # (تا admin feeds به لیست لینک‌های کاربر اضافه نشوند)
                        for _, e, _, url in chunk:
                            eid = self.entry_id(e)
                            try:
                                seen = self._get_seen_safe(cid_int, url)
                                seen.add(eid)
                                self._set_seen_safe(cid_int, url, seen)
                            except Exception:
                                LOG.debug("failed to persist keyword-seen for %s (cid=%s)", url, cid_int, exc_info=True)



                        self.stats["sent"] += len(chunk)

                # پاک‌سازی بعد از ارسال
                self._keyword_global_matches[cid_int].clear()
                self._keyword_seen_global[cid_int].clear()

        # لاگ کلی آمار
        total = self.stats["sent"] + self.stats["skipped"]
        if total:
            ratio = round(100 * self.stats["skipped"] / total, 1)
            LOG.info("[SUMMARY][STATS] sent=%d skipped=%d (%.1f%%) reasons=%s",
                     self.stats["sent"], self.stats["skipped"], ratio, self.stats["reasons"])
