import sqlite3
import time
import requests

# ----- تنظیمات -----
DB_PATH = "state.db"  # مسیر دیتابیس
TABLE_NAME = "chats"
ADMIN_CHAT_ID = "1324005362"  # آیدی ادمین
BOT_TOKEN = "1759611476:AAHOYSJyTxXu6tJDPa1-F06QjOYFj8BsLqg"
CHECK_INTERVAL = 10  # هر چند ثانیه چک کنه (مثلاً هر 10 ثانیه)

# ----- تابع برای ارسال پیام -----
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": ADMIN_CHAT_ID, "text": text}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print(f"[ERROR] Couldn't send message: {e}")

# ----- تابع برای گرفتن تعداد یوزرها -----
def get_user_count():
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}")
        count = cur.fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        print(f"[ERROR] DB access: {e}")
        return None

# ----- حلقه اصلی -----
def main():
    print("[INFO] Monitoring started...")
    last_count = get_user_count()
    if last_count is None:
        print("[ERROR] Could not read initial count.")
        return

    while True:
        time.sleep(CHECK_INTERVAL)
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

# اجرای اسکریپت
if __name__ == "__main__":
    main()
