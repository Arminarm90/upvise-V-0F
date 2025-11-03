import asyncio
import logging
import re
import os
import httpx
from playwright.async_api import async_playwright
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# === تنظیمات اولیه ===
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", 15))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# === الگوهای کمکی ===
CATEGORY_PATTERN = re.compile(r"category_id=(\d+)")

# === دریافت داده از API رسمی/غیررسمی دیجی‌کالا ===
async def fetch_from_api(category_id: str):
    url = f"https://api.digikala.com/v1/incredible-offers/products/?page=1&category_id={category_id}"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            r = await client.get(url)
            if r.status_code == 200 and "data" in r.json():
                products = r.json()["data"]["products"]

                # ✅ اصلاح قیمت‌ها (تبدیل ریال به تومان)
                for p in products:
                    variant = p.get("default_variant") or p.get("variant", {})
                    price_info = variant.get("price", {})
                    if "selling_price" in price_info:
                        price_info["selling_price"] = int(price_info["selling_price"] / 10)
                    if "rrp_price" in price_info:
                        price_info["rrp_price"] = int(price_info["rrp_price"] / 10)

                return products
    except Exception as e:
        logger.warning(f"API fetch failed: {e}")
    return None


# === در صورت شکست API از Playwright استفاده کن ===
async def fetch_from_playwright(category_id: str):
    target_url = f"https://www.digikala.com/incredible-offers/?category_id={category_id}"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(target_url, timeout=60000)
            data = await page.evaluate("window.__NEXT_DATA__")
            await browser.close()

            products = (
                data.get("props", {})
                .get("pageProps", {})
                .get("initialReduxState", {})
                .get("incredibleOffersV4", {})
                .get("products", [])
            )
            return products
    except Exception as e:
        logger.error(f"Playwright fallback failed: {e}")
    return None

# === قالب‌بندی خروجی برای تلگرام ===
def format_product(item, index):
    try:
        title = item.get("title_fa") or item.get("title") or "بدون عنوان"
        variant = item.get("default_variant") or item.get("variant", {})
        price_info = variant.get("price", {})
        selling_price = price_info.get("selling_price") or 0
        rrp_price = price_info.get("rrp_price") or 0
        discount = 0
        if selling_price and rrp_price and rrp_price > selling_price:
            discount = round((rrp_price - selling_price) / rrp_price * 100)

        stock_status = "موجود ✅"
        if variant.get("is_in_stock") is False:
            stock_status = "ناموجود ❌"
        elif variant.get("remaining") or variant.get("remaining_quantity"):
            stock_status = f"موجود ✅ ({variant.get('remaining') or variant.get('remaining_quantity')} عدد)"

        link = f"https://www.digikala.com/product/{item.get('id')}/"

        def fnum(n):
            return f"{n:,}".replace(",", "،")

        msg = (
            f"{index}️⃣ {title}\n"
            f"💸 قیمت جدید: {fnum(selling_price)} تومان\n"
        )
        if rrp_price:
            msg += f"💰 قیمت قبل: {fnum(rrp_price)} تومان\n"
        if discount:
            msg += f"📉 تخفیف: {discount}%\n"
        msg += f"{stock_status}\n🔗 لینک محصول: {link}"
        return msg
    except Exception as e:
        logger.error(f"Format error: {e}")
        return f"{index}️⃣ خطا در پردازش محصول"

# === دستور /offers ===
async def offers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📎 لطفاً لینک دسته‌بندی پیشنهادهای شگفت‌انگیز را ارسال کن.")
    context.user_data["awaiting_offer_link"] = True

# === وقتی کاربر لینک می‌فرسته ===
async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_offer_link"):
        return

    text = update.message.text.strip()
    match = CATEGORY_PATTERN.search(text)
    if not match:
        await update.message.reply_text("❌ لینک معتبر نیست. لطفاً لینک دسته‌بندی صحیح بفرست.")
        return

    category_id = match.group(1)
    await update.message.reply_text("⏳ در حال دریافت اطلاعات... لطفاً چند لحظه صبر کن.")

    # ابتدا تلاش با API
    products = await fetch_from_api(category_id)
    source = "API"

    # اگر API شکست خورد، fallback → Playwright
    if not products:
        source = "Playwright"
        products = await fetch_from_playwright(category_id)

    if not products:
        await update.message.reply_text("❌ خطا در دریافت داده‌ها. لطفاً بعداً دوباره تلاش کن.")
        context.user_data.pop("awaiting_offer_link", None)
        return

    top_items = products[:10]
    msgs = []
    for i, item in enumerate(top_items, 1):
        msgs.append(format_product(item, i))

    for m in msgs:
        await update.message.reply_text(m, disable_web_page_preview=True)

    await update.message.reply_text(f"✅ منبع داده: {source}")
    context.user_data.pop("awaiting_offer_link", None)

