import sqlite3
import time
from datetime import datetime, timedelta
import threading
import requests
import json

# ---------------- تنظیمات ----------------
DB_PATH = "state.db"

USERS_TABLE = "chats"
SEEN_TABLE = "seen"
KEYWORDS_TABLE = "user_keywords"

# 🔐 اطلاعات بات تلگرام
# BOT_TOKEN = "1759611476:AAHOYSJyTxXu6tJDPa1-F06QjOYFj8BsLqg"
# CHAT_ID = "1324005362"

BOT_TOKEN = "8092658674:AAHt2XZNOoVQOEcizA-YFGyZ9UyTgYVzdcE"
CHAT_ID = "394617203"
# ⏱ تنظیم بازه‌ها
USER_CHECK_INTERVAL = 10             # هر چند ثانیه چک کنه (افزایش کاربر)
SEEN_CHECK_INTERVAL_HOURS = 24       # هر چند ساعت گزارش بده (فیدها)
KEYWORD_CHECK_INTERVAL = 15          # هر چند ثانیه کلیدواژه‌ها چک بشن
# ------------------------------------------


def send_telegram_message(text: str):
    """ارسال پیام به تلگرام"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"❌ خطا در ارسال پیام به تلگرام: {e}")


# ================= مانیتور کاربران جدید =================
def get_user_count():
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {USERS_TABLE}")
        count = cur.fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        print(f"[ERROR] DB access (users): {e}")
        return None


def monitor_users():
    print("👥 مانیتور کاربران فعال شد...")
    last_count = get_user_count()
    if last_count is None:
        send_telegram_message("⚠️ خطا در خواندن تعداد اولیه کاربران.")
        return

    while True:
        time.sleep(USER_CHECK_INTERVAL)
        current_count = get_user_count()
        if current_count is None:
            continue

        if current_count > last_count:
            new_users = current_count - last_count
            message = (
                f"📢 کاربر جدید اضافه شد!\n"
                f"👤 تعداد کاربران جدید: {new_users}\n"
                f"👥 کل کاربران فعلی: {current_count}"
            )
            send_telegram_message(message)
            print(message)
            last_count = current_count


# ================= مانیتور فیدهای جدید =================
def get_seen_count_since(hours_ago: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({SEEN_TABLE});")
    cols = [c[1] for c in cur.fetchall()]
    has_time_col = "created_at" in cols

    if has_time_col:
        since_time = datetime.now() - timedelta(hours=hours_ago)
        cur.execute(
            f"SELECT COUNT(*) FROM {SEEN_TABLE} WHERE created_at >= ?",
            (since_time.isoformat(),)
        )
    else:
        cur.execute(f"SELECT COUNT(*) FROM {SEEN_TABLE};")

    count = cur.fetchone()[0]
    conn.close()
    return count


def monitor_seen_table():
    print("📊 مانیتور فیدها فعال شد...")
    send_telegram_message("📊 مانیتورینگ جدول فیدها شروع شد ✅")

    while True:
        count = get_seen_count_since(SEEN_CHECK_INTERVAL_HOURS)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        message = (
            f"⏱ گزارش مانیتورینگ در {now_str}\n"
            f"📨 در {SEEN_CHECK_INTERVAL_HOURS} ساعت گذشته، {count} فید ارسال شده است ✅"
        )
        print(message)
        send_telegram_message(message)
        time.sleep(SEEN_CHECK_INTERVAL_HOURS * 3600)


# ================= مانیتور کلیدواژه‌های جدید =================
def get_all_keywords():
    """تمام کلیدواژه‌های فعلی جدول را برمی‌گرداند"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(f"SELECT chat_id, keyword FROM {KEYWORDS_TABLE}")
    rows = cur.fetchall()
    conn.close()
    return rows


def monitor_keywords():
    print("🗝 مانیتور کلیدواژه‌ها فعال شد...")
    send_telegram_message("🗝 مانیتورینگ کلیدواژه‌ها شروع شد ✅")

    seen_keywords = set(get_all_keywords())  # مجموعه‌ای از (chat_id, keyword)

    if seen_keywords:
        total = len(seen_keywords)
        send_telegram_message(f"🔍 مانیتورینگ آغاز شد")

        # استخراج فقط کلیدواژه‌ها بدون تکرار
        unique_keywords = sorted({kw for _, kw in seen_keywords})

        # تقسیم پیام‌ها اگر طولش زیاد شد (برای محدودیت تلگرام)
        chunk_size = 40  # حداکثر تعداد کلمه در هر پیام
        for i in range(0, len(unique_keywords), chunk_size):
            chunk = unique_keywords[i:i+chunk_size]
            msg = "🗝 کلیدواژه‌های فعلی:\n" + "\n".join(chunk)
            send_telegram_message(msg)

    while True:
        time.sleep(KEYWORD_CHECK_INTERVAL)
        current_keywords = set(get_all_keywords())

        new_keywords = current_keywords - seen_keywords
        if new_keywords:
            for chat_id, keyword in new_keywords:
                message = (
                    f"🆕 کلیدواژه جدید اضافه شد!\n"
                    f"👤 Chat ID: {chat_id}\n"
                    f"🔑 Keyword: {keyword}"
                )
                send_telegram_message(message)
                print(message)

            seen_keywords = current_keywords



# ================= اجرای همه‌ی مانیتورها =================
if __name__ == "__main__":
    send_telegram_message("🚀 مانیتورینگ کلی شروع شد ✅")

    threading.Thread(target=monitor_users, daemon=True).start()
    threading.Thread(target=monitor_seen_table, daemon=True).start()
    threading.Thread(target=monitor_keywords, daemon=True).start()

    while True:
        time.sleep(60)
