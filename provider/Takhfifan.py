#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import re
import logging
import os
from typing import List, Dict, Any, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse
from datetime import datetime

import httpx
from bs4 import BeautifulSoup
from persiantools.jdatetime import JalaliDate

logger = logging.getLogger("Takhfifan")
logging.basicConfig(level=logging.INFO)

BASE_DOMAIN = "takhfifan.com"
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
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', s)


def _digits_only(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    s2 = re.sub(r"[^\d]", "", str(s))
    if not s2:
        return None
    try:
        return int(s2)
    except:
        return None


def _fmt_toman(n: Optional[int]) -> str:
    if not n:
        return "—"
    return f"{n:,}".replace(",", "،")


def _num_emoji(i: int) -> str:
    if 1 <= i <= 9:
        return f"{i}\u20E3"
    if i == 10:
        return "🔟"
    return str(i)


def _get_jalali_date_str() -> str:
    today = JalaliDate.today()
    months_fa = [
        "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
        "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"
    ]
    return f"{today.day} {months_fa[today.month - 1]} {today.year}"


async def _fetch_html_with_final(url: str) -> Tuple[str, str]:
    """
    Returns (html_text, final_url).
    Ensures we return the exact page we were redirected to (so base path is accurate).
    But avoids false redirects (e.g., to homepage).
    """
    last_exc = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            headers = {"User-Agent": USER_AGENT, "Referer": url}
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers, follow_redirects=True) as client:
                r = await client.get(url)
                r.raise_for_status()

                final = str(r.url) if r.url else url
                # اگر به صفحه اصلی یا مسیر خیلی کوتاه ریدایرکت شده، همون URL اولیه رو نگه دار
                parsed_final = urlparse(final)
                if parsed_final.netloc.endswith(BASE_DOMAIN):
                    bad_paths = ["", "/", "/fa", "/en", "/offers", "/offers/"]
                    # مسیرهای خیلی کلی یا بدون دسته‌بندی
                    if any(parsed_final.path.rstrip("/") == bp.rstrip("/") for bp in bad_paths):
                        final = url  # یعنی برگشت به صفحه اصلی یا دسته‌ی عمومی

                return r.text or "", final
        except Exception as e:
            last_exc = e
            await asyncio.sleep(min(2 ** attempt, 2))
    logger.exception("Takhfifan: fetch failed for %s", url, exc_info=last_exc)
    return "", url



def _normalize_link(href: str, base: str) -> str:
    if not href:
        return ""
    href = href.strip()
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http"):
        return href
    # use urljoin with provided base (final_url)
    return urljoin(base, href)


def _anchor_contains_price(a) -> bool:
    if a.find("del"):
        return True
    price_selectors = [".price", ".amount", ".new-price", ".price--now", ".price-now", ".price-current"]
    for sel in price_selectors:
        if a.select_one(sel):
            return True
    txt = a.get_text(" ", strip=True)
    if "تومان" in txt or "تومن" in txt:
        return True
    if re.search(r"[\d،,]{3,}", txt):
        nums = re.findall(r"[\d،,]{3,}", txt)
        for n in nums:
            if len(re.sub(r"[^\d]", "", n)) >= 4:
                return True
    return False


def _choose_listing_scopes(soup: BeautifulSoup) -> List[BeautifulSoup]:
    """
    برای تخفیفان، کارت‌ها داخل div.vendor-card-box هستند
    """
    scopes = soup.find_all("div", class_="vendor-card-box")
    if not scopes:
        scopes = [soup]
    return scopes

