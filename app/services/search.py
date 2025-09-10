# app/services/search.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import re
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from ddgs import DDGS
# from duckduckgo_search import DDGS


from ..utils.text import root_url, ensure_scheme


class SearchService:
    """
    جستجو/کشف سایت‌ها و فیدهای RSS.

    - sites_by_specialty(q, lang): جستجوی وب برای یافتن سایت‌های مرتبط با یک موضوع (Serper → DDG).
    - discover_rss(site_url): کشف فید RSS/Atom برای یک سایت (چندمرحله‌ای).
    """

    # پارامترهای پیش‌فرض کشف RSS
    _DISC_TIMEOUT = 8          # ثانیه
    _DISC_MAX_BYTES = 131072   # 128KB حداکثر بایت برای بررسی محتوای فید
    _DISC_MAX_REDIRECTS = 3
    _UA = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
    _GUESS_PATHS: List[str] = [
        "/feed",
        "/rss",
        "/rss.xml",
        "/atom.xml",
        "/feed.xml",
        "/index.xml",
        "/blog/feed",
        "/news/feed",
        "/posts/index.xml",
    ]

    def __init__(self, serper_key: Optional[str], default_lang: str = "fa"):
        self.serper_key = (serper_key or "").strip()
        self.default_lang = (default_lang or "fa").lower()
        self.endpoint = "https://google.serper.dev/search"

    async def search(self, query: str, max_results: int = 3) -> List[dict]:
        """
        جستجوی ساده برای متن (برای fallback مرحله دوم).
        خروجی: لیست دیکشنری‌ها با کلیدهای link/title/snippet
        """
        if not query:
            return []

        # اولویت ۱ → Serper API (اگر کلید داری)
        if self.serper_key:
            try:
                async with httpx.AsyncClient(
                    timeout=self._DISC_TIMEOUT,
                    headers={"X-API-KEY": self.serper_key, "User-Agent": self._UA},
                ) as c:
                    payload = {"q": query, "num": max_results, "hl": "en", "gl": "us"}
                    r = await c.post(self.endpoint, json=payload)
                    if r.status_code == 200:
                        data = r.json()
                        organic = data.get("organic", []) or []
                        out = []
                        for item in organic[:max_results]:
                            out.append({
                                "link": item.get("link"),
                                "title": item.get("title"),
                                "snippet": item.get("snippet", ""),
                            })
                        return out
            except Exception as ex:
                print("⚠️ serper search failed:", ex)

        # اولویت ۲ → DuckDuckGo fallback
        try:
            def _do():
                with DDGS() as ddgs:
                    return list(ddgs.text(query, region="us-en", safesearch="moderate", max_results=max_results))

            res = await asyncio.to_thread(_do)
            out = []
            for r in res:
                out.append({
                    "link": r.get("href") or r.get("link") or r.get("url"),
                    "title": r.get("title") or "",
                    "snippet": r.get("body") or "",
                })
            return out
        except Exception as ex:
            print("⚠️ ddg search failed:", ex)
            return []

    # --------------------------------------------------------------------- #
    # جستجوی موضوعی سایت‌ها (برای استفاده‌های آتی / سازگار با نسخهٔ قبل)
    # --------------------------------------------------------------------- #
    async def sites_by_specialty(self, q: str, lang: Optional[str] = None) -> List[str]:
        """
        بر اساس کوئری موضوعی، فهرستی از URL های سایت‌ها را برمی‌گرداند.
        تلاش می‌کند ابتدا از Serper (اگر کلید باشد) و در غیر اینصورت از DuckDuckGo استفاده کند.
        """
        lang = (lang or self.default_lang or "fa").lower()
        urls: List[str] = []

        # 1) Serper (اگر کلید وجود داشته باشد)
        if self.serper_key:
            try:
                async with httpx.AsyncClient(
                    timeout=self._DISC_TIMEOUT,
                    headers={"X-API-KEY": self.serper_key, "User-Agent": self._UA},
                    follow_redirects=True,
                    max_redirects=self._DISC_MAX_REDIRECTS,
                ) as c:
                    # مستندات Serper (google.serper.dev/search)
                    payload = {"q": q, "num": 20, "hl": lang, "gl": "us"}
                    r = await c.post("https://google.serper.dev/search", json=payload)
                    if r.status_code == 200:
                        data = r.json()
                        organic = data.get("organic", []) or []
                        for item in organic:
                            link = item.get("link") or item.get("url")
                            if link:
                                urls.append(link)
            except Exception:
                # در صورت خطا، به fallback می‌رویم
                pass

        # 2) DuckDuckGo fallback
        if not urls:
            def _do():
                with DDGS() as ddgs:
                    # region و max_results قابل تنظیم؛ در اینجا تنظیماتی ایمن و عمومی گذاشته شده
                    res = list(ddgs.text(q, region="us-en", safesearch="moderate", max_results=30))
                # استخراج لینک از ساختارهای مختلف نتایج
                return [r.get("href") or r.get("link") or r.get("url") for r in res if r]

            try:
                urls = await asyncio.to_thread(_do)
            except Exception:
                urls = []

        # حذف مقادیر None و پاکسازی ساده
        urls = [u for u in urls if isinstance(u, str) and u.strip()]
        return urls

    # --------------------------------------------------------------------- #
    # کشف RSS برای یک سایت (چندمرحله‌ای): <link rel="alternate"> → مسیرهای حدسی → sitemap
    # --------------------------------------------------------------------- #
    async def discover_rss(self, site_url: str) -> Optional[str]:
        """
        تلاش چندمرحله‌ای برای یافتن فید RSS/Atom یک سایت.
        - ورودی: URL سایت (ممکن است بدون scheme باشد → ensure_scheme)
        - خروجی: بهترین URL فید (str) یا None اگر چیزی یافت نشد.
        """
        site_url = ensure_scheme((site_url or "").strip())
        parsed = urlparse(site_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None

        base = root_url(site_url)
        candidates: List[str] = []

        async with httpx.AsyncClient(
            timeout=self._DISC_TIMEOUT,
            headers={"User-Agent": self._UA},
            follow_redirects=True,
            max_redirects=self._DISC_MAX_REDIRECTS,
        ) as client:
            # Phase A: اسکن لینک‌های alternate در صفحهٔ اصلی
            try:
                html = await self._safe_get_text(client, site_url, limit=self._DISC_MAX_BYTES)
                if html:
                    links = self._find_alternate_links(html, site_url)
                    candidates.extend(links)
            except Exception:
                # ادامه می‌دهیم به مراحل بعدی
                pass

            # Phase B: مسیرهای حدسی روی root
            for path in self._GUESS_PATHS:
                guessed = urljoin(base + "/", path.lstrip("/"))
                candidates.append(guessed)

            # حذف تکراری‌ها با حفظ ترتیب
            seen = set()
            uniq_candidates = []
            for u in candidates:
                if not u or u in seen:
                    continue
                seen.add(u)
                uniq_candidates.append(u)

            # اعتبارسنجی سبک کاندیدها
            valid: List[str] = []
            for u in uniq_candidates:
                try:
                    if await self._looks_like_rss(client, u):
                        valid.append(u)
                except Exception:
                    continue

            if not valid:
                # Phase C: تلاش روی sitemap.xml (اختیاری)
                try:
                    sm_url = urljoin(base + "/", "sitemap.xml")
                    sm = await self._safe_get_text(client, sm_url, limit=self._DISC_MAX_BYTES)
                    if sm:
                        # هر لینکی که در سایت‌مپ به فید اشاره دارد
                        # یک جستجوی سبک بر اساس وجود واژه‌های rss/atom/feed
                        rssish = re.findall(r"https?://[^\s\"<>]*?(rss|atom|feed)[^\s\"<>]*", sm, flags=re.I)
                        # regex بالا گروه می‌گیرد؛ URL کامل را از تطابق کامل استخراج کنیم
                        urlish = re.findall(r"https?://[^\s\"<>]+", sm, flags=re.I)
                        for u in urlish:
                            if re.search(r"(rss|atom|feed)", u, flags=re.I):
                                try:
                                    if await self._looks_like_rss(client, u):
                                        valid.append(u)
                                except Exception:
                                    pass
                except Exception:
                    pass

        if not valid:
            return None

        # انتخاب بهترین فید
        best = self._choose_best_feed(valid, base)
        return best

    # ----------------------------- Helpers -------------------------------- #

    async def _safe_get_text(self, client: httpx.AsyncClient, url: str, limit: int) -> Optional[str]:
        """
        GET سبک که فقط بخشی از محتوا را می‌خواند تا سریع و ایمن باشد.
        """
        try:
            r = await client.get(url, headers={"User-Agent": self._UA})
            if r.is_success:
                # برش محتوا به حداکثر بایت (برای سرعت و امنیت)
                text = r.text
                if len(text) > limit:
                    return text[:limit]
                return text
        except Exception:
            return None
        return None

    def _find_alternate_links(self, html: str, base_url: str) -> List[str]:
        """
        در HTML صفحه، لینک‌های <link rel="alternate" type="application/rss+xml|application/atom+xml"> را پیدا می‌کند.
        """
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return []

        out: List[str] = []
        for link in soup.find_all("link", attrs={"rel": True, "type": True, "href": True}):
            try:
                rel = " ".join(link.get("rel") if isinstance(link.get("rel"), list) else [link.get("rel")]).lower()
                typ = str(link.get("type") or "").lower()
                if "alternate" in rel and typ in ("application/rss+xml", "application/atom+xml", "application/xml"):
                    href = link.get("href")
                    if not href:
                        continue
                    u = urljoin(base_url, href)
                    out.append(u)
            except Exception:
                continue
        return out

    async def _looks_like_rss(self, client: httpx.AsyncClient, url: str) -> bool:
        """
        اعتبارسنجی سبک: بررسی Content-Type و بدنهٔ کوتاه برای وجود نشانه‌های RSS/Atom.
        """
        try:
            r = await client.get(url, headers={"User-Agent": self._UA})
        except Exception:
            return False

        if not r.is_success:
            return False

        ctype = (r.headers.get("Content-Type") or "").lower()
        if any(x in ctype for x in ("application/rss+xml", "application/atom+xml", "application/xml", "text/xml")):
            return True

        # اگر Content-Type عمومی بود، بدنهٔ کوتاه را نگاه می‌کنیم
        body = r.text[: self._DISC_MAX_BYTES] if r.text else ""
        if not body:
            return False

        # نشانه‌های سادهٔ RSS/Atom
        if re.search(r"<rss[\s>]", body, flags=re.I) or re.search(r"<feed[\s>]", body, flags=re.I):
            return True

        return False

    def _choose_best_feed(self, feeds: List[str], base: str) -> str:
        """
        انتخاب «بهترین» فید بر اساس اولویت‌های ساده:
        1) همان دامنهٔ ورودی
        2) مسیرهای عمومی و ریشه‌ای (مثل /feed, /rss.xml)
        3) ترجیح rss بر atom در نام
        """
        if not feeds:
            return ""

        # اول: همان دامنه
        same_domain = []
        other = []
        base_host = urlparse(base).netloc.lower()
        for u in feeds:
            host = urlparse(u).netloc.lower()
            (same_domain if host == base_host or host.endswith("." + base_host) else other).append(u)

        ordered = same_domain + other

        # سپس: امتیازدهی مسیرها
        def score(u: str) -> int:
            path = urlparse(u).path.lower()
            s = 0
            # مسیرهای ریشه‌ای/معروف امتیاز بالاتر
            if path in ("/feed", "/rss", "/rss.xml", "/atom.xml", "/feed.xml", "/index.xml"):
                s += 3
            if "/blog" in path or "/news" in path:
                s += 1
            # ترجیح rss نسبت به atom در نام
            if "rss" in u.lower():
                s += 1
            return s

        ordered.sort(key=score, reverse=True)
        return ordered[0]
    
# Search
class SerperSearch:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.endpoint = "https://google.serper.dev/search"

    async def search(self, query: str, max_results: int = 3):
        headers = {"X-API-KEY": self.api_key, "Content-Type": "application/json"}
        payload = {"q": query}
        async with httpx.AsyncClient() as client:
            resp = await client.post(self.endpoint, headers=headers, json=payload)
            data = resp.json()

        out = []
        for item in data.get("organic", [])[:max_results]:
            out.append({
                "link": item.get("link"),
                "title": item.get("title"),
                "snippet": item.get("snippet", "")
            })
        return out

