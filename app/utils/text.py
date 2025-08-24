# app/utils/text.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from typing import Iterable, List, Tuple, Optional
from urllib.parse import (
    urlparse,
    urlunparse,
    urlsplit,
    urlunsplit,
    parse_qsl,
    urlencode,
)

# ----------------------------- Basics ----------------------------- #

def ensure_scheme(u: str) -> str:
    """
    اگر ورودی scheme نداشت، https:// اضافه می‌کند.
    - ورودی‌های protocol-relative مثل //example.com → https://example.com
    - اگر scheme دیگری غیر از http/https داشت، همان را برمی‌گرداند (تغییری نمی‌دهد).
    """
    u = (u or "").strip()
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.startswith(("http://", "https://")):
        return u
    # اگر به‌نظر می‌رسد scheme دیگری دارد (مثل mailto:), دست نمی‌زنیم
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+\-.]*:", u):
        return u
    return f"https://{u}"


def root_url(url: str) -> str:
    """
    ریشهٔ URL (scheme + netloc) را برمی‌گرداند؛ اگر پارس نامعتبر بود، همان ورودی برگردانده می‌شود.
    """
    try:
        p = urlparse(ensure_scheme(url))
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return url


def short(s: str, n: int = 42) -> str:
    """
    کوتاه‌سازی امن رشته با «...».
    """
    if not s:
        return s
    return s if len(s) <= n else s[: n - 3] + "..."


def html_escape(s: str) -> str:
    """
    Escape حداقلی برای متن (نه برای مقدار خصیصهٔ HTML).
    برای استفاده داخل بدنهٔ HTML/تلگرام مناسب است.
    """
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def html_attr_escape(s: str) -> str:
    """
    Escape مخصوص مقدار خصیصه‌های HTML (مثل داخل href="...").
    """
    s = html_escape(s or "")
    s = s.replace('"', "&quot;").replace("'", "&#39;")
    return s

# ----------------------- URL Canonicalization ---------------------- #

# مجموعهٔ پارامترهای ردیابی/تبلیغاتی که باید حذف شوند (case-insensitive)
_TRACKER_PARAMS_DEFAULT = {
    # UTM family
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id", "utm_name",
    # Click IDs
    "gclid", "fbclid", "msclkid", "gbraid", "wbraid",
    # Mail/CRM
    "mc_eid", "mc_cid",
    # Piwik/Matomo
    "pk_campaign", "pk_kwd",
    # Generic/refs
    "ref", "ref_src", "igshid", "yclid",
}

def _drop_default_port(scheme: str, netloc: str) -> str:
    """
    اگر پورت پیش‌فرض باشد (http:80 / https:443) آن را حذف می‌کند.
    """
    if ":" not in netloc:
        return netloc
    host, _, port = netloc.rpartition(":")
    try:
        p = int(port)
    except ValueError:
        return netloc
    if (scheme == "http" and p == 80) or (scheme == "https" and p == 443):
        return host
    return netloc


def clean_query_params(
    url: str,
    drop_known_trackers: bool = True,
    extra_drop: Optional[Iterable[str]] = None,
) -> str:
    """
    پارامترهای ردیابی/تبلیغاتی را از query حذف می‌کند و query را مرتب می‌سازد.
    - drop_known_trackers: اگر True باشد، مجموعهٔ پیش‌فرض حذف می‌شود.
    - extra_drop: مجموعهٔ اضافی پارامترهایی که باید حذف شوند (case-insensitive).
    """
    if not url:
        return ""

    s = urlsplit(ensure_scheme(url))
    q_items = parse_qsl(s.query, keep_blank_values=False)

    drop_set = set()
    if drop_known_trackers:
        drop_set |= {k.lower() for k in _TRACKER_PARAMS_DEFAULT}
    if extra_drop:
        drop_set |= {str(k).lower() for k in extra_drop}

    new_q = [(k, v) for (k, v) in q_items if k and k.lower() not in drop_set]
    # مرتب‌سازی پایدار الفبایی برای یکتاسازی بهتر
    new_q.sort(key=lambda kv: (kv[0].lower(), kv[1]))

    return urlunsplit((s.scheme, s.netloc, s.path, urlencode(new_q, doseq=True), s.fragment))


def canonicalize_url(
    url: str,
    drop_params: Optional[Iterable[str]] = None,
    strip_fragment: bool = True,
    strip_www: bool = False,
    remove_trailing_slash: bool = True,
) -> str:
    """
    نرمال‌سازی URL برای یکتاسازی و جلوگیری از تکراری‌ها:
      - افزودن scheme در صورت فقدان
      - lowercase کردن host و حذف پورت پیش‌فرض
      - حذف پارام‌های ردیابی (معمول + drop_params)
      - مرتب‌سازی query
      - حذف fragment (اختیاری)
      - حذف www. (اختیاری)
      - حذف اسلش انتهایی (اختیاری؛ اگر path فقط "/" نباشد)

    مثال:
      https://EXAMPLE.com:443/News/Article/?utm_source=x&b=2&a=1#frag
      → https://example.com/News/Article/?a=1&b=2
    """
    if not url:
        return ""

    s = urlsplit(ensure_scheme(url))

    # host normalization
    host = (s.netloc or "").strip()
    if strip_www and host.lower().startswith("www."):
        host = host[4:]
    host = host.lower()
    host = _drop_default_port(s.scheme.lower(), host)

    # path normalization: حذف اسلش انتهایی اگر path غیر ریشه است
    path = s.path or ""
    if remove_trailing_slash and path.endswith("/") and path != "/":
        path = path[:-1]

    # query normalization: حذف پارام‌های ردیابی و مرتب‌سازی
    drop = set(k.lower() for k in (drop_params or []))
    q_items = parse_qsl(s.query, keep_blank_values=False)
    cleaned = []
    for k, v in q_items:
        if not k:
            continue
        kl = k.lower()
        if kl in _TRACKER_PARAMS_DEFAULT or kl in drop:
            continue
        cleaned.append((k, v))
    cleaned.sort(key=lambda kv: (kv[0].lower(), kv[1]))
    q = urlencode(cleaned, doseq=True)

    # fragment
    frag = "" if strip_fragment else (s.fragment or "")

    return urlunsplit((s.scheme.lower(), host, path, q, frag))

# ----------------------- Optional Safety Helper -------------------- #

_PRIVATE_HOST_RE = re.compile(
    r"^(?:"
    r"localhost"
    r"|127(?:\.\d{1,3}){3}"
    r"|10(?:\.\d{1,3}){3}"
    r"|192\.168(?:\.\d{1,3}){2}"
    r"|172\.(?:1[6-9]|2\d|3[0-1])(?:\.\d{1,3}){2}"
    r"|::1"
    r"|fe80::"
    r")$",
    flags=re.I,
)

def is_public_http_url(u: str) -> bool:
    """
    چک سریع: فقط http/https و نه روی هاست‌های خصوصی/لوپ‌بک.
    (برای گارد سبک در لایه‌های بالاتر مفید است.)
    """
    try:
        p = urlparse(ensure_scheme(u))
        if p.scheme not in ("http", "https") or not p.netloc:
            return False
        host = p.netloc.split(":")[0].strip().lower()
        return not bool(_PRIVATE_HOST_RE.match(host))
    except Exception:
        return False