def _find_offer_anchors(soup: BeautifulSoup, base_path: str, base_url: str) -> List[Any]:
    """
    برای ساختار جدید تخفیفان (vendor-card-box)
    """
    anchors = []
    scopes = _choose_listing_scopes(soup)
    for scope in scopes:
        a_tag = scope.find("a", href=True)
        if not a_tag:
            continue

        href = a_tag["href"].strip()
        full = _normalize_link(href, base_url)

        # فقط vendorها رو بگیر، نه دسته‌بندی‌های کلی
        if not re.search(r"/vendor/\d+", href):
            continue

        anchors.append((full, scope))  # کل کارت رو برای استخراج بعدی می‌فرستیم

    # حذف تکراری‌ها
    uniq = {}
    for full, box in anchors:
        title_tag = box.find("p", class_="vendor-card-box__title-text")
        title = title_tag.get_text(strip=True) if title_tag else ""
        key = (full, title)
        if key not in uniq:
            uniq[key] = box
    return list(uniq.items())


def _extract_from_anchor(card: BeautifulSoup, base_url: str) -> Dict[str, Any]:
    """
    استخراج داده از کارت‌های جدید تخفیفان
    """
    link_tag = card.find("a", href=True)
    link = _normalize_link(link_tag["href"], base_url) if link_tag else ""

    # 🎭 عنوان پیشنهاد
    title_tag = card.find("p", class_="vendor-card-box__title-text")
    title = title_tag.get_text(strip=True) if title_tag else "بدون عنوان"

    # 📍 موقعیت (ممکنه وجود نداشته باشه)
    location_tag = card.find("div", class_="vendor-card-box__location")
    location = location_tag.get_text(strip=True) if location_tag else "—"

    # 💥 درصد تخفیف (ساختار جدید و قدیمی هر دو)
    discount = None

    # حالت جدید: vendor-card-box__percent-container
    percent_tag = card.select_one("div.vendor-card-box__percent-container span:nth-of-type(2)")
    if percent_tag:
        m = re.search(r"(\d+)", percent_tag.get_text(strip=True))
        if m:
            discount = int(m.group(1))
    else:
        # حالت قدیمی برای پشتیبانی عقب
        discount_tag = card.find("div", class_="vendor-card-box__offcb-percentage") \
            or card.find("div", class_="vendor-card-box__discount") \
            or card.find("span", class_="badge__off-percent")
        if discount_tag:
            m = re.search(r"(\d+)", discount_tag.get_text(strip=True))
            if m:
                discount = int(m.group(1))


    # ⭐ امتیاز (مثلاً "4.5 (12 نظر)")
    rating = None
    votes = None

    # rate-badge__rate-value → عدد امتیاز (مثل 4.3)
    rating_tag = card.find("p", class_="rate-badge__rate-value")
    if rating_tag:
        m = re.search(r"([\d\.]+)", rating_tag.get_text(strip=True))
        if m:
            rating = float(m.group(1))

    # rate-badge__rate-count → تعداد نظرها، مثلاً (14)
    votes_tag = card.find("p", class_="rate-badge__rate-count")
    if votes_tag:
        m = re.search(r"(\d+)", votes_tag.get_text(strip=True))
        if m:
            votes = int(m.group(1))




    # 💬 تعداد نظر یا رأی
    votes = None
    votes_tag = card.find("div", class_="vendor-card-box__rating-count")
    if votes_tag:
        m = re.search(r"(\d+)", votes_tag.get_text(strip=True))
        if m:
            votes = int(m.group(1))

    # 🛒 تعداد خرید (vendor-card-box__purchase-count)
    purchase_count = None
    buy_tag = card.find("div", class_="vendor-card-box__purchase-count")
    if buy_tag:
        m = re.search(r"(\d+)", buy_tag.get_text(strip=True))
        if m:
            purchase_count = int(m.group(1))

    # 🖼 تصویر
    img_tag = card.find("img")
    img = None
    if img_tag:
        img = (
            img_tag.get("src")
            or img_tag.get("data-src")
            or img_tag.get("data-original")
        )
        if img and img.startswith("//"):
            img = "https:" + img

    # ❌ قیمت در کارت‌ها نیست — فقط در صفحه vendor
    selling_price = None
    rrp_price = None
    stock = "—"

    # 🆔 شناسه یا اسلاگ
    sku = None
    try:
        parsed = urlparse(link)
        seg = parsed.path.rstrip("/").split("/")[-1]
        if seg:
            sku = seg
    except:
        sku = link or title or None
    unique_id = sku or link or title

    return {
        "id": str(unique_id),
        "title": title,
        "location": location,
        "link": link,
        "img": img or "",
        "discount": discount,
        "rating": rating,
        "votes": votes,
        "purchase_count": purchase_count,
        "selling_price": selling_price,
        "rrp_price": rrp_price,
        "stock": stock,
    }


