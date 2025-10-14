# app/sub/payments_db.py
# -*- coding: utf-8 -*-
import sqlite3
import threading
import json
import os
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta

DEFAULT_DB = os.getenv("STATE_DB", "state.db")


class PaymentsDB:
    def __init__(self, db_path: str = DEFAULT_DB):
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_schema()

    def _locked_cursor(self):
        class Ctx:
            def __init__(self, outer):
                self.outer = outer
                self.cur = None

            def __enter__(self):
                self.outer._lock.acquire()
                self.cur = self.outer.conn.cursor()
                return self.cur

            def __exit__(self, exc_type, exc, tb):
                try:
                    if exc_type is None:
                        self.outer.conn.commit()
                finally:
                    try:
                        self.cur.close()
                    except Exception:
                        pass
                    self.outer._lock.release()
        return Ctx(self)

    def _init_schema(self):
        with self._locked_cursor() as cur:
            # جدول پرداخت‌ها (Payments)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS payments(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    gateway TEXT NOT NULL,
                    status TEXT NOT NULL,
                    authority TEXT,
                    ref_id TEXT,
                    days INTEGER,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_payments_authority ON payments (authority)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_payments_chat_id ON payments (chat_id)")
            
            # ----------------------------------------------------------------------
            # جدول جدید: اشتراک‌ها (Subscriptions)
            # ----------------------------------------------------------------------
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions(
                    chat_id TEXT PRIMARY KEY,
                    start_date TEXT NOT NULL,  -- تاریخ شروع اشتراک
                    end_date TEXT NOT NULL,    -- تاریخ پایان اشتراک (مهمترین فیلد)
                    active INTEGER NOT NULL,
                    last_payment_id INTEGER,
                    FOREIGN KEY (last_payment_id) REFERENCES payments(id)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_chat_id ON subscriptions (chat_id)")
            self.conn.commit()

    # -------- payments --------
    def create_payment(self, chat_id: str, amount: int, gateway: str, days: int) -> int: 
        """ایجاد یک رکورد جدید پرداخت و برگرداندن ID آن."""
        with self._locked_cursor() as cur:
            cur.execute(
                "INSERT INTO payments(chat_id, amount, gateway, status, days) VALUES (?, ?, ?, 'pending', ?)",
                (chat_id, amount, gateway, days), # <--- اضافه شدن days
            )
            return cur.lastrowid

    def update_payment_status(self, payment_id: int, status: str, ref_id: Optional[str] = None) -> None:
        with self._locked_cursor() as cur:
            cur.execute(
                """
                UPDATE payments
                SET status = ?, ref_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, ref_id, payment_id),
            )

    def get_payment(self, payment_id: int) -> Optional[dict]:
        with self._locked_cursor() as cur:
            cur.execute("SELECT * FROM payments WHERE id = ?", (payment_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    # -------- payments --------
    def update_payment_authority(self, payment_id: int, authority: str) -> None:
        """ذخیره authority برای یک پرداخت"""
        with self._locked_cursor() as cur:
            cur.execute(
                """
                UPDATE payments
                SET authority = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (authority, payment_id),
            )

    def get_payment_by_authority(self, authority: str) -> Optional[dict]:
        """گرفتن رکورد پرداخت بر اساس authority"""
        with self._locked_cursor() as cur:
            cur.execute("SELECT * FROM payments WHERE authority = ?", (authority,))
            row = cur.fetchone()
            return dict(row) if row else None

    # -------- subscriptions --------
    def activate_subscription(self, chat_id: str, days: int, payment_id: int):
        # از کد شما در snippet استفاده شده است
        now = datetime.utcnow()
        with self._locked_cursor() as cur:
            cur.execute("SELECT end_date FROM subscriptions WHERE chat_id = ?", (chat_id,))
            sub = cur.fetchone()
            
            if sub:
                # تمدید اشتراک: تاریخ پایان جدید را حساب می‌کنیم
                current_end = datetime.fromisoformat(sub["end_date"])
                
                # اگر اشتراک منقضی شده، تمدید از الان شروع می‌شود، در غیر این صورت از تاریخ پایان فعلی
                start_from = max(now, current_end)
                new_end = start_from + timedelta(days=days)
                
                cur.execute(
                    """
                    UPDATE subscriptions
                    SET end_date = ?, active = 1, last_payment_id = ?
                    WHERE chat_id = ?
                    """,
                    (new_end.isoformat(), payment_id, chat_id),
                )
            else:
                # ایجاد اشتراک جدید
                new_end = now + timedelta(days=days)
                cur.execute(
                    """
                    INSERT INTO subscriptions(chat_id, start_date, end_date, active, last_payment_id)
                    VALUES (?, ?, ?, 1, ?)
                    """,
                    (chat_id, now.isoformat(), new_end.isoformat(), payment_id),
                )

    def get_subscription_info(self, chat_id: str) -> Optional[dict]:
        """اطلاعات کامل اشتراک کاربر را برمی‌گرداند."""
        with self._locked_cursor() as cur:
            cur.execute("SELECT * FROM subscriptions WHERE chat_id = ?", (chat_id,))
            row = cur.fetchone()
            if not row:
                return None
            
            sub_info = dict(row)
            
            # محاسبه روزهای باقی‌مانده
            end_date = datetime.fromisoformat(sub_info["end_date"])
            now = datetime.utcnow()
            
            if end_date > now:
                # اشتراک فعال است
                remaining_days = (end_date - now).days
                # برای نمایش دقیق‌تر، یک روز کامل را به بالا گرد می‌کنیم
                remaining_days = max(1, remaining_days + 1)
                sub_info["remaining_days"] = remaining_days
                sub_info["is_active"] = True
            else:
                # اشتراک منقضی شده
                sub_info["remaining_days"] = 0
                sub_info["is_active"] = False
                
            return sub_info

    # متد check_active_subscription قدیمی را حذف می‌کنیم یا به این شکل ساده‌سازی می‌کنیم:
    def check_active_subscription(self, chat_id: str) -> bool:
        info = self.get_subscription_info(chat_id)
        return info["is_active"] if info else False

    # def check_active_subscription(self, chat_id: str) -> bool:
    #     from datetime import datetime
    #     now = datetime.utcnow().isoformat()
    #     with self._locked_cursor() as cur:
    #         cur.execute(
    #             "SELECT 1 FROM subscriptions WHERE chat_id = ? AND active = 1 AND end_date > ?",
    #             (chat_id, now),
    #         )
    #         return bool(cur.fetchone())

    def get_subscription(self, chat_id: str) -> Optional[dict]:
        with self._locked_cursor() as cur:
            cur.execute("SELECT * FROM subscriptions WHERE chat_id = ?", (chat_id,))
            row = cur.fetchone()
            return dict(row) if row else None
