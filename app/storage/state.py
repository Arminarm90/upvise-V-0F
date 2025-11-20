# app/storage/state.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import json, os, tempfile
from typing import Dict, List, Tuple, Iterable, Any


class StateStore:
    """
    ساختار کلی ذخیره:
    {
      "<chat_id>": {
        "lang": "fa" | "en" | ...,
        "feeds": ["https://...", ...],
        "seen": {
          "<feed_url>": ["<item_id>", ...]
        }
      },
      ...
    }
    """
    def __init__(self, path: str):
        self.path = path
        # مطمئن شو پوشه وجود داره
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._state: Dict[str, dict] = self._load()

    # ---------------------- فایل/دیسک ----------------------
    def _load(self) -> Dict[str, dict]:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save(self) -> None:
        """ذخیره اتمیک روی دیسک تا خراب شدن فایل کمینه شود."""
        tmp_dir = os.path.dirname(self.path) or "."
        try:
            with tempfile.NamedTemporaryFile("w", delete=False, dir=tmp_dir, encoding="utf-8") as tf:
                json.dump(self._state, tf, ensure_ascii=False, indent=2)
                tmp_name = tf.name
            os.replace(tmp_name, self.path)
        except Exception:
            # اگر هر مشکلی پیش آمد، در بدترین حالت فایل اصلی دست‌نخورده می‌ماند
            try:
                if tmp_name and os.path.exists(tmp_name):
                    os.remove(tmp_name)
            except Exception:
                pass

    # ---------------------- دسترسی سطح چت ----------------------
    def get_chat(self, chat_id: int | str) -> dict:
        """دریافت شیء وضعیت یک چت؛ اگر وجود نداشت، نمونهٔ خالی برمی‌گرداند (بدون ایجاد روی دیسک)."""
        return dict(self._state.get(str(chat_id), {}))

    def set_chat(self, chat_id: int | str, data: dict) -> None:
        """
        ثبت/به‌روزرسانی وضعیت یک چت. حداقل فیلدها را سالم نگه می‌دارد.
        data می‌تواند شامل lang/feeds/seen باشد.
        """
        cid = str(chat_id)
        cur = self._state.get(cid, {})
        # ادغام محتوا
        name = data.get("name", cur.get("name"))
        lang = data.get("lang", cur.get("lang"))
        feeds = list(data.get("feeds", cur.get("feeds", [])) or [])
        seen = dict(data.get("seen", cur.get("seen", {})) or {})
        self._state[cid] = {"name": name, "lang": lang, "feeds": feeds, "seen": seen}
        self.save()

    def register_user(self, chat_id: int | str, name: str) -> None:
        cid = str(chat_id)
        # فقط در صورتی که کاربر وجود ندارد، اطلاعات را اضافه می‌کنیم
        if cid not in self._state:
            self._state[cid] = {
                "name": name,
                "lang": "en", 
                "feeds": [],
                "seen": {}
            }
            self.save()
            
    def drop_chat(self, chat_id: int | str) -> bool:
        """
        حذف کامل یک چت از ذخیره (برای موارد 'Chat not found' یا 'bot blocked').
        خروجی: True اگر وجود داشت و حذف شد؛ False اگر اصلاً نبود.
        """
        cid = str(chat_id)
        if cid in self._state:
            try:
                del self._state[cid]
                self.save()
                return True
            except Exception:
                # در صورت بروز خطا، وضعیت در حافظه ممکن است حذف شده باشد اما ذخیره ناموفق بماند.
                # این سناریو نادر است و در چرخهٔ بعدی با _load بازسازی خواهد شد.
                return False
        return False

    # ---------------------- عملیات روی فیدها ----------------------
    def list_feeds(self, chat_id: int | str) -> List[str]:
        cid = str(chat_id)
        return list(self._state.get(cid, {}).get("feeds", []) or [])

    def add_feed(self, chat_id: int | str, url: str) -> bool:
        cid = str(chat_id)
        st = self._state.setdefault(cid, {})  # <-- تغییر اصلی
        feeds = list(st.get("feeds", []) or [])
        seen = dict(st.get("seen", {}) or {})
        
        if url in feeds:
            return False
            
        feeds.append(url)
        seen.setdefault(url, [])
        
        # ✅ تغییر اصلی: فقط فیلدهای مربوطه را به‌روزرسانی می‌کنیم.
        st["feeds"] = feeds
        st["seen"] = seen
        self.save()
        return True

    def remove_feed(self, chat_id: int | str, url: str) -> bool:
        cid = str(chat_id)
        st = self._state.get(cid, {})
        if not st:
            return False
            
        feeds = list(st.get("feeds", []) or [])
        seen = dict(st.get("seen", {}) or {})
        
        if url not in feeds:
            return False
            
        feeds.remove(url)
        seen.pop(url, None)
        
        # ✅ تغییر اصلی: فقط فیلدهای مربوطه را به‌روزرسانی می‌کنیم.
        st["feeds"] = feeds
        st["seen"] = seen
        self.save()
        return True

    # ---------------------- جلوگیری از ارسال تکراری ----------------------
    def get_seen(self, chat_id: int | str, url: str) -> set[str]:
        cid = str(chat_id)
        st = self._state.get(cid, {})
        return set(st.get("seen", {}).get(url, []) or [])

    def set_seen(self, chat_id: int | str, url: str, seen_set: Iterable[str]) -> None:
        cid = str(chat_id)
        st = self._state.setdefault(cid, {})  # <-- تغییر اصلی
        feeds = list(st.get("feeds", []) or [])
        seen = dict(st.get("seen", {}) or {})
        
        seen[url] = list(seen_set)
        
        # ✅ تغییر اصلی: فقط فیلدهای مربوطه را به‌روزرسانی می‌کنیم.
        st["feeds"] = feeds
        st["seen"] = seen
        self.save()

    # ---------------------- پیمایش ----------------------
    def iter_chats(self) -> List[Tuple[str, dict]]:
        """لیست (chat_id, state) برای همهٔ چت‌ها."""
        return list(self._state.items())

    def clear_feeds(self, chat_id: int) -> None:
        cid = str(chat_id)
        st = self._state.get(cid, {})
        if not st:
            return False
            
        feeds = list(st.get("feeds", []) or [])
        
             
        # ✅ تغییر اصلی: فقط فیلدهای مربوطه را به‌روزرسانی می‌کنیم.
        st["feeds"] = []
        self.save()
        return True
    
    
