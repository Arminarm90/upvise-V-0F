#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import re
import logging
import os
from typing import List, Dict, Any, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse
import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("ThereIsAnAIForThat")
logging.basicConfig(level=logging.INFO)

BASE_DOMAIN = "theresanaiforthat.com"
BASE_URL = "https://" + BASE_DOMAIN
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
)
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "15"))
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "2"))
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "10"))


def _escape_md(text: Optional[str]) -> str:
    if not text:
        return ""
    s = str(text).strip()
    # فقط حروف حساس Markdown رو escape کن
    return re.sub(r'([`*_\\\[\]])', r'\\\1', s)


def _num_emoji(i: int) -> str:
    if 1 <= i <= 9:
        return f"{i}\u20E3"
    if i == 10:
        return "🔟"
    return str(i)


async def _fetch_html_with_final(url: str) -> Tuple[str, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/",
        "Cache-Control": "no-cache",
    }
    for attempt in range(HTTP_RETRIES + 1):
        try:
            async with httpx.AsyncClient(
                timeout=HTTP_TIMEOUT,
                headers=headers,
                follow_redirects=True
            ) as client:
                r = await client.get(url)
                r.raise_for_status()
                return r.text, str(r.url)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                logger.warning("403 Forbidden — site blocked the request (attempt %s)", attempt + 1)
            await asyncio.sleep(2)
    return "", url



def _normalize_link(href: str, base: str) -> str:
    if not href:
        return ""
    href = href.strip()
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http"):
        return href
    return urljoin(base, href)


def _extract_ai_item(li: BeautifulSoup, base_url: str) -> Dict[str, Any]:
    """
    استخراج اطلاعات از هر کارت AI شامل:
    - name, desc, task, rating, price
    - لینک اصلی (data-url) و لینک صفحه‌ی TAFT
    """
    name = li.get("data-name", "—")
    desc_tag = li.select_one(".short_desc")
    desc = desc_tag.get_text(strip=True) if desc_tag else "—"
    task = li.get("data-task", "—")
    rating_tag = li.select_one(".average_rating")
    rating = rating_tag.get_text(strip=True) if rating_tag else "—"
    price_tag = li.select_one(".ai_launch_date")
    price = price_tag.get_text(strip=True) if price_tag else "—"

    # لینک اصلی ابزار (از data-url)
    ext_url = li.get("data-url", "").strip()
    if ext_url and not ext_url.startswith("http"):
        ext_url = _normalize_link(ext_url, base_url)

    # لینک صفحه داخلی TAFT (backup)
    link_tag = li.select_one(".ai_link")
    href = link_tag.get("href") if link_tag else ""
    internal_link = _normalize_link(href, base_url)

    # اگر لینک اصلی وجود نداشت، fallback به لینک داخلی
    link = ext_url or internal_link

    return {
        "id": name or link,
        "name": name,
        "desc": desc,
        "task": task,
        "rating": rating,
        "price": price,
        "link": link,
    }


def _collect_ais_from_html(html: str, base_url: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    lis = soup.select("li.li")
    items = []
    seen = set()
    for li in lis:
        try:
            item = _extract_ai_item(li, base_url)
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            items.append(item)
        except Exception:
            logger.exception("ThereIsAnAIForThat: error extracting an AI item")
    return items


async def process_theresanaiforthat(store, cid_int: int, url: str, chat_lang=None) -> str:
    try:
        if not url:
            return ""
        parsed = urlparse(url)
        if not parsed.netloc:
            url = urljoin(BASE_URL, url)
        html, final_url = await _fetch_html_with_final(url)
        if not html:
            return ""
        ais = _collect_ais_from_html(html, base_url=final_url)
        if not ais:
            return ""

        seen_key = f"theresanaiforthat_seen::{final_url}"
        try:
            prev = store.get_seen(cid_int, seen_key) or set()
            if isinstance(prev, (list, tuple)):
                seen_ids = set(map(str, prev))
            elif isinstance(prev, str):
                seen_ids = set(prev.split(","))
            elif isinstance(prev, set):
                seen_ids = set(prev)
            else:
                seen_ids = set(prev)
        except Exception:
            seen_ids = set()

        new_ais = [o for o in ais if str(o.get("id")) not in seen_ids]
        if not new_ais:
            return ""

        latest = new_ais[:MAX_ITEMS]
        for o in latest:
            seen_ids.add(str(o.get("id")))
        try:
            store.set_seen(cid_int, seen_key, seen_ids)
        except Exception:
            logger.warning("ThereIsAnAIForThat: could not persist seen ids")

        header = f"{len(latest)} ابزار جدید در There's An AI For That 🧠\n\n"
        parts: List[str] = []
        for idx, it in enumerate(latest, start=1):
            num_emoji = _num_emoji(idx)
            name = _escape_md(it.get("name") or "—")
            desc = _escape_md(it.get("desc") or "—")
            task = _escape_md(it.get("task") or "—")
            rating = _escape_md(it.get("rating") or "—")
            price = _escape_md(it.get("price") or "—")
            link = it.get("link") or ""
            link_md = link.replace("(", "\\(").replace(")", "\\)")

            lines = [
                f"{num_emoji} *{name}*",
                f"📝 {desc}",
                f"🏷️ {task}",
                f"⭐ {rating}",
                f"💰 {price}",
            ]
            if link:
                lines.append(f"🔗 [مشاهده ابزار اصلی]({link_md})")
            parts.append("\n".join(lines))
        msg = header + "\n\n".join(parts)
        return msg
    except Exception:
        logger.exception("ThereIsAnAIForThat: unexpected error in processing")
        return ""


async def get_theresanaiforthat_offers(store, cid_int: int, url: str, chat_lang=None) -> str:
    return await process_theresanaiforthat(store, cid_int, url, chat_lang)


if __name__ == "__main__":
    async def _test():
        class DummyStore:
            def __init__(self):
                self._d = {}
            def get_seen(self, cid, key):
                return self._d.get((cid, key), set())
            def set_seen(self, cid, key, val):
                self._d[(cid, key)] = set(val)
        store = DummyStore()
        url = "https://theresanaiforthat.com"
        out = await process_theresanaiforthat(store, 1, url)
        print(out or "NO OUTPUT")

    asyncio.run(_test())
