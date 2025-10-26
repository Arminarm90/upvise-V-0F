#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Divar Watcher — Jet Project (3 files only)
Files: bot.py, Requirements.txt, .env

ویژگی‌ها:
- دستورات تلگرام: /add /remove /list /interval /help
- پایش دوره‌ای URLهای جست‌وجوی دیوار (search/list pages)
- استخراج آگهی‌های جدید و ارسال به تلگرام
- بدون دیتابیس؛ state فقط در حافظه‌ی اجرا نگه‌داری می‌شود (طبق خواسته شما)
- محدودیت ساده‌ی نرخ درخواست + backoff روی خطاها
- فقط لینک‌های نوع لیست (مسیر /s/...) پذیرفته می‌شوند؛ صفحات تکی (/v/...) رد می‌شوند

به‌روزرسانی‌ها:
- لینک تمیز و هایپرلینک‌شده: 🔗 [لینک آگهی](URL)
- ارسال همهٔ آگهی‌ها در بَچ‌های ۱۰تایی با مکث ۳ ثانیه‌ای بین پیام‌ها (قابل‌تنظیم از .env)
- غیرفعال بودن پیش‌نمایش لینک‌ها
- Fail-safe در صورت خطای MarkdownV2
- منطق جدید قیمت/کارکرد ویژهٔ دستهٔ خودرو:
  * قیمت فقط وقتی پذیرفته می‌شود که حاوی «تومان» یا «قیمت توافقی/توافقی» باشد.
  * رشته‌های شامل «کیلومتر» یا «km» به‌عنوان mileage ذخیره می‌شوند و هرگز به‌جای قیمت قرار نمی‌گیرند.
  * اگر قیمت یافت نشود: «—» نمایش داده می‌شود.
  * اگر کارکرد یافت شود: «کارکرد: ...» در خروجی اضافه می‌شود.

نکتهٔ JobQueue:
  pip install 'python-telegram-bot[job-queue]==21.6'