# app/storage/state_sqlite.py
import sqlite3
import threading
import json
import os
from typing import Dict, List, Tuple, Iterable, Any, Optional

DEFAULT_DB = os.getenv("STATE_DB", "state.db")


# app/storage/state_sqlite.py
# -*- coding: utf-8 -*-
import sqlite3
import threading
import json
import os
from typing import Dict, List, Tuple, Iterable, Any, Optional

DEFAULT_DB = os.getenv("STATE_DB", "state.db")


class SQLiteStateStore:
    def __init__(self, db_path: str = DEFAULT_DB):
        self.db_path = db_path
        # allow multithreaded access from async contexts
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        # return rows as dict-like
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_schema()

    def _locked_cursor(self):
        """Context manager that acquires lock and yields cursor."""
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
                        try:
                            self.outer.conn.commit()
                        except Exception:
                            pass
                finally:
                    try:
                        self.cur.close()
                    except Exception:
                        pass
                    self.outer._lock.release()

        return Ctx(self)

    def _init_schema(self) -> None:
        """
        Create base tables and ensure new columns exist (migration-safe).
        New fields:
          - chats.first_seen TEXT (timestamp when user first registered)
          - chats.last_action TEXT (last action timestamp)
          - chats.feeds_history TEXT (JSON array of all feeds ever added by user)
        """
        with self._locked_cursor() as cur:
            # PRAGMAs for better concurrency and safety
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA foreign_keys=ON;")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id TEXT PRIMARY KEY,
                    name TEXT,
                    username TEXT UNIQUE COLLATE NOCASE,
                    lang TEXT DEFAULT 'en'
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_chats_username ON chats(username);")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS feeds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    UNIQUE(chat_id, url),
                    FOREIGN KEY(chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS seen (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    feed_url TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    UNIQUE(chat_id, feed_url, item_id),
                    FOREIGN KEY(chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
                )
                """
            )
            cur.execute(
            """
                CREATE TABLE IF NOT EXISTS kv (
                    chat_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT,
                    PRIMARY KEY (chat_id, key)
                )
            """
            )
            self.conn.commit()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS keyword_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    keyword TEXT NOT NULL,
                    feed_url TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
                )
            """)

            cur.execute("PRAGMA table_info(chats);")
            cols = [r["name"] for r in cur.fetchall()]
            if "first_seen" not in cols:
                try:
                    cur.execute("ALTER TABLE chats ADD COLUMN first_seen TEXT;")
                except Exception:
                    pass
            if "last_action" not in cols:
                try:
                    cur.execute("ALTER TABLE chats ADD COLUMN last_action TEXT;")
                except Exception:
                    pass
            if "feeds_history" not in cols:
                try:
                    cur.execute("ALTER TABLE chats ADD COLUMN feeds_history TEXT DEFAULT '[]';")
                except Exception:
                    pass
                
            if "owner_id" not in cols:
                try:
                    cur.execute("ALTER TABLE chats ADD COLUMN owner_id TEXT;")
                except Exception:
                    pass


            self.conn.commit()

    # ---------------- normalize username ----------------
    def _normalize_username(self, username: Optional[str]) -> Optional[str]:
        """Ensure username stored like '@name' and lowercase (or None)."""
        if not username:
            return None
        s = str(username).strip()
        if not s:
            return None
        if s.startswith("@"):
            s = s[1:]
        s = s.lower()
        return "@" + s

    # ---------------- time helper ----------------
    def _now_sql(self) -> str:
        from datetime import datetime
        return datetime.utcnow().isoformat(timespec='seconds')

    # ---------------- core chat operations ---------------
    def register_user(self, chat_id: int | str, name: str, username: Optional[str] = None, lang: str = "en") -> None:
        cid = str(chat_id)
        uname = self._normalize_username(username)
        with self._locked_cursor() as cur:
            # check existence
            cur.execute("SELECT 1 FROM chats WHERE chat_id = ?", (cid,))
            if not cur.fetchone():
                cur.execute(
                    """
                    INSERT INTO chats(chat_id, name, username, lang, first_seen, last_action, feeds_history)
                    VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)
                    """,
                    (cid, name or None, uname, lang or "en", json.dumps([])),
                )
            else:
                # chat exists: update name/lang/username if provided (do not touch first_seen)
                if name is not None or uname is not None or lang is not None:
                    cur.execute(
                        """
                        UPDATE chats
                        SET name = COALESCE(?, name),
                            username = COALESCE(?, username),
                            lang = COALESCE(?, lang)
                        WHERE chat_id = ?
                        """,
                        (name, uname, lang, cid),
                    )

    def get_chat(self, chat_id: int | str) -> dict:
        cid = str(chat_id)
        with self._locked_cursor() as cur:
            cur.execute("SELECT chat_id, name, username, lang, first_seen, last_action, feeds_history FROM chats WHERE chat_id = ?", (cid,))
            row = cur.fetchone()
            if not row:
                return {}
            name = row["name"]
            username = row["username"]
            lang = row["lang"]
            first_seen = row["first_seen"]
            last_action = row["last_action"]
            feeds_history_raw = row["feeds_history"] or "[]"
            try:
                feeds_history = json.loads(feeds_history_raw)
            except Exception:
                feeds_history = []
            cur.execute("SELECT url FROM feeds WHERE chat_id = ? ORDER BY id", (cid,))
            feeds = [r["url"] for r in cur.fetchall()]
            cur.execute("SELECT feed_url, item_id FROM seen WHERE chat_id = ?", (cid,))
            seen_rows = cur.fetchall()
            seen: Dict[str, List[str]] = {}
            for r in seen_rows:
                seen.setdefault(r["feed_url"], []).append(r["item_id"])
            return {
                "name": name,
                "username": username,
                "lang": lang,
                "first_seen": first_seen,
                "last_action": last_action,
                "feeds": feeds,
                "feeds_history": feeds_history,
                "seen": seen,
            }

    def set_chat(self, chat_id: int | str, data: dict) -> None:
        """
        Upsert chat fields. data may contain name, username, lang, feeds, seen, first_seen, last_action, feeds_history.
        This will overwrite feeds/seen according to provided values (if present) and keep others.
        """
        cid = str(chat_id)
        name = data.get("name")
        username = data.get("username")
        lang = data.get("lang")
        feeds_in = data.get("feeds")
        seen_in = data.get("seen")
        first_seen_in = data.get("first_seen")
        last_action_in = data.get("last_action")
        feeds_history_in = data.get("feeds_history")

        uname = self._normalize_username(username) if username is not None else None

        with self._locked_cursor() as cur:
            cur.execute(
                """
                INSERT INTO chats(chat_id, name, username, lang, first_seen, last_action, feeds_history)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                  name = COALESCE(?, name),
                  username = COALESCE(?, username),
                  lang = COALESCE(?, lang),
                  first_seen = COALESCE(?, first_seen),
                  last_action = COALESCE(?, last_action),
                  feeds_history = COALESCE(?, feeds_history)
                """,
                (
                    cid,
                    name or None,
                    uname,
                    lang or None,
                    first_seen_in or None,
                    last_action_in or None,
                    json.dumps(feeds_history_in or []),
                    # conflict update args
                    name,
                    uname,
                    lang,
                    first_seen_in,
                    last_action_in,
                    json.dumps(feeds_history_in or []) if feeds_history_in is not None else None,
                ),
            )

            if feeds_in is not None:
                feeds = list(dict.fromkeys([str(u) for u in (feeds_in or [])]))
                if feeds:
                    placeholders = ",".join("?" for _ in feeds)
                    cur.execute(
                        f"DELETE FROM feeds WHERE chat_id = ? AND url NOT IN ({placeholders})",
                        tuple([cid] + feeds),
                    )
                else:
                    cur.execute("DELETE FROM feeds WHERE chat_id = ?", (cid,))
                for u in feeds:
                    cur.execute("INSERT OR IGNORE INTO feeds(chat_id, url) VALUES(?, ?)", (cid, u))

            if seen_in is not None:
                cur.execute("DELETE FROM seen WHERE chat_id = ?", (cid,))
                for feed_url, items in (seen_in or {}).items():
                    for it in (items or []):
                        cur.execute(
                            "INSERT OR IGNORE INTO seen(chat_id, feed_url, item_id) VALUES(?, ?, ?)",
                            (cid, str(feed_url), str(it)),
                        )

    def drop_chat(self, chat_id: int | str) -> bool:
        cid = str(chat_id)
        with self._locked_cursor() as cur:
            cur.execute("SELECT 1 FROM chats WHERE chat_id = ?", (cid,))
            if not cur.fetchone():
                return False
            cur.execute("DELETE FROM chats WHERE chat_id = ?", (cid,))
            return True

    # --------------- feed operations ---------------
    def list_feeds(self, chat_id: int | str) -> List[str]:
        cid = str(chat_id)
        with self._locked_cursor() as cur:
            cur.execute("SELECT url FROM feeds WHERE chat_id = ? ORDER BY id", (cid,))
            return [r["url"] for r in cur.fetchall()]

    def add_feed(self, chat_id: int | str, url: str) -> bool:
        cid = str(chat_id)
        u = str(url)
        with self._locked_cursor() as cur:
            cur.execute("INSERT OR IGNORE INTO chats(chat_id, lang, feeds_history, first_seen, last_action) VALUES(?, ?, COALESCE(?, '[]'), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)", (cid, "en", json.dumps([])))
            try:
                cur.execute("INSERT INTO feeds(chat_id, url) VALUES(?, ?)", (cid, u))
            except sqlite3.IntegrityError:
                pass

            cur.execute("SELECT feeds_history FROM chats WHERE chat_id = ?", (cid,))
            row = cur.fetchone()
            if row:
                raw = row["feeds_history"] or "[]"
                try:
                    arr = json.loads(raw)
                    if not isinstance(arr, list):
                        arr = []
                except Exception:
                    arr = []
                if u not in arr:
                    arr.append(u)
                    cur.execute("UPDATE chats SET feeds_history = ? WHERE chat_id = ?", (json.dumps(arr, ensure_ascii=False), cid))
            return True

    def remove_feed(self, chat_id: int | str, url: str) -> bool:
        cid = str(chat_id)
        u = str(url)
        with self._locked_cursor() as cur:
            cur.execute("SELECT 1 FROM feeds WHERE chat_id = ? AND url = ?", (cid, u))
            if not cur.fetchone():
                return False
            cur.execute("DELETE FROM seen WHERE chat_id = ? AND feed_url = ?", (cid, u))
            cur.execute("DELETE FROM feeds WHERE chat_id = ? AND url = ?", (cid, u))
            return True

    def clear_feeds(self, chat_id: int | str) -> bool:
        cid = str(chat_id)
        with self._locked_cursor() as cur:
            cur.execute("SELECT 1 FROM chats WHERE chat_id = ?", (cid,))
            if not cur.fetchone():
                return False
            # پاک کردن همه‌ی فیدها و آیتم‌های دیده‌شده
            cur.execute("DELETE FROM feeds WHERE chat_id = ?", (cid,))
            cur.execute("DELETE FROM seen WHERE chat_id = ?", (cid,))
            # ✅ پاک کردن تمام کلیدواژه‌ها هم‌زمان
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_keywords (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    keyword TEXT NOT NULL,
                    UNIQUE(chat_id, keyword)
                )
            """)
            cur.execute("DELETE FROM user_keywords WHERE chat_id = ?", (cid,))
            return True


    # --------------- seen operations ---------------
    def get_seen(self, chat_id: int | str, url: str) -> set:
        cid = str(chat_id)
        u = str(url)
        with self._locked_cursor() as cur:
            cur.execute("SELECT item_id FROM seen WHERE chat_id = ? AND feed_url = ? ORDER BY id", (cid, u))
            return set([row["item_id"] for row in cur.fetchall()])

    def set_seen(self, chat_id: int | str, url: str, seen_set: Iterable[str]) -> None:
        """
        ذخیره‌ی آیتم‌های seen برای هر فید.
        فیدهایی که با پیشوند خاص ('seen_admin::' یا 'takhfifan_seen::') شروع می‌شن،
        فقط در جدول seen ذخیره می‌شن و در جدول feeds نمی‌رن.
        """
        cid = str(chat_id)
        u = str(url)

        # اگر فید از نوع خاص (ادمین یا اختصاصی) است
        is_special_feed = u.startswith("seen_admin::") or u.startswith("takhfifan_seen::")

        with self._locked_cursor() as cur:
            # اطمینان از وجود کاربر
            cur.execute(
                "INSERT OR IGNORE INTO chats(chat_id, lang) VALUES(?, ?)",
                (cid, "en"),
            )

            # فقط اگر فید معمولی باشد، در جدول feeds هم ذخیره کن
            if not is_special_feed:
                cur.execute(
                    "INSERT OR IGNORE INTO feeds(chat_id, url) VALUES(?, ?)",
                    (cid, u),
                )

            # حالا seenها را به‌روز کن
            cur.execute(
                "DELETE FROM seen WHERE chat_id = ? AND feed_url = ?",
                (cid, u),
            )
            for it in (seen_set or []):
                cur.execute(
                    "INSERT OR IGNORE INTO seen(chat_id, feed_url, item_id) VALUES(?, ?, ?)",
                    (cid, u, str(it)),
                )

    # For groups and channels, set the owner user ID
    def set_owner(self, chat_id: int | str, owner_id: int | str) -> None:
        """ثبت مالک گروه یا کانال (کاربر ایجادکننده فید)."""
        cid = str(chat_id)
        oid = str(owner_id)
        with self._locked_cursor() as cur:
            # اطمینان از اینکه چت وجود داره
            cur.execute(
                "INSERT OR IGNORE INTO chats(chat_id, lang, feeds_history, first_seen, last_action) VALUES(?, ?, COALESCE(?, '[]'), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (cid, "en", json.dumps([])),
            )
            # تنظیم owner
            cur.execute("UPDATE chats SET owner_id = ? WHERE chat_id = ?", (oid, cid))

    def get_owner(self, chat_id: int | str) -> Optional[str]:
        cid = str(chat_id)
        with self._locked_cursor() as cur:
            cur.execute("SELECT owner_id FROM chats WHERE chat_id = ?", (cid,))
            r = cur.fetchone()
            return r["owner_id"] if r else None

    def delete_chat(self, chat_id: int):
        with self._locked_cursor() as cur:
            cur.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
            cur.execute("DELETE FROM feeds WHERE chat_id = ?", (chat_id,))
            cur.execute("DELETE FROM user_keywords WHERE chat_id = ?", (chat_id,))
            cur.execute("DELETE FROM seen WHERE chat_id = ?", (chat_id,))
            self.conn.commit()
            
    def set_chat_name(self, chat_id: int, name: str):
        """ثبت یا به‌روزرسانی نام چت (گروه/کانال)."""
        with self._locked_cursor() as cur:
            cur.execute("""
                INSERT INTO chats (chat_id, name)
                VALUES (?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET name = excluded.name
            """, (chat_id, name))
            self.conn.commit()

    # --------------- username helpers ---------------
    def set_username(self, chat_id: int | str, username: Optional[str]) -> None:
        cid = str(chat_id)
        uname = self._normalize_username(username)
        with self._locked_cursor() as cur:
            cur.execute("INSERT OR IGNORE INTO chats(chat_id, lang, feeds_history, first_seen, last_action) VALUES(?, ?, COALESCE(?, '[]'), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)", (cid, "en", json.dumps([])))
            cur.execute("UPDATE chats SET username = ? WHERE chat_id = ?", (uname, cid))

    def get_username(self, chat_id: int | str) -> Optional[str]:
        cid = str(chat_id)
        with self._locked_cursor() as cur:
            cur.execute("SELECT username FROM chats WHERE chat_id = ?", (cid,))
            r = cur.fetchone()
            return r["username"] if r else None

    def find_chat_by_username(self, username: str) -> Optional[str]:
        """
        Return chat_id (string) or None for given username (with/without @).
        Case-insensitive thanks to COLLATE NOCASE.
        """
        uname = self._normalize_username(username)
        if not uname:
            return None
        with self._locked_cursor() as cur:
            cur.execute("SELECT chat_id FROM chats WHERE username = ? LIMIT 1", (uname,))
            r = cur.fetchone()
            return r["chat_id"] if r else None

    # --------------- action / timestamps ---------------
    def mark_action(self, chat_id: int | str) -> None:
        """Update last_action to now (CURRENT_TIMESTAMP). Call this when user does something."""
        cid = str(chat_id)
        with self._locked_cursor() as cur:
            cur.execute("INSERT OR IGNORE INTO chats(chat_id, lang, feeds_history, first_seen) VALUES(?, ?, COALESCE(?, '[]'), CURRENT_TIMESTAMP)", (cid, "en", json.dumps([])))
            cur.execute("UPDATE chats SET last_action = CURRENT_TIMESTAMP WHERE chat_id = ?", (cid,))

    # ---------------- lang helpers ----------------
    def get_lang(self, chat_id: int | str) -> str:
        """زبان ذخیره شده برای کاربر (پیش‌فرض en)"""
        cid = str(chat_id)
        with self._locked_cursor() as cur:
            cur.execute("SELECT lang FROM chats WHERE chat_id = ?", (cid,))
            r = cur.fetchone()
            return r["lang"] if r and r["lang"] else "en"

    # --------------- iteration ---------------
    def iter_chats(self) -> List[Tuple[str, dict]]:
        with self._locked_cursor() as cur:
            cur.execute("SELECT chat_id, name, username, lang, first_seen, last_action, feeds_history FROM chats ORDER BY chat_id")
            rows = cur.fetchall()
            out: List[Tuple[str, dict]] = []
            for r in rows:
                cid = r["chat_id"]
                name = r["name"]
                username = r["username"]
                lang = r["lang"]
                first_seen = r["first_seen"]
                last_action = r["last_action"]
                try:
                    feeds_history = json.loads(r["feeds_history"] or "[]")
                except Exception:
                    feeds_history = []
                cur.execute("SELECT url FROM feeds WHERE chat_id = ? ORDER BY id", (cid,))
                feeds = [x["url"] for x in cur.fetchall()]
                cur.execute("SELECT feed_url, item_id FROM seen WHERE chat_id = ?", (cid,))
                seen_rows = cur.fetchall()
                seen: Dict[str, List[str]] = {}
                for s in seen_rows:
                    seen.setdefault(s["feed_url"], []).append(s["item_id"])
                out.append((cid, {"name": name, "username": username, "lang": lang, "first_seen": first_seen, "last_action": last_action, "feeds": feeds, "feeds_history": feeds_history, "seen": seen}))
            return out

    # --------------- utilities ---------------
    def close(self) -> None:
        try:
            self.conn.commit()
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass
        
    # ---------------------- KV Inserts----------------------
    def set_kv(self, chat_id: int, key: str, value: Any) -> None:
        """ذخیره یک مقدار (dict) به صورت JSON در جدول kv."""
        with self._locked_cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO kv(chat_id, key, value) VALUES(?, ?, ?)",
                (str(chat_id), key, json.dumps(value))
            )
            self.conn.commit()

    def get_kv(self, chat_id: int, key: str) -> Optional[Any]:
        """بازیابی یک مقدار از جدول kv و تبدیل آن به dict."""
        try:
            with self._locked_cursor() as cur:
                cur.execute(
                    "SELECT value FROM kv WHERE chat_id = ? AND key = ?",
                    (str(chat_id), key)
                )
                row = cur.fetchone()
                if row:
                    return json.loads(row[0])
                return None
        except (sqlite3.OperationalError, json.JSONDecodeError):
            return None
    
    def import_from_json_file(self, json_path: str) -> None:
        """Utility: import existing JSON state (same format as old StateStore)."""
        if not os.path.exists(json_path):
            raise FileNotFoundError(json_path)
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("json root must be a dict of chat_id -> data")

        with self._locked_cursor() as cur:
            for cid, st in data.items():
                name = st.get("name")
                lang = st.get("lang", "en")
                username = st.get("username", None)
                uname = self._normalize_username(username)
                first = st.get("first_seen")
                last = st.get("last_action")
                feeds_history = st.get("feeds_history") or []
                cur.execute(
                    "INSERT OR REPLACE INTO chats(chat_id, name, username, lang, first_seen, last_action, feeds_history) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (str(cid), name, uname, lang, first, last, json.dumps(feeds_history)),
                )
                feeds = st.get("feeds") or []
                for u in feeds:
                    cur.execute("INSERT OR IGNORE INTO feeds(chat_id, url) VALUES(?, ?)", (str(cid), str(u)))
                seen = st.get("seen") or {}
                for feed_url, items in seen.items():
                    for it in items or []:
                        cur.execute(
                            "INSERT OR IGNORE INTO seen(chat_id, feed_url, item_id) VALUES(?, ?, ?)",
                            (str(cid), str(feed_url), str(it)),
                        )

    # ---------------------- Keywords ----------------------
    def add_keyword(self, chat_id: str, keyword: str) -> None:
        """افزودن یک کلمه کلیدی برای کاربر"""
        cid = str(chat_id)
        kw = keyword.strip().lower()
        if not kw:
            return
        with self._locked_cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_keywords (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    keyword TEXT NOT NULL,
                    UNIQUE(chat_id, keyword)
                )
            """)
            cur.execute(
                "INSERT OR IGNORE INTO user_keywords (chat_id, keyword) VALUES (?, ?)",
                (cid, kw),
            )

    def list_keywords(self, chat_id: str) -> list[dict]:
        """لیست تمام کلمات کلیدی ثبت‌شده برای کاربر"""
        cid = str(chat_id)
        with self._locked_cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_keywords (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    keyword TEXT NOT NULL,
                    UNIQUE(chat_id, keyword)
                )
            """)
            cur.execute("SELECT id, keyword FROM user_keywords WHERE chat_id=? ORDER BY id", (cid,))
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def remove_keyword(self, chat_id: str, index: int) -> bool:
        """حذف کلمه بر اساس شماره در لیست کاربر"""
        cid = str(chat_id)
        with self._locked_cursor() as cur:
            cur.execute("SELECT id FROM user_keywords WHERE chat_id=? ORDER BY id", (cid,))
            rows = cur.fetchall()
            if index < 1 or index > len(rows):
                return False
            kid = rows[index - 1]["id"]
            cur.execute("DELETE FROM user_keywords WHERE id=?", (kid,))
            return True

    # log keywords feeds
    def log_keyword_event(self, chat_id: str, keyword: str, feed_url: str, item_id: str, ts: str):
        """ثبت ارسال یک پیام keyword-match"""
        with self._locked_cursor() as cur:
            cur.execute("""
                INSERT INTO keyword_events (chat_id, keyword, feed_url, item_id, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (str(chat_id), keyword.lower(), feed_url, item_id, ts))
