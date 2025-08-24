# app/utils/postprocess.py
import re

_CLICHE_TLDR = [
    r"^(این\s*مطلب|این\s*مقاله)\s+به\s+.+?می‌پردازد[\.،]?\s*",
    r"^(در\s*این\s*مقاله|نویسنده\s+می‌گوید|بیان\s+می‌کند)\s*[:：]?\s*",
]

def tidy_tldr(t: str, title: str = "") -> str:
    t = (t or "").strip()
    for pat in _CLICHE_TLDR:
        t = re.sub(pat, "", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip()
    if title and title.lower() in t.lower():
        t = re.sub(re.escape(title), "", t, flags=re.I).strip(" .،")
    return t

def tidy_bullets(bullets, max_n=8):
    seen, out = set(), []
    for b in bullets or []:
        b = (b or "").strip()
        b = re.sub(r"^[•\-–—\u2022\*\s]+", "", b)
        b = re.sub(r"\s+", " ", b)
        if len(b) < 8:
            continue
        key = b.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(b)
        if len(out) >= max_n:
            break
    return out