"""

import asyncio
import os
import re
from typing import Dict, Set, List, Tuple, Iterable
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
import logging

import httpx
from bs4 import BeautifulSoup

# --- بارگذاری .env در ابتدای اجرا ---
from dotenv import load_dotenv
load_dotenv()  # .env در همان پوشه پروژه خوانده می‌شود

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from datetime import datetime
from persiantools.jdatetime import JalaliDate
# --------------------------- Config & Globals ---------------------------

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
DEFAULT_INTERVAL_MIN = int(os.getenv("DEFAULT_INTERVAL_MIN", "3"))
ALLOWED_CHAT_ID = os.getenv("ALLOWED_CHAT_ID", "").strip()  # اختیاری: محدود کردن به یک چت/کانال
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "15"))
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "2"))
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# تنظیمات ارسال
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "10"))
BATCH_PAUSE_SEC = float(os.getenv("BATCH_PAUSE_SEC", "3"))  # مکث بین پیام‌های بَچ

LOG = logging.getLogger("Divar")
# ساختار داده‌ی درون‌حافظه‌ای:
# chats_state = {
#   chat_id: {
#       "feeds": set([url, ...]),
#       "seen": { url: set([ad_id, ...]) },
#       "interval": minutes
#   }
# }
chats_state: Dict[int, Dict] = {}

# برای job‌های زمان‌بندی شده‌ی هر چت:
# jobs[chat_id] = job
jobs: Dict[int, object] = {}

DIVAR_HOST = "divar.ir"

# --------------------------- Utilities ---------------------------

def normalize_url(u: str) -> str:
    """یونیفورم کردن URL (حذف fragment، sort کردن queryها، حذف slash انتهایی طبق نیاز)"""
    u = u.strip()
    parsed = urlparse(u)
    # اگر پروتکل نداشت، https اضافه کن
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc or DIVAR_HOST
    path = parsed.path or "/"
    # حذف fragment
    fragment = ""
    # query مرتب‌شده
    q_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    q_pairs.sort()
    query = urlencode(q_pairs)

    # حذف slash اضافه در انتها مگر اینکه فقط "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]

    normalized = urlunparse((scheme, netloc, path, "", query, fragment))
    return normalized


def is_divar_search_url(u: str) -> bool:
    """فقط صفحات لیست جست‌وجو قابل پایش هستند: دامنه divar.ir و مسیر /s/"""
    try:
        parsed = urlparse(u)
        return (parsed.netloc or "").endswith(DIVAR_HOST) and parsed.path.startswith("/s/")
    except Exception:
        return False


def is_divar_single_ad(u: str) -> bool:
    """صفحه تکی آگهی معمولاً مسیر /v/... دارد."""
    try:
        parsed = urlparse(u)
        return parsed.path.startswith("/v/")
    except Exception:
        return False


def clean_text(txt: str) -> str:
    return re.sub(r"\s+", " ", txt or "").strip()


def parse_price_and_mileage(texts: List[str]) -> Tuple[str, str]:
    """
    از مجموعه‌ای از رشته‌های مرتبط با کارت، قیمت و کارکرد را استخراج می‌کند.
    - قیمت فقط اگر شامل 'تومان' یا 'قیمت توافقی' یا 'توافقی' باشد پذیرفته می‌شود.
    - mileage اگر شامل 'کیلومتر' یا 'km' باشد استخراج می‌شود.
    خروجی: (price, mileage)
    """
    price = ""
    mileage = ""

    re_mileage = re.compile(r"(?:(?:کارکرد|کیلومتر|km)\s*[:：]?\s*)?([\d,\.]+)\s*(?:کیلومتر|km)", re.IGNORECASE)
    re_price = re.compile(r"(?:^|\s)(?:قیمت[:：]?\s*)?([\d,\.]+(?:\s*,\s*[\d\.]+)*)\s*تومان", re.IGNORECASE)
    re_price_tavafoghi = re.compile(r"(?:قیمت\s*)?(?:توافقی|قیمت توافقی)", re.IGNORECASE)

    for raw in texts:
        t = clean_text(raw)

        if not mileage:
            m = re_mileage.search(t)
            if m:
                val = m.group(1)
                mileage = f"{val} کیلومتر"

        if not price:
            m2 = re_price.search(t)
            if m2:
                num = m2.group(1)
                price = f"{num} تومان"

        if not price and re_price_tavafoghi.search(t):
            price = "قیمت توافقی"

    return price, mileage


def extract_ads_from_html(html: str, base_url: str) -> List[Dict]:
    """
    تلاش مقاوم برای استخراج آگهی‌ها از HTML نتایج.
    - آگهی‌ها از لینک‌های /v/ شناسایی می‌شوند.
    - title/price/location/mileage با best-effort استخراج می‌گردد.
    """
    soup = BeautifulSoup(html, "lxml")

    ads = []
    seen_href: Set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("/v/"):
            continue
        if href in seen_href:
            continue
        seen_href.add(href)

        ad_id = href.split("/v/", 1)[-1].split("?")[0].strip("/")

        # کارت والد
        card = a
        for _ in range(4):
            if card and (card.name in ("article", "div", "li")):
                break
            card = card.parent

        title = None
        price = ""
        location = None
        mileage = ""

        if a.get_text(strip=True):
            title = a.get_text(strip=True)

        texts_for_parse: List[str] = []
        if card:
            for selector in ["h2", "h3", "h4", ".kt-post-card__title", "[data-test-id='title']"]:
                el = card.select_one(selector)
                if el and el.get_text(strip=True):
                    title = el.get_text(strip=True)
                    break

            for selector in [
                ".kt-post-card__description",
                ".kt-post-card__price",
                ".kt-post-card__bottom-description",
                "[data-test-id='price']",
                "[data-test-id='location']",
                "[class*='post-card']",
            ]:
                for el in card.select(selector):
                    txt = el.get_text(" ", strip=True)
                    if txt:
                        texts_for_parse.append(txt)

        if texts_for_parse:
            p, m = parse_price_and_mileage(texts_for_parse)
            if p:
                price = p
            if m:
                mileage = m

            for t in texts_for_parse:
                tt = clean_text(t)
                if ("کیلومتر" in tt) or ("km" in tt.lower()) or ("تومان" in tt) or ("قیمت" in tt):
                    continue
                if "در " in tt or "محله" in tt or "منطقه" in tt:
                    location = tt
                    break

        parsed_base = urlparse(base_url)
        full_url = urlunparse((parsed_base.scheme, parsed_base.netloc, href, "", "", ""))

        ads.append(
            {
                "id": ad_id or href,
                "url": full_url,
                "title": title or "بدون عنوان",
                "price": price or "—",
                "location": location or "—",
                "mileage": mileage or "",
            }
        )

    return ads


async def fetch_search_page(client: httpx.AsyncClient, url: str) -> Tuple[str, int]:
    """دریافت HTML صفحه با backoff ساده؛ خروجی: (متن، status_code)"""
    last_exc = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            resp = await client.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8"},
                timeout=HTTP_TIMEOUT,
            )
            return resp.text, resp.status_code
        except Exception as e:
            last_exc = e
            await asyncio.sleep(min(2 ** attempt, 5))
    raise last_exc if last_exc else RuntimeError("Fetch failed")


def ensure_chat(chat_id: int):
    if chat_id not in chats_state:
        chats_state[chat_id] = {"feeds": set(), "seen": {}, "interval": DEFAULT_INTERVAL_MIN}


def get_job_for_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    return jobs.get(chat_id)


def schedule_or_update_job(application, chat_id: int):
    job = jobs.get(chat_id)
    if job:
        job.schedule_removal()

    interval_min = max(1, int(chats_state[chat_id]["interval"]))

    if application.job_queue is None:
        raise RuntimeError(
            "JobQueue در دسترس نیست. نصب کنید: pip install 'python-telegram-bot[job-queue]==21.6'"
        )

    job = application.job_queue.run_repeating(
        callback=poll_chat_feeds,
        interval=interval_min * 60,
        first=5,
        data={"chat_id": chat_id},
        name=f"poll_{chat_id}",
    )
    jobs[chat_id] = job


# --------------------------- Telegram Handlers ---------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    ensure_chat(update.effective_chat.id)
    text = (
        "سلام! 👋\n"
        "من ربات پایش آگهی‌های دیوار هستم.\n\n"
        "دستورها:\n"
        "/add <url> – اضافه کردن لینک جست‌وجوی دیوار (نوع /s/)\n"
        "/remove <url> – حذف لینک\n"
        "/list – نمایش لینک‌های در حال پایش\n"
        "/interval <minutes> – تعیین فاصله‌ی پایش (پیش‌فرض: {min} دقیقه)\n"
        "/help – نمایش همین راهنما\n\n"
        "قانون اصلی: فقط لینک‌هایی که در مرورگر «لیست آگهی‌ها» نشان می‌دهند و مسیرشان با /s/ شروع می‌شود قابل پایش هستند."
    ).format(min=DEFAULT_INTERVAL_MIN)
    await update.effective_message.reply_text(text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    await start(update, context)


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    ensure_chat(update.effective_chat.id)

    if not context.args:
        await update.effective_message.reply_text("لطفاً پس از /add لینک جست‌وجوی دیوار را بدهید.")
        return

    raw_url = " ".join(context.args).strip()
    url = normalize_url(raw_url)

    if is_divar_single_ad(url):
        await update.effective_message.reply_text("این لینک یک آگهی تکی (/v/...) است و قابل پایش نیست.")
        return

    if not is_divar_search_url(url):
        await update.effective_message.reply_text("این لینک به نظر نمی‌رسد صفحهٔ نتایج (/s/...) باشد. فقط لینک‌های لیست قابل پایش هستند.")
        return

    chats_state[update.effective_chat.id]["feeds"].add(url)
    chats_state[update.effective_chat.id]["seen"].setdefault(url, set())
    schedule_or_update_job(context.application, update.effective_chat.id)

    await update.effective_message.reply_text(f"✅ اضافه شد:\n{url}")


async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    ensure_chat(update.effective_chat.id)

    if not context.args:
        await update.effective_message.reply_text("لطفاً پس از /remove، دقیقاً همان لینکی را بدهید که قبلاً اضافه کرده‌اید.")
        return

    # ✅ تایپوی قبلی اصلاح شد: " ".join(...)
    raw_url = " ".join(context.args).strip()
    url = normalize_url(raw_url)

    feeds: Set[str] = chats_state[update.effective_chat.id]["feeds"]
    if url in feeds:
        feeds.remove(url)
        chats_state[update.effective_chat.id]["seen"].pop(url, None)
        await update.effective_message.reply_text(f"🗑 حذف شد:\n{url}")
    else:
        await update.effective_message.reply_text("چنین لینکی در لیست پایش نبود.")


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    ensure_chat(update.effective_chat.id)

    feeds: Set[str] = chats_state[update.effective_chat.id]["feeds"]
    if not feeds:
        await update.effective_message.reply_text("هنوز لینکی اضافه نکرده‌اید. با دستور /add شروع کنید.")
        return

    lines = ["🔎 لینک‌های در حال پایش:"]
    for u in sorted(feeds):
        lines.append(f"• {u}")
    await update.effective_message.reply_text("\n".join(lines))


async def interval_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    ensure_chat(update.effective_chat.id)

    if not context.args:
        cur = chats_state[update.effective_chat.id]["interval"]
        await update.effective_message.reply_text(f"فاصله‌ی فعلی پایش: {cur} دقیقه\nبرای تغییر: /interval 5")
        return

    try:
        m = int(context.args[0])
        if m < 1 or m > 60:
            raise ValueError
    except Exception:
        await update.effective_message.reply_text("عدد دقیقه معتبر نیست. بازهٔ مجاز: 1 تا 60.")
        return

    chats_state[update.effective_chat.id]["interval"] = m
    if chats_state[update.effective_chat.id]["feeds"]:
        schedule_or_update_job(context.application, update.effective_chat.id)

    await update.effective_message.reply_text(f"⏱ فاصله‌ی پایش روی {m} دقیقه تنظیم شد.")


# --------------------------- Send helpers ---------------------------

def escape_md(text: str) -> str:
    """Escape ساده برای MarkdownV2 (طبق مستندات تلگرام)"""
    if text is None:
        return ""
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", text)


async def safe_send_markdown(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str):
    """
    تلاش برای ارسال با MarkdownV2؛ اگر BadRequest به خاطر escape رخ داد،
    بدون parse_mode به صورت متن ساده ارسال می‌کند.
    """
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    except BadRequest:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                disable_web_page_preview=True,
            )
        except Exception:
            pass


def chunk(iterable: List[Dict], size: int) -> Iterable[List[Dict]]:
    """تقسیم لیست به تکه‌های size-تایی"""
    for i in range(0, len(iterable), size):
        yield iterable[i:i + size]


def build_ad_block(ad: Dict) -> str:
    """
    بلوک نمایش هر آگهی برای قرار گرفتن در یک پیام دسته‌ای.
    URL به صورت هایپرلینک نمایش داده می‌شود (MarkdownV2).
    """
    mileage_line = ""
    if ad.get("mileage"):
        mileage_line = f"\n  کارکرد: {escape_md(ad['mileage'])}"

    return (
        f"• *{escape_md(ad['title'])}*\n"
        f"  قیمت: {escape_md(ad['price'])}\n"
        f"  مکان: {escape_md(ad['location'])}"
        f"{mileage_line}\n"
        f"  🔗 [لینک آگهی]({ad['url']})"
    )


# --------------------------- Polling Job ---------------------------

async def poll_chat_feeds(context: ContextTypes.DEFAULT_TYPE):
    """Job: هر N دقیقه برای یک چت همه‌ی فیدها را چک می‌کند."""
    job_data = context.job.data or {}
    chat_id = job_data.get("chat_id")
    if chat_id is None or chat_id not in chats_state:
        return

    feeds: Set[str] = chats_state[chat_id]["feeds"]
    if not feeds:
        return

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for url in list(feeds):
            try:
                html, status = await fetch_search_page(client, url)
                if status != 200:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"⚠️ دریافت ناموفق از دیوار ({status}) برای:\n{url}",
                        disable_web_page_preview=True,
                    )
                    continue

                ads = extract_ads_from_html(html, url)
                LOG.info("DIVAR: Extracted %d ads for URL: %s", len(ads), url)
                if not ads:
                    continue

                seen_set: Set[str] = chats_state[chat_id]["seen"].setdefault(url, set())

                new_ads = [ad for ad in ads if ad["id"] not in seen_set]

                for ad in ads[:50]:
                    seen_set.add(ad["id"])

                if not new_ads:
                    continue

                for idx, batch in enumerate(chunk(new_ads, BATCH_SIZE), start=1):
                    header = "📢 *آگهی‌های جدید در دیوار*\n"
                    body = "\n\n".join(build_ad_block(ad) for ad in batch)
                    msg_text = header + body
                    await safe_send_markdown(context, chat_id, msg_text)

                    if idx * BATCH_SIZE < len(new_ads):
                        await asyncio.sleep(BATCH_PAUSE_SEC)

            except Exception as e:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ خطا هنگام پایش:\n{url}\n{type(e).__name__}: {e}",
                    disable_web_page_preview=True,
                )


# --------------------------- Main ---------------------------

async def check_access(update: Update) -> bool:
    """اگر ALLOWED_CHAT_ID تنظیم شده باشد، فقط همان چت مجاز است."""
    if not ALLOWED_CHAT_ID:
        return True
    try:
        allowed = int(ALLOWED_CHAT_ID)
        return update.effective_chat and update.effective_chat.id == allowed
    except Exception:
        return True


def ensure_env():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("متغیر محیطی TELEGRAM_TOKEN در .env تنظیم نشده است.")

async def _get_html(url: str) -> str:
    """Async fetches HTML using Divar's config (timeout, UA)."""
    try:
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT, # استفاده از متغیر سراسری
            headers={"User-Agent": USER_AGENT}, # استفاده از متغیر سراسری
            follow_redirects=True,
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
            # retries=HTTP_RETRIES,
        ) as c:
            r = await c.get(url)
            r.raise_for_status() # این خط اگر کد پاسخ 4xx/5xx باشد، HTTPStatusError صادر می‌کند
            
        if "html" in (r.headers.get("content-type") or "").lower():
            return r.text or ""
        else:
            LOG.warning("DIVAR: Fetched non-HTML content for %s (Status: %d, Type: %s)", 
                        url, r.status_code, r.headers.get("content-type"))
            return ""
            
    except httpx.HTTPStatusError as e:
        # خطای وضعیت HTTP (مثلاً 403 Forbidden یا 404 Not Found)
        LOG.error("DIVAR: HTTP Error %s for URL: %s", e.response.status_code, url)
        # 💡 این معمولاً به معنی مسدود شدن ربات توسط دیوار است.
    except Exception as e:
        # خطاهای دیگر (مانند Timeout، DNS یا Connection Error)
        # 💡 این اغلب به معنی مشکل در شبکه یا Timeout است.
        LOG.error("DIVAR: Fetch Failed for URL: %s. Error Type: %s", url, type(e).__name__, exc_info=True)
        
    return ""

