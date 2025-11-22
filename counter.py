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
BOT_TOKEN = "8092658674:AAHt2XZNOoVQOEcizA-YFGyZ9UyTgYVzdcE"
CHAT_ID = "394617203"

# BOT_TOKEN = "6015328845:AAEr5M2VWVqGugUOGaTVwJ747xIomscR2s0"
# CHAT_ID = "1324005362"

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

        # --- محاسبه درصد کلیدواژه‌های فعال ---
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        # تعداد کل کلیدواژه‌ها
        cur.execute("SELECT COUNT(*) FROM user_keywords")
        total_keywords = cur.fetchone()[0]

        # تعداد کلیدواژه‌هایی که حداقل یک keyword_event داشتند
        cur.execute("""
            SELECT COUNT(DISTINCT keyword)
            FROM keyword_events
            WHERE created_at >= ?
        """, ((datetime.utcnow() - timedelta(hours=SEEN_CHECK_INTERVAL_HOURS)).isoformat(),))
        active_keywords = cur.fetchone()[0]

        conn.close()

        # محاسبه درصد
        percent = 0
        if total_keywords > 0:
            percent = (active_keywords / total_keywords) * 100

        # --- محاسبه درصد لینک‌های فعال ---
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        # تعداد کل لینک‌ها
        cur.execute("SELECT COUNT(*) FROM feeds")
        total_links = cur.fetchone()[0]

        # تعداد لینک‌هایی که seen جدید داشته‌اند
        cur.execute("""
            SELECT COUNT(DISTINCT feed_url)
            FROM seen
            WHERE created_at >= ?
        """, ((datetime.utcnow() - timedelta(hours=SEEN_CHECK_INTERVAL_HOURS)).isoformat(),))
        active_links = cur.fetchone()[0]

        conn.close()

        # محاسبه درصد لینک‌ها
        percent_links = 0
        if total_links > 0:
            percent_links = (active_links / total_links) * 100

        # ساخت پیام
        message = (
            f"⏱ گزارش مانیتورینگ در {now_str}\n"
            f"📨 در {SEEN_CHECK_INTERVAL_HOURS} ساعت گذشته، {count} فید ارسال شده است ✅\n"
            f"📊 درصد کلیدواژه‌های فعال: {percent:.2f}%\n"
            f"🔗 درصد لینک‌های فعال: {percent_links:.2f}%"
        )


        print(message)
        send_telegram_message(message)

        time.sleep(SEEN_CHECK_INTERVAL_HOURS * 3600)



# ================= مانیتور کلیدواژه‌های جدید (با نام/یوزرنیم) =================
def get_all_keywords_with_user_info():
    """
    برمی‌گرداند لیستی از سطرها به شکل:
      (chat_id, keyword, name, username)
    با یک JOIN روی جدول chats تا اطلاعات اسم/یوزرنیم گرفته شود.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT k.chat_id, k.keyword, c.name, c.username
        FROM {KEYWORDS_TABLE} k
        LEFT JOIN {USERS_TABLE} c ON k.chat_id = c.chat_id
        """
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def _user_label(chat_id: str, name: str, username: str) -> str:
    """
    بازمی‌گرداند یک متن برای نمایش کاربر:
      اگر username موجود باشه: @username
      در غیر این صورت اگر name موجود باشه: name
      و اگر هیچ‌کدوم نبود، chat_id
    """
    if username:
        # تضمین اینکه با @ شروع کنه
        return f"@{username.lstrip('@')}"
    if name:
        return name
    return str(chat_id)


