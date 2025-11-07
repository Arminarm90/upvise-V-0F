import sqlite3
import time
from datetime import datetime, timedelta
import requests

# ---------------- تنظیمات ----------------
DB_PATH = "state.db"

# 🕒 بازه زمانی بررسی (به ساعت)
CHECK_INTERVAL_HOURS = 2  # هر زمان خواستی عوض کن

# 🔐 اطلاعات بات تلگرام
BOT_TOKEN = "6015328845:AAEr5M2VWVqGugUOGaTVwJ747xIomscR2s0"  # ← توکن باتت
CHAT_ID = "1324005362"  # ← آیدی عددی خودت یا گروهی که می‌خوای پیام بره اونجا

# ------------------------------------------


def send_telegram_message(text: str):
    """ارسال پیام به تلگرام از طریق Bot API"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"❌ خطا در ارسال پیام به تلگرام: {e}")


def get_seen_count_since(hours_ago: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # بررسی وجود ستون created_at
    cur.execute("PRAGMA table_info(seen);")
    cols = [c[1] for c in cur.fetchall()]
    has_time_col = "created_at" in cols

    if has_time_col:
        since_time = datetime.now() - timedelta(hours=hours_ago)
        cur.execute("SELECT COUNT(*) FROM seen WHERE created_at >= ?", (since_time.isoformat(),))
    else:
        cur.execute("SELECT COUNT(*) FROM seen;")
        total = cur.fetchone()[0]
        conn.close()
        return total

    count = cur.fetchone()[0]
    conn.close()
    return count


def monitor_seen_table():
    send_telegram_message("📊 مانیتورینگ جدول `seen` شروع شد ✅")
    last_total = get_seen_count_since(99999)

    while True:
        current_total = get_seen_count_since(99999)
        new_records = current_total - last_total
        last_total = current_total

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        message = (
            f"⏱ گزارش مانیتورینگ در {now_str}\n"
            f"📨 در {CHECK_INTERVAL_HOURS} ساعت گذشته، {new_records} فید ارسال شده است ✅"
        )

        print(message)
        send_telegram_message(message)

        time.sleep(CHECK_INTERVAL_HOURS * 3600)


if __name__ == "__main__":
    monitor_seen_table()