import re
from persiantools.jdatetime import JalaliDate

def _escape_md(text: str) -> str:
    """Escape امن برای MarkdownV2 (کامل برای فارسی و تلگرام)"""
    if not text:
        return ""
    text = str(text)
    # حذف فاصله‌های غیرضروری در ابتدا
    text = text.strip()
    # escape همه‌ی کاراکترهای خاص MarkdownV2
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', text)



def _num_emoji(i: int) -> str:
    """برگرداندن ایموجی عددی 1️⃣ تا 9️⃣ و 🔟 برای 10"""
    if 1 <= i <= 9:
        return f"{i}\u20E3"
    elif i == 10:
        return "🔟"
    else:
        return str(i)


def _get_jalali_date_str() -> str:
    """تاریخ امروز شمسی با نام ماه فارسی"""
    today = JalaliDate.today()
    months_fa = [
        "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
        "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"
    ]
    month_name = months_fa[today.month - 1]
    return f"{today.day} {month_name} {today.year}"


def _parse_divar_url(url: str):
    """
    استخراج نام شهر و دسته‌بندی از URL دیوار
    مثل: https://divar.ir/s/tehran/auto/qeytarieh
    """
    pattern = r"https?://divar\.ir/s/([^/]+)/([^/?#]+)"
    m = re.search(pattern, url)
    if not m:
        return ("", "")
    city = m.group(1)
    category = m.group(2)
    return (city, category)

