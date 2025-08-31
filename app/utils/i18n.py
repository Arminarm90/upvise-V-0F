# app/utils/i18n.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
import pathlib
from string import Template
from typing import Dict, Optional

_LOG = logging.getLogger("i18n")

# کش ترجمه‌ها: {"fa": {...}, "en": {...}}
_LOCALES: Dict[str, Dict] = {}

# ---- تلاش برای خواندن تنظیمات جهت مسیر و زبان پیش‌فرض
try:
    from app.config import settings  # type: ignore
    _DEFAULT_LOCALES_DIR = getattr(settings, "locales_dir", "app/i18n")
    _DEFAULT_LANG = (getattr(settings, "prompt_lang", "en") or "en")
except Exception:
    _DEFAULT_LOCALES_DIR = "app/i18n"
    _DEFAULT_LANG = "en"


def _norm_lang(code: Optional[str]) -> str:
    """نرمال‌سازی کد زبان."""
    code = (code or _DEFAULT_LANG or "en").strip().lower()
    if "-" in code:
        code = code.split("-", 1)[0]
    return code


# زبان پیش‌فرض داخلی (پس از نرمال‌سازی)
DEFAULT_LANG = _norm_lang(_DEFAULT_LANG)


def _resolved_dir(path: Optional[str]) -> pathlib.Path:
    """مسیر پوشهٔ ترجمه‌ها را به مسیر مطلق resolve می‌کند."""
    base = pathlib.Path(path or _DEFAULT_LOCALES_DIR)
    try:
        return base.resolve()
    except Exception:
        return base


def _load_file_json(p: pathlib.Path) -> Optional[Dict]:
    try:
        txt = p.read_text(encoding="utf-8")
        return json.loads(txt)
    except Exception as ex:
        _LOG.warning("i18n failed to load %s: %s", str(p), ex)
        return None


def load_locales(path: Optional[str] = None) -> None:
    """messages.<lang>.json ها را بارگذاری می‌کند."""
    _LOCALES.clear()
    base = _resolved_dir(path)
    try:
        files = sorted(base.glob("messages.*.json"))
    except Exception as ex:
        _LOG.error("i18n invalid path %s: %s", str(base), ex)
        files = []

    loaded_langs = []
    for f in files:
        try:
            parts = f.stem.split(".")
            lang = parts[1] if len(parts) >= 2 else ""
            lang = _norm_lang(lang)
        except Exception:
            lang = ""

        if not lang:
            _LOG.warning("i18n skip file without lang code: %s", str(f))
            continue

        data = _load_file_json(f)
        if data is None:
            continue

        _LOCALES[lang] = data
        loaded_langs.append(lang)

    if not _LOCALES:
        _LOG.error("i18n loaded 0 locales from %s", str(base))
    else:
        _LOG.info("i18n loaded locales: %s (from %s)", ",".join(sorted(set(loaded_langs))), str(base))


def _lazy_ensure_loaded() -> None:
    if _LOCALES:
        return
    try:
        load_locales(None)
    except Exception as ex:
        _LOG.error("i18n lazy load failed: %s", ex)


def t(key: str, lang: Optional[str] = None, **kwargs) -> str:
    """
    متن ترجمه با جایگذاری پارامترها. Fallback:
    lang → DEFAULT_LANG → en → key
    """
    _lazy_ensure_loaded()
    lang = _norm_lang(lang)
    layers = []
    if lang in _LOCALES:
        layers.append(_LOCALES[lang])
    if DEFAULT_LANG in _LOCALES and _LOCALES.get(DEFAULT_LANG) not in layers:
        layers.append(_LOCALES[DEFAULT_LANG])
    if "en" in _LOCALES and _LOCALES.get("en") not in layers:
        layers.append(_LOCALES["en"])

    s: Optional[str] = None
    for d in layers:
        s = d.get(key)
        if s:
            break

    if not s:
        _LOG.warning("i18n missing key: %s (%s)", key, lang)
        s = key

    try:
        return Template(s).safe_substitute(**kwargs)
    except Exception:
        return s


# ---- ذخیره/خواندن زبان هر چت در StateStore ----
def get_chat_lang(store, chat_id: int | str) -> str:
    try:
        st = store.get_chat(str(chat_id)) or {}
        raw = st.get("lang")
    except Exception:
        raw = None

    lang = _norm_lang(raw)
    if lang in _LOCALES:
        return lang
    if DEFAULT_LANG in _LOCALES:
        return DEFAULT_LANG
    if "en" in _LOCALES:
        return "en"
    return DEFAULT_LANG


def set_chat_lang(store, chat_id: int | str, lang: str) -> None:
    code = _norm_lang(lang)
    if code not in _LOCALES:
        if DEFAULT_LANG in _LOCALES:
            code = DEFAULT_LANG
        elif "en" in _LOCALES:
            code = "en"

    try:
        st = store.get_chat(str(chat_id)) or {}
    except Exception:
        st = {}
    st["lang"] = code

    try:
        if hasattr(store, "set_chat"):
            store.set_chat(str(chat_id), st)
        else:
            store[str(chat_id)] = st  # type: ignore
    except Exception as ex:
        _LOG.warning("i18n failed to persist lang for chat %s: %s", chat_id, ex)

    try:
        if hasattr(store, "save"):
            store.save()
    except Exception:
        pass
