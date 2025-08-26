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
        lang = data.get("lang", cur.get("lang"))
        feeds = list(data.get("feeds", cur.get("feeds", [])) or [])
        seen = dict(data.get("seen", cur.get("seen", {})) or {})
        self._state[cid] = {"lang": lang, "feeds": feeds, "seen": seen}
        self.save()

    def register_user(self, chat_id: int | str, account_name: str) -> None:
        cid = str(chat_id)
        # فقط در صورتی که کاربر وجود ندارد، اطلاعات را اضافه می‌کنیم
        if cid not in self._state:
            self._state[cid] = {
                "account_name": account_name,
                "lang": "fa", 
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
        # نیازی به تغییر نیست؛ فقط می‌خواند
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