def _map_divar_category(category: str) -> str:
    """ترجمه‌ی slug دسته‌بندی دیوار به فارسی خوانا"""
    mapping = {
        "auto": "خودرو",
        "car": "خودرو",
        "cars": "خودرو",
        "real-estate": "املاک",
        "mobile-phones": "موبایل و تلفن همراه",
        "electronic-devices": "کالای دیجیتال",
        "home-kitchen": "لوازم خانگی",
        "jobs": "استخدام و کاریابی",
        "personal-goods": "وسایل شخصی",
        "entertainment": "فرهنگی و سرگرمی",
        "services": "خدمات",
        "animals": "حیوانات",
        "tools-materials-equipment": "تجهیزات و صنعتی",
        "social-services": "اجتماعی",
    }
    return mapping.get(category.lower(), category.replace("-", " ").capitalize())


async def process_divar(store, cid_int, url: str, chat_lang) -> str:
    """
    استخراج آگهی‌های جدید از دیوار:
    - فقط آگهی‌های جدید
    - حداکثر ۱۰ عدد
    - همراه با اطلاعات شهر و دسته‌بندی
    """
    if not is_divar_search_url(url):
        LOG.warning("DIVAR: URL validation failed for: %s", url)
        return ""

    html = await _get_html(url)
    if not html:
        return ""

    ads = extract_ads_from_html(html, url)
    if not ads:
        return ""

    # استخراج شهر و دسته‌بندی از URL
    city, category = _parse_divar_url(url)
    city = city.capitalize() if city else "نامشخص"
    category = category.capitalize() if category else "—"

    seen_key = f"divar_seen::{url}"
    seen_ids = set(store.get_seen(cid_int, seen_key) or [])

    new_ads = [ad for ad in ads if ad["id"] not in seen_ids]
    if not new_ads:
        return ""

    latest_new = new_ads[:10]
    today_jalali = _get_jalali_date_str()

    # سرآغاز پیام جدید:
    fa_category = _map_divar_category(category)

    # تعداد آگهی‌های جدید
    count = len(latest_new)
    count_fa = str(count)  # اگر خواستی بعداً اعداد فارسی‌سازی بشن، میشه جدا نوشت

    # دسته‌بندی به شکل هشتگ تمیز (فاصله‌ها به _ تبدیل می‌شن)
    fa_category_tag = "#" + fa_category.replace(" ", "_")

    # پیام بالای خروجی
    header = f"{count_fa} آگهی جدید در دسته‌بندی {fa_category_tag}\n\n"

    lines = []
    for i, ad in enumerate(latest_new, start=1):
        num_emoji = _num_emoji(i)
        title = _escape_md(ad.get("title", "بدون عنوان"))
        price = _escape_md(ad.get("price", "—"))
        loc = _escape_md(ad.get("location", "—"))
        mileage = _escape_md(ad.get("mileage", ""))
        url_md = ad["url"].replace(")", "\\)").replace("(", "\\(")


        part = (
            f"{num_emoji} *{title}*\n"
            f"_{_escape_md('دیوار')} \\| {today_jalali}_\n\n"
            f" قیمت: {price}\n"
            f" مکان: {loc}"
        )
        if mileage:
            part += f"\n  کارکرد: {mileage}"
        part += f"\n🔗 [لینک آگهی]({url_md})"
        lines.append(part)
        seen_ids.add(ad["id"])

    store.set_seen(cid_int, seen_key, seen_ids)

    msg = header + "\n\n".join(lines)
    return msg

# def main():
#     ensure_env()

#     application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

#     application.add_handler(CommandHandler("start", start))
#     application.add_handler(CommandHandler("help", help_cmd))
#     application.add_handler(CommandHandler("add", add_cmd))
#     application.add_handler(CommandHandler("remove", remove_cmd))
#     application.add_handler(CommandHandler("list", list_cmd))
#     application.add_handler(CommandHandler("interval", interval_cmd))

#     print("Bot is running (long polling)...")
#     application.run_polling()


# if __name__ == "__main__":
#     main()
