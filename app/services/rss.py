# app/services/rss.py
# -*- coding: utf-8 -*-
from __future__ import annotations

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

from ..utils.text import ensure_scheme, root_url
from .summary import Summarizer
from ..storage.state import StateStore
from ..utils.message_formatter import format_entry, format_article
from ..utils.i18n import get_chat_lang

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
        rss_max_items_per_feed = 10
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

    # ------------------------------------------------------------------ #
    # Main poll
    # ------------------------------------------------------------------ #
    async def poll_once(self, app: Application):
        reporter = app.bot_data.get("reporter")
        for cid, st in self.store.iter_chats():
            try:
                cid_int = int(cid)
            except Exception:
                try:
                    cid_int = int(st.get("chat_id") or 0)
                except Exception:
                    continue

            # زبان چت و تنظیم برای Summarizer
            try:
                chat_lang = get_chat_lang(self.store, cid_int)
                try:
                    self.summarizer.prompt_lang = chat_lang
                except Exception:
                    pass
            except Exception:
                chat_lang = "fa"

            feeds: Iterable[str] = list(st.get("feeds", []))
            for url in feeds:
                url = ensure_scheme(url)
                try:
                    # مسیر RSS
                    f = await self._fetch_feed(url)
                    if "trends.google.com/trending/rss" in url:
                        html = await google_trends.process_google_trends(f, self.store, cid_int, url)
                        if html:
                            await app.bot.send_message(
                                chat_id=cid_int,
                                text=html,
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                            )
                        continue

                    if f and getattr(f, "entries", None):
                        seen = set(self.store.get_seen(cid_int, url))
                        new_entries = []
                        cap = int(getattr(settings, "rss_max_items_per_feed", 10))
                        for e in f.entries[:cap]:
                            if "trends.google.com" in url:
                                eid = f"trend:{getattr(e, 'title', '').strip()}"
                            else:
                                eid = self.entry_id(e)                            
                            if not eid or eid in seen:
                                continue
                            new_entries.append((eid, e))

                        feed_title = getattr(getattr(f, "feed", object()), "title", "") or urlparse(url).netloc
                        html = await format_entry(feed_title, e, self.summarizer, url, lang=chat_lang)
                        for eid, e in reversed(new_entries):
                            html = await format_entry(feed_title, e, self.summarizer, url, lang=chat_lang)
                            if not html or not str(html).strip():
                                reason = "ai_empty_output"
                                self.stats["reasons"][reason] = self.stats["reasons"].get(reason, 0) + 1
                                self.stats["skipped"] += 1
                                continue

                            await app.bot.send_message(
                                chat_id=cid_int,
                                text=html,
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                            )
                            self.stats["sent"] += 1
                            seen.add(eid)

                        self.store.set_seen(cid_int, url, seen)
                        continue  # RSS مسیر کامل شد؛ به URL بعدی برو

                    # --- مسیر Page‑Watch (خلاصه‌ساز یکپارچه) ---
                    page_html = await self._get_html(url)
                    if not page_html:
                        continue

                    listing_limit = int(getattr(settings, "pagewatch_listing_limit", 30))
                    links = self._extract_listing_links(url, page_html, limit=listing_limit)
                    if not links:
                        continue

                    seen = set(self.store.get_seen(cid_int, url))
                    per_cycle = int(getattr(settings, "pagewatch_links_per_cycle", 3))
                    new_links = [u for u in links if u not in seen][:per_cycle]
                    if not new_links:
                        continue

                    feed_title = urlparse(url).netloc or url
                    for link in reversed(new_links):
                        # برای عنوان و متن مقاله
                        title_html = await self._get_html(link)
                        title = self._page_title(title_html, fallback=urlparse(link).path or link)

                        # متن مقاله: اول از fetcher (تمیز و آماده‌ی خلاصه‌سازی)
                        article_text = await fetch_article_text(
                            link, timeout=int(getattr(settings, "fetcher_timeout", 12))
                        )
                        if not article_text:
                            # اگر نشد، حداقل متن خام صفحه را به Summarizer بدهیم تا Lite بسازد
                            try:
                                soup = BeautifulSoup(title_html or "", "html.parser")
                                for tnode in soup(["script", "style", "noscript"]):
                                    tnode.decompose()
                                article_text = (soup.get_text(" ", strip=True) or title).strip()
                            except Exception:
                                article_text = title or link

                        html = await format_article(
                            feed_title=feed_title,
                            title=title,
                            link=link,
                            text=article_text,
                            summarizer=self.summarizer,
                            lang=chat_lang,
                        )
                        if not html or not str(html).strip():
                            reason = "article_empty_output"
                            self.stats["reasons"][reason] = self.stats["reasons"].get(reason, 0) + 1
                            self.stats["skipped"] += 1
                        try:
                            await app.bot.send_message(
                                chat_id=cid_int,
                                text=html,
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                            )
                            self.stats["sent"] += 1
                            seen.add(link)
                        except Exception:
                            LOG.debug("send_message failed for %s", link, exc_info=True)

                    self.store.set_seen(cid_int, url, seen)

                except Exception as ex:
                    LOG.exception("poll_once error for %s: %s", url, ex)

        # ---- لاگ آمار یک‌بار در انتهای poll_once
        total = self.stats["sent"] + self.stats["skipped"]
        if total:
            ratio = round(100 * self.stats["skipped"] / total, 1)
            LOG.info(
                "[SUMMARY][STATS] sent=%d skipped=%d (%.1f%%) reasons=%s",
                self.stats["sent"],
                self.stats["skipped"],
                ratio,
                self.stats["reasons"],
            )