# === تابع قابل فراخوانی برای RSS ===
import re
from persiantools.jdatetime import JalaliDate

def _escape_md(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", text)

def _num_emoji(i: int) -> str:
    if 1 <= i <= 9:
        return f"{i}\u20E3"
    elif i == 10:
        return "🔟"
    else:
        return str(i)

def _get_jalali_date_str() -> str:
    today = JalaliDate.today()
    months_fa = [
        "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
        "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"
    ]
    month_name = months_fa[today.month - 1]
    return f"{today.day} {month_name} {today.year}"


async def process_digikala(store, cid_int, url: str, chat_lang) -> str:
    """
    نسخه‌ی نهایی دیجی‌کالا:
    - مدیریت دقیق seen در دیتابیس (بدون تکرار)
    - خروجی Markdown منظم مثل دیوار
    - نمایش وضعیت موجودی (موجود / ناموجود / تعداد باقی‌مانده)
    - تا سقف ۱۰ تخفیف جدید
    """
    match = re.search(r"category_id=(\d+)", url)
    category_id = match.group(1) if match else None

    # --- دریافت دیتا از API یا Playwright ---
    products = await fetch_from_api(category_id) if category_id else await fetch_from_api("")
    source = "API"
    if not products:
        products = await fetch_from_playwright(category_id or "")
        source = "Playwright"
    if not products:
        return ""

    # --- کلید اختصاصی برای seen ---
    seen_key = f"seen_admin::digikala::{url}"
    seen_ids = store.get_seen(cid_int, seen_key) or set()

    # فقط محصولات جدید
    new_items = [p for p in products if str(p.get("id")) not in seen_ids]
    if not new_items:
        return ""

    latest = new_items[:10]

    # ثبت seen در دیتابیس (بدون ورود به feeds)
    for p in latest:
        pid = str(p.get("id"))
        if pid:
            seen_ids.add(pid)
    store.set_seen(cid_int, seen_key, seen_ids)

    # --- فرمت خروجی ---
    count = len(latest)
    

    header = f"{count} تخفیف جدید در دیجی‌کالا\n\n"

    def fnum(n):
        try:
            return f"{int(n):,}".replace(",", "،")
        except Exception:
            return str(n)

    lines = []
    for i, item in enumerate(latest, start=1):
        num_emoji = _num_emoji(i)
        title = _escape_md(item.get("title_fa") or item.get("title") or "بدون عنوان")

        variant = item.get("default_variant") or item.get("variant", {})
        price_info = variant.get("price", {})
        selling_price = price_info.get("selling_price") or 0
        rrp_price = price_info.get("rrp_price") or 0
        discount = 0
        if selling_price and rrp_price and rrp_price > selling_price:
            discount = round((rrp_price - selling_price) / rrp_price * 100)

        # ✅ وضعیت موجودی
        stock_status = "موجود ✅"
        if variant.get("is_in_stock") is False:
            stock_status = "ناموجود ❌"
        elif variant.get("remaining") or variant.get("remaining_quantity"):
            qty = variant.get("remaining") or variant.get("remaining_quantity")
            stock_status = f"موجود ✅ ({qty} عدد باقی‌مانده)"

        link = f"https://www.digikala.com/product/{item.get('id')}/"
        link_md = link.replace(")", "\\)").replace("(", "\\(")
        today = _get_jalali_date_str()
        part = (
            f"{num_emoji} *{title}*\n"
            f"_دیجی‌کالا | {today}_\n\n"
            f"💸 قیمت جدید: {fnum(selling_price)} تومان"
        )
        if rrp_price:
            part += f"\n💰 قیمت قبل: {fnum(rrp_price)} تومان"
        if discount:
            part += f"\n📉 تخفیف: {discount}%"
        part += f"\n{stock_status}"
        part += f"\n🔗 [مشاهده محصول]({link_md})"

        lines.append(part)

    body = "\n\n".join(lines)
    msg = header + body
    return msg



# === تابع main ===
# def main():
#     app = ApplicationBuilder().token(BOT_TOKEN).build()
#     app.add_handler(CommandHandler("offers", offers_command))
#     app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
#     logger.info("Bot started.")
#     app.run_polling()

# if __name__ == "__main__":
#     main()