def _collect_offers_from_html(html: str, base_url: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    base_path = urlparse(base_url).path.rstrip("/")
    anchors = _find_offer_anchors(soup, base_path, base_url)
    items: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for full, a in anchors:
        try:
            item = _extract_from_anchor(a, base_url)
            # prefer item's absolute link as id
            if not item.get("id"):
                item["id"] = (full or item.get("title") or "")[:200]
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            items.append(item)
        except Exception:
            logger.exception("Takhfifan: error extracting an offer")
    return items


async def process_takhfifan(store, cid_int: int, url: str, chat_lang=None) -> str:
    """
    Compatible signature: (store, cid_int, url, chat_lang)
    IMPORTANT: this function will scrape exactly the URL passed by the user and use
    the final redirected URL as base for link normalization.
    """
    try:
        if not url:
            return ""
        parsed = urlparse(url)
        if not parsed.netloc:
            url = urljoin(BASE_URL, url)

        html, final_url = await _fetch_html_with_final(url)
        if not html:
            return ""

        # use final_url (after redirects) as base for path decisions
        offers = _collect_offers_from_html(html, base_url=final_url)
        if not offers:
            return ""

        seen_key = f"takhfifan_seen::{final_url}"
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

        new_offers = [o for o in offers if str(o.get("id")) not in seen_ids]
        if not new_offers:
            return ""

        latest = new_offers[:MAX_ITEMS]

        for o in latest:
            try:
                seen_ids.add(str(o.get("id")))
            except:
                pass
        try:
            store.set_seen(cid_int, seen_key, seen_ids)
        except Exception:
            logger.warning("Takhfifan: could not persist seen ids")

        header = f"{len(latest)} پیشنهاد جدید در تخفیفان\n\n"
        today = _get_jalali_date_str()
        parts: List[str] = []
        for idx, it in enumerate(latest, start=1):
            num_emoji = _num_emoji(idx)
            title = _escape_md(it.get("title") or "بدون عنوان")
            brand = _escape_md(it.get("brand") or "—")
            link = it.get("link") or ""
            link_md = link.replace("(", "\\(").replace(")", "\\)")
            selling = it.get("selling_price")
            rrp = it.get("rrp_price")
            disc = it.get("discount")
            # stock = it.get("stock", "—")
            location = _escape_md(it.get("location") or "—")
            rating = it.get("rating")
            votes = it.get("votes")
            buy_count = it.get("purchase_count")
            
            lines = [
                f"{num_emoji} *{title}*",
                f"_{_escape_md('تخفیفان')} | {today}_",
                "",
                f"💥 تخفیف: {disc}%" + "  " + f"📍{location}",
            ]

            if rating:
                lines.append(f"⭐ امتیاز: {rating}")
            if buy_count:
                lines.append(f"🛒 خرید: {buy_count}")
                
            # lines.append(f"{_escape_md(stock)}")


            if link:
                lines.append(f"🔗 [مشاهده پیشنهاد]({link_md})")

            parts.append("\n".join(lines))

        msg = header + "\n\n".join(parts)
        return msg

    except Exception:
        logger.exception("Takhfifan: unexpected error in processing")
        return ""


# alias for provider registry
async def get_takhfifan_offers(store, cid_int: int, url: str, chat_lang=None) -> str:
    return await process_takhfifan(store, cid_int, url, chat_lang)


# quick local test
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
        url = "https://takhfifan.com/mashhad/restaurants-cafes/breakfast"
        out = await process_takhfifan(store, 1, url, None)
        print(out or "NO OUTPUT")
    asyncio.run(_test())