def monitor_keywords():
    print("🗝 مانیتور کلیدواژه‌ها فعال شد...")
    send_telegram_message("🗝 مانیتورینگ کلیدواژه‌ها شروع شد ✅")

    # خواندن اولیه: لیستی از ردیف‌ها با اطلاعات کاربر
    rows = get_all_keywords_with_user_info()
    # مجموعه‌ای از کلیدواژه‌های فعلی (فقط خودِ کلیدواژه برای جلوگیری از تکرار)
    seen_keyword_set = {kw for (_cid, kw, _name, _uname) in rows}

    if seen_keyword_set:
        total = len(seen_keyword_set)
        send_telegram_message(f"🔍 مانیتورینگ آغاز شد — {total} کلیدواژه موجود شناسایی شد.")

        # لیست یونیک کلیدواژه‌ها مرتب‌شده
        unique_keywords = sorted(seen_keyword_set)

        # ارسال لیست کلیدواژه‌ها (بصورت chunk اگر طول زیاد باشه)
        chunk_size = 40  # تعداد کلیدواژه در هر پیام (قابل تنظیم)
        for i in range(0, len(unique_keywords), chunk_size):
            chunk = unique_keywords[i:i+chunk_size]
            msg = "🗝 کلیدواژه‌های فعلی:\n" + "\n".join(chunk)
            send_telegram_message(msg)

    # حلقه اصلی: هر بار جدول را می‌خوانیم و فقط کلیدواژه‌های کاملاً جدید (که قبلاً نبودند) را گزارش می‌کنیم
    while True:
        time.sleep(KEYWORD_CHECK_INTERVAL)
        current_rows = get_all_keywords_with_user_info()
        # مجموعه فعلی کلیدواژه‌ها (فقط متن کلیدواژه)
        current_keyword_set = {kw for (_cid, kw, _name, _uname) in current_rows}

        # کلیدواژه‌هایی که الان وجود دارند ولی قبلاً نبودند
        newly_added_keywords = current_keyword_set - seen_keyword_set

        if newly_added_keywords:
            # برای هر کلیدواژه‌ی جدید، همه‌ی کاربرانی که آن را دارند پیدا کن و نام/یوزرنیم‌شان را نمایش بده
            for new_kw in sorted(newly_added_keywords):
                # فیلتر کردن ردیف‌هایی که keyword == new_kw
                owners = [
                    (cid, name, uname) for (cid, kw, name, uname) in current_rows if kw == new_kw
                ]
                # ساخت لیست نمایش‌دهنده‌ها
                owners_labels = [_user_label(cid, name, uname) for (cid, name, uname) in owners]
                owners_text = ", ".join(owners_labels)

                message = (
                    f"🆕 کلیدواژه جدید اضافه شد!\n"
                    f"🔑 Keyword: {new_kw}\n"
                    f"👥 توسط: {owners_text}"
                )
                send_telegram_message(message)
                print(message)

            # به‌روز‌رسانی مجموعه‌ی دیده‌شده‌ها
            seen_keyword_set = current_keyword_set

def get_keyword_stats(hours):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    since = datetime.utcnow() - timedelta(hours=hours)
    since_str = since.isoformat()

    cur.execute("""
        SELECT keyword, feed_url, COUNT(*) AS cnt
        FROM keyword_events
        WHERE created_at >= ?
        GROUP BY keyword, feed_url
    """, (since_str,))
    
    rows = cur.fetchall()
    conn.close()
    return rows


def monitor_keyword_stats():
    HOURS = 24  # قابل تنظیم
    
    send_telegram_message(f"📊 مانیتور آمار کلیدواژه‌ها هر {HOURS} ساعت فعال شد.")
    
    while True:
        rows = get_keyword_stats(HOURS)

        # ساخت مپ keyword → feed counts
        stats = {}
        for r in rows:
            kw = r["keyword"]
            feed = r["feed_url"]
            cnt = r["cnt"]
            stats.setdefault(kw, []).append((feed, cnt))

        # استخراج کلیدواژه‌های موجود
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT keyword FROM user_keywords")
        all_keywords = [row[0] for row in cur.fetchall()]
        conn.close()

        # تشخیص کلیدواژه‌هایی که هیچ فیدی نگرفته‌اند
        without_feed = sorted(set(all_keywords) - set(stats.keys()))

        # ساخت گزارش
        report = f"📊 گزارش {HOURS} ساعته کلیدواژه‌ها:\n\n"

        for kw, feeds in stats.items():
            t = sum(cnt for _, cnt in feeds)
            report += f"🔑 {kw} → {t} فید\n"
            for feed, cnt in feeds:
                report += f"   • {feed}: {cnt}\n"
            report += "\n"

        if without_feed:
            report += "❌ کلیدواژه‌های بدون فید:\n"
            for kw in without_feed:
                report += f"  - {kw}\n"

        # ---------- بررسی سایز پیام ----------
        if len(report) <= 3900:  # در محدوده امن تلگرام
            send_telegram_message(report)
        else:
            # ذخیره گزارش در فایل
            filename = f"keyword_report_{int(time.time())}.txt"
            with open(filename, "w", encoding="utf-8") as f:
                f.write(report)

            # ارسال فایل به تلگرام
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
            with open(filename, "rb") as doc:
                requests.post(
                    url,
                    data={"chat_id": CHAT_ID},
                    files={"document": doc}
                )

        # صبر برای دوره بعد
        time.sleep(HOURS * 3600)



# ================= اجرای همه‌ی مانیتورها =================
if __name__ == "__main__":
    send_telegram_message("🚀 مانیتورینگ کلی شروع شد ✅")

    threading.Thread(target=monitor_users, daemon=True).start()
    threading.Thread(target=monitor_seen_table, daemon=True).start()
    threading.Thread(target=monitor_keywords, daemon=True).start()
    threading.Thread(target=monitor_keyword_stats, daemon=True).start()

    while True:
        time.sleep(60)
