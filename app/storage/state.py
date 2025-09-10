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

    def _init_schema(self) -> None:
        with self._locked_cursor() as cur:
            # PRAGMAs for better concurrency and safety
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA foreign_keys=ON;")
            # chats table includes username (unique, case-insensitive)
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
            # index for username for fast lookup (redundant if UNIQUE exists but fine)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_chats_username ON chats(username);")
            # feeds table
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
            # seen table: one row per seen item
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
            self.conn.commit()

    # --------------- helpers ---------------
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
                        # commit only when no exception
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

    # --------------- core chat operations ---------------
    def register_user(self, chat_id: int | str, name: str, username: Optional[str] = None, lang: str = "en") -> None:
        """Insert chat row if missing. Optionally store username and lang."""
        cid = str(chat_id)
        uname = self._normalize_username(username)
        with self._locked_cursor() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO chats(chat_id, name, username, lang) VALUES(?, ?, ?, ?)",
                (cid, name or None, uname, lang or "en"),
            )

    def get_chat(self, chat_id: int | str) -> dict:
        cid = str(chat_id)
        with self._locked_cursor() as cur:
            cur.execute("SELECT chat_id, name, username, lang FROM chats WHERE chat_id = ?", (cid,))
            row = cur.fetchone()
            if not row:
                return {}
            name = row["name"]
            username = row["username"]
            lang = row["lang"]
            # feeds
            cur.execute("SELECT url FROM feeds WHERE chat_id = ? ORDER BY id", (cid,))
            feeds = [r["url"] for r in cur.fetchall()]
            # seen mapping
            cur.execute("SELECT feed_url, item_id FROM seen WHERE chat_id = ?", (cid,))
            seen_rows = cur.fetchall()
            seen: Dict[str, List[str]] = {}
            for r in seen_rows:
                seen.setdefault(r["feed_url"], []).append(r["item_id"])
            return {"name": name, "username": username, "lang": lang, "feeds": feeds, "seen": seen}

    def set_chat(self, chat_id: int | str, data: dict) -> None:
        """
        Upsert chat fields. data may contain name, username, lang, feeds, seen.
        This will overwrite feeds/seen according to provided values (if present) and keep others.
        """
        cid = str(chat_id)
        name = data.get("name")
        username = data.get("username")
        lang = data.get("lang")
        feeds_in = data.get("feeds")
        seen_in = data.get("seen")

        uname = self._normalize_username(username) if username is not None else None

        with self._locked_cursor() as cur:
            # ensure chat row exists / upsert name/lang/username if provided
            # Note: use COALESCE to keep existing values when None is passed
            cur.execute(
                """
                INSERT INTO chats(chat_id, name, username, lang)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                  name = COALESCE(?, name),
                  username = COALESCE(?, username),
                  lang = COALESCE(?, lang)
                """,
                (cid, name or None, uname, lang or None, name, uname, lang),
            )

            # update feeds if provided
            if feeds_in is not None:
                feeds = list(dict.fromkeys([str(u) for u in (feeds_in or [])]))
                # delete feeds not in list
                if feeds:
                    placeholders = ",".join("?" for _ in feeds)
                    cur.execute(
                        f"DELETE FROM feeds WHERE chat_id = ? AND url NOT IN ({placeholders})",
                        tuple([cid] + feeds),
                    )
                else:
                    cur.execute("DELETE FROM feeds WHERE chat_id = ?", (cid,))
                # insert or ignore remaining
                for u in feeds:
                    cur.execute("INSERT OR IGNORE INTO feeds(chat_id, url) VALUES(?, ?)", (cid, u))

            # update seen if provided: replace feed-specific seen sets
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
            cur.execute("INSERT OR IGNORE INTO chats(chat_id, lang) VALUES(?, ?)", (cid, "en"))
            try:
                cur.execute("INSERT INTO feeds(chat_id, url) VALUES(?, ?)", (cid, u))
                return True
            except sqlite3.IntegrityError:
                return False

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
            cur.execute("DELETE FROM feeds WHERE chat_id = ?", (cid,))
            cur.execute("DELETE FROM seen WHERE chat_id = ?", (cid,))
            return True

    # --------------- seen operations ---------------
    def get_seen(self, chat_id: int | str, url: str) -> set:
        cid = str(chat_id)
        u = str(url)
        with self._locked_cursor() as cur:
            cur.execute("SELECT item_id FROM seen WHERE chat_id = ? AND feed_url = ? ORDER BY id", (cid, u))
            return set([row["item_id"] for row in cur.fetchall()])

    def set_seen(self, chat_id: int | str, url: str, seen_set: Iterable[str]) -> None:
        cid = str(chat_id)
        u = str(url)
        with self._locked_cursor() as cur:
            cur.execute("INSERT OR IGNORE INTO chats(chat_id, lang) VALUES(?, ?)", (cid, "en"))
            cur.execute("INSERT OR IGNORE INTO feeds(chat_id, url) VALUES(?, ?)", (cid, u))
            cur.execute("DELETE FROM seen WHERE chat_id = ? AND feed_url = ?", (cid, u))
            for it in (seen_set or []):
                cur.execute(
                    "INSERT OR IGNORE INTO seen(chat_id, feed_url, item_id) VALUES(?, ?, ?)",
                    (cid, u, str(it)),
                )

    # --------------- username helpers ---------------
    def set_username(self, chat_id: int | str, username: Optional[str]) -> None:
        cid = str(chat_id)
        uname = self._normalize_username(username)
        with self._locked_cursor() as cur:
            cur.execute("INSERT OR IGNORE INTO chats(chat_id, lang) VALUES(?, ?)", (cid, "en"))
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

    # --------------- iteration ---------------
    def iter_chats(self) -> List[Tuple[str, dict]]:
        with self._locked_cursor() as cur:
            cur.execute("SELECT chat_id, name, username, lang FROM chats ORDER BY chat_id")
            rows = cur.fetchall()
            out: List[Tuple[str, dict]] = []
            for r in rows:
                cid = r["chat_id"]
                name = r["name"]
                username = r["username"]
                lang = r["lang"]
                cur.execute("SELECT url FROM feeds WHERE chat_id = ? ORDER BY id", (cid,))
                feeds = [x["url"] for x in cur.fetchall()]
                cur.execute("SELECT feed_url, item_id FROM seen WHERE chat_id = ?", (cid,))
                seen_rows = cur.fetchall()
                seen: Dict[str, List[str]] = {}
                for s in seen_rows:
                    seen.setdefault(s["feed_url"], []).append(s["item_id"])
                out.append((cid, {"name": name, "username": username, "lang": lang, "feeds": feeds, "seen": seen}))
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
                cur.execute(
                    "INSERT OR REPLACE INTO chats(chat_id, name, username, lang) VALUES(?, ?, ?, ?)",
                    (str(cid), name, uname, lang),
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
