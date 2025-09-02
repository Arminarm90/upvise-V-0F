import time, humanize
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, quote
from persiantools.jdatetime import JalaliDateTime

async def process_google_trends(feed, store, cid_int: int, url: str) -> str | None:
    """پردازش اختصاصی فید گوگل ترندز (۱۰تایی + قالب خاص)"""
    if not feed or not getattr(feed, "entries", None):
        return None

    entries = feed.entries
    seen = set(store.get_seen(cid_int, url))

    # استخراج new trends
    new_terms = []
    for e in entries:
        eid = f"trend:{getattr(e, 'title', '')}"
        if eid not in seen and getattr(e, "title", None):
            new_terms.append({"id": eid, "entry": e})

    # اگه هیچ new entry نیست → چیزی نفرست
    if not new_terms:
        return None

    # combined list (جدید + قدیمی)
    all_terms = new_terms + [
        {"id": f"trend:{getattr(e, 'title', '')}", "entry": e}
        for e in entries
        if f"trend:{getattr(e, 'title', '')}" in seen
    ]
    final_terms = all_terms[:10]

    # ذخیره در seen
    for t in new_terms:
        seen.add(t["id"])
    store.set_seen(cid_int, url, seen)

    # کشور از geo پارامتر
    qs = parse_qs(urlparse(url).query)
    geo = qs.get("geo", [""])[0] or "Global"

    now = datetime.now(timezone.utc)
    if geo.upper() == "IR":
        date_str = JalaliDateTime(now).strftime("%d %B %Y")
    else:
        date_str = now.strftime("%d %B %Y")

    header = f"📊 Top search trends in {geo} (past 24h)\n{date_str} | Google Trends\n\n"

    lines = []
    for idx, t in enumerate(final_terms, 1):
        e = t["entry"]
        title = e.title
        search_url = f"https://trends.google.com/trends/explore?q={quote(title)}&geo={geo}"

        # زمان
        published = getattr(e, "published_parsed", None)
        if published:
            dt = datetime.fromtimestamp(time.mktime(published), tz=timezone.utc)
            age = humanize.naturaltime(now - dt)
        else:
            age = "recently"

        # تعداد سرچ‌ها
        approx = getattr(e, "ht_approx_traffic", "") or getattr(e, "approx_traffic", "")
        approx = approx.replace("+", "+ ") if approx else ""
        searches = f"{approx} searches" if approx else "—"

        lines.append(
            f"{idx}) <a href='{search_url}'>{title}</a>\n{searches} • ↗️ Active • {age}"
        )

    return header + "\n\n".join(lines)


