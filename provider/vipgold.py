import os, re, time, logging, requests, pytz, httpx, hashlib
from bs4 import BeautifulSoup
from datetime import datetime
import jdatetime, feedparser
from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import urlparse, urlunparse, parse_qsl
import asyncio
import json
import hashlib
from typing import Optional, List, Dict, Any

# =========================
# Meta / Version
# =========================
APP_NAME, APP_VER = "VIPGoldBot", "2.0.0"

# =========================
# Logging
# =========================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("goldbot")

# =========================
# Config (Gold)
# =========================
IR_TZ = pytz.timezone("Asia/Tehran")
UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
    "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
}
MAIN_URL = "https://www.tgju.org/gold-chart"
PROFILE_URLS = {
    "gram18":  "https://www.tgju.org/profile/geram18",
    "gram24":  "https://www.tgju.org/profile/geram24",
    "second":  "https://www.tgju.org/profile/gold_mini_size",
    "mesghal": "https://www.tgju.org/profile/mesghal",
    "sekke":   "https://www.tgju.org/profile/sekee",
    "ounce":   "https://www.tgju.org/profile/ons",
}
SLUG_MAP = {
    "geram18": "gram18",
    "geram24": "gram24",
    "gold_mini_size": "second",
    "mesghal": "mesghal",
    "sekee": "sekke",
    "ons": "ounce",
}
TIMEOUT, RETRIES, BACKOFF, CACHE_TTL = 10, 2, 0.6, 60

def make_session():
    s = requests.Session()
    retry = Retry(total=RETRIES, connect=RETRIES, read=RETRIES, backoff_factor=BACKOFF,
                  status_forcelist=(429,500,502,503,504), allowed_methods=frozenset(["GET"]))
    ad = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
    s.mount("https://", ad); s.mount("http://", ad)
    s.headers.update(UA)
    return s
SESSION = make_session()

# =========================
# Utils (Gold)
# =========================
DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
def _normalize(s:str)->str:
    return (s or "").translate(DIGIT_MAP)\
        .replace('٫','.')\
        .replace('٬',',')\
        .replace('\u066C',',')\
        .replace('،',',')\
        .replace('\u00A0','')\
        .replace('\u2009','')\
        .replace('\u202F','')\
        .strip()
num_re = re.compile(r'[-+]?\d{1,3}(?:,\d{3})*(?:\.\d+)?')
pct_re = re.compile(r'([-+]?\s*\d+(?:\.\d+)?)\s*[%٪]')
def _to_int(txt):  m=num_re.search(_normalize(txt)); return int(round(float(m.group(0).replace(',','')))) if m else None
def _to_float_pct(txt): m=pct_re.search(_normalize(txt)); return float(m.group(1).replace(' ','')) if m else None
PERSIAN_MONTHS = ["فروردین","اردیبهشت","خرداد","تیر","مرداد","شهریور","مهر","آبان","آذر","دی","بهمن","اسفند"]
def fmt_datetime_now():
    now = datetime.now(IR_TZ)
    jnow = jdatetime.datetime.fromgregorian(datetime=now)
    return f"{jnow.day:02d} {PERSIAN_MONTHS[jnow.month-1]} {jnow.year}", now.strftime("%H:%M:%S")
def fmt_int(n): return f"{int(n):,}"
def fmt_pct(x): return f" ({x:+0.2f}%)" if x is not None else ""

# =========================
# GOLD — Fetchers
# =========================
def fetch_gold_chart(url=MAIN_URL):
    log.info("GOLD.AGG: fetch %s", url)
    try:
        r = SESSION.get(url, timeout=TIMEOUT); r.raise_for_status()
    except Exception as e:
        log.error("GOLD.AGG: request failed: %s", e); return {}
    soup = BeautifulSoup(r.text, "lxml")
    result = {}
    for tr in soup.select("table tr"):
        a = tr.select_one("a[href]"); 
        if not a: continue
        href = a.get("href","")
        slug = next((SLUG_MAP[k] for k in SLUG_MAP if k in href), None)
        if not slug: continue
        row_text = " ".join(td.get_text(" ", strip=True) for td in tr.select("td"))
        price, change = _to_int(row_text), _to_float_pct(row_text)
        result[slug] = {"price": price, "change": change}
        log.debug("GOLD.AGG: row %-7s -> price=%s change=%s", slug, price, change)
    log.info("GOLD.AGG: parsed keys=%s", list(result.keys()))
    return result

def _extract_by_label(soup, pattern):
    for node in soup.find_all(string=re.compile(pattern)):
        row = node.find_parent(["tr","div","li","section"]) or node.parent
        if not row: continue
        txt = row.get_text(" ", strip=True)
        if txt: return _normalize(txt)
    return ""

def fetch_profile(url):
    log.info("GOLD.PROFILE: fetch %s", url)
    try:
        r = SESSION.get(url, timeout=TIMEOUT); r.raise_for_status()
    except Exception as e:
        log.error("GOLD.PROFILE: request failed: %s", e); return None, None
    soup = BeautifulSoup(r.text, "lxml")
    price = _to_int(_extract_by_label(soup, r"نرخ\s*فعلی") or soup.get_text(" ", strip=True))
    change = _to_float_pct(_extract_by_label(soup, r"درصد\s*تغییر") or soup.get_text(" ", strip=True))
    log.info("GOLD.PROFILE: parsed -> price=%s change=%s", price, change)
    return price, change

# Cache
_last_data, _last_ts, _last_meta = None, 0.0, {}
def _cache_get():
    global _last_data, _last_ts
    if _last_data and (time.time()-_last_ts) < CACHE_TTL:
        log.info("GOLD.CACHE: hit (age=%.1fs)", time.time()-_last_ts)
        return _last_data
    log.info("GOLD.CACHE: miss")
    return None
def _cache_set(d, meta=None):
    global _last_data, _last_ts, _last_meta
    _last_data, _last_ts, _last_meta = d, time.time(), (meta or {})
def _cache_meta():
    return (time.time()-_last_ts if _last_ts else None), _last_meta

def collect_gold():
    cached = _cache_get()
    if cached: return cached
    data = {f"{k}_{t}":None for k in ["gram18","gram24","second","mesghal","sekke","ounce"] for t in ["price","change"]}
    agg = fetch_gold_chart(); meta = {"agg_keys": list(agg.keys()), "fallback_keys": []}
    for k in ["gram18","gram24","second","mesghal","sekke","ounce"]:
        if k in agg:
            data[f"{k}_price"], data[f"{k}_change"] = agg[k]["price"], agg[k]["change"]
    for k,u in PROFILE_URLS.items():
        if data[f"{k}_price"] is None or data[f"{k}_change"] is None:
            p,c = fetch_profile(u)
            if data[f"{k}_price"] is None and p is not None:  data[f"{k}_price"]=p
            if data[f"{k}_change"] is None and c is not None: data[f"{k}_change"]=c
            meta["fallback_keys"].append(k)
    log.debug("GOLD.DATA: %s", data)
    _cache_set(data, meta)
    return data

def build_gold_msg(d):
    date_str, time_str = fmt_datetime_now()

    def fmt_line(label, price, change, unit="ریال"):
        if not price:
            return f"**{label}**: —"

        # تشخیص مثبت/منفی بر اساس عدد
        arrow = ""
        if isinstance(change, (int, float)):
            if change > 0:
                arrow = "⬆️"
            elif change < 0:
                arrow = "⬇️"
        else:
            ch_str = str(change or "")
            if ch_str.startswith("+"):
                arrow = "⬆️"
            elif ch_str.startswith("-"):
                arrow = "⬇️"

        return f"**{label}**: {fmt_int(price)} {unit} {arrow}{fmt_pct(change)}"


    lines = [
        "*📊 بازار طلا | امروز*",
        f"_{date_str} | {time_str}_",
        "",
        fmt_line("*طلا ۱۸ عیار*", d["gram18_price"], d["gram18_change"]),
        fmt_line("*طلا ۲۴ عیار*", d["gram24_price"], d["gram24_change"]),
        fmt_line("*طلا دست دوم*", d["second_price"], d["second_change"]),
        fmt_line("*مثقال طلا*", d["mesghal_price"], d["mesghal_change"]),
        fmt_line("*سکه امامی*", d["sekke_price"], d["sekke_change"]),
        fmt_line("*اونس جهانی*", d["ounce_price"], d["ounce_change"], unit="دلار"),
        ""
    ]
    return "\n".join(lines)


# =========================
# NEWS — Config & Scoring
# =========================
NEWS_KEYWORDS = {
    "policy": {"weight":5,"words":["fed","fomc","interest rate","rate cut","tightening","easing","qe","tapering","central bank","ecb","boj","monetary policy","bond yields","real yields"]},
    "economy":{"weight":4,"words":["inflation","cpi","ppi","jobs report","unemployment","gdp","recession","dollar index","dxy","treasury yields","ism","retail sales"]},
    "demand":{"weight":3,"words":["gold purchases","gold reserves","china demand","india demand","jewelry demand","etf flows","spdr gold"]},
    "geopolitics":{"weight":3,"words":["war","conflict","sanctions","middle east","ukraine","us-china","financial crisis","banking collapse","market turmoil"]},
    "commodities":{"weight":2,"words":["commodity rally","silver/gold ratio","mining supply","safe haven demand","metals rally"]},
    "general":{"weight":1,"words":["analysis","forecast","long-term","outlook","commentary","educational"]},
}

# محدودیت‌ها و آستانه‌ها
NEWS_PER_SOURCE_MAX = int(os.getenv("NEWS_PER_SOURCE_MAX", "2"))
NEWS_MIN_SCORE      = int(os.getenv("NEWS_MIN_SCORE", "2"))  # حداقل امتیاز خبر

# اولویت 1..10 — RSS واقعی یا HTML صفحه خبری
NEWS_FEEDS = [
    ("World Gold Council", "https://www.gold.org/news"),
    ("Reuters",             "https://www.reuters.com/markets/gold/"),
    ("Kitco",               "https://www.kitco.com/news/"),
    ("Investing",           "https://www.investing.com/commodities/gold-news"),
    ("TradingEconomics",    "https://tradingeconomics.com/rss"),
    ("BullionVault",        "https://www.bullionvault.com/gold-news"),
    ("Money Metals",        "https://www.moneymetals.com/news"),
    ("FGMR",                "https://www.fgmr.com"),
    ("GoldSeek",            "https://news.goldseek.com/newsRSS.xml"),
    ("Feedspot",            "https://rss.feedspot.com/gold_rss_feeds"),
]

# ---- HTTP + RSS/HTML detection ----
UA_HDRS = {"User-Agent": UA["User-Agent"], "Accept-Language": UA["Accept-Language"]}
def _http_get(url, timeout=10):
    return httpx.get(url, headers=UA_HDRS, timeout=timeout, follow_redirects=True)

def _is_rss(text, ctype):
    if ctype and ("xml" in ctype or "rss" in ctype or "atom" in ctype):
        return True
    t = (text or "").lstrip()
    return t.startswith("<?xml") or "<rss" in t or "<feed" in t

# ---- Site-specific HTML parsers (gold-focused) ----
def parse_reuters(html, base_url, limit=6):
    soup = BeautifulSoup(html, "lxml"); items=[]
    for a in soup.select('h3 a[href], a[href*="/markets/"]'):
        h = a.get_text(strip=True); href = a.get("href","")
        if not h or not href or len(h)<15: continue
        link = href if href.startswith("http") else "https://www.reuters.com"+href
        txt = h.lower()
        if ("gold" not in txt) and ("bullion" not in txt) and ("/gold" not in link):
            continue
        items.append({"title": h, "link": link, "summary": ""})
        if len(items)>=limit: break
    return items

def parse_investing(html, base_url, limit=6):
    soup = BeautifulSoup(html, "lxml"); items=[]
    for a in soup.select('h3 a[href], a[href*="/news/"], a[href*="/commodities/"]'):
        h=a.get_text(strip=True); href=a.get("href","")
        if not h or not href or len(h)<15: continue
        link = href if href.startswith("http") else "https://www.investing.com"+href
        if "gold" not in h.lower() and "/gold" not in link:
            continue
        items.append({"title":h,"link":link,"summary":""})
        if len(items)>=limit: break
    return items

def parse_goldorg(html, base_url, limit=6):
    soup=BeautifulSoup(html,"lxml"); items=[]
    for a in soup.select('article a[href*="/news/"], article a[href*="/insights/"], h2 a[href], h3 a[href]'):
        h=a.get_text(strip=True); href=a.get("href","")
        if not h or not href or len(h)<15: continue
        link = href if href.startswith("http") else "https://www.gold.org"+href
        if "gold" not in h.lower() and "/gold-" not in link:
            continue
        items.append({"title":h,"link":link,"summary":""})
        if len(items)>=limit: break
    return items

def parse_bullionvault(html, base_url, limit=6):
    soup=BeautifulSoup(html,"lxml"); items=[]
    for a in soup.select('a[href*="/gold-news/"], h2 a'):
        h=a.get_text(strip=True); href=a.get("href","")
        if not h or not href or len(h)<20: continue
        link = href if href.startswith("http") else "https://www.bullionvault.com"+href
        items.append({"title":h,"link":link,"summary":""})
        if len(items)>=limit: break
    return items

def parse_moneymetals(html, base_url, limit=6):
    soup=BeautifulSoup(html,"lxml"); items=[]
    for a in soup.select('h2 a, .news-list a, article h2 a'):
        h=a.get_text(strip=True); href=a.get("href","")
        if not h or not href or len(h)<20: continue
        link = href if href.startswith("http") else "https://www.moneymetals.com"+href
        items.append({"title":h,"link":link,"summary":""})
        if len(items)>=limit: break
    return items

def parse_fgmr(html, base_url, limit=6):
    soup=BeautifulSoup(html,"lxml"); items=[]
    for a in soup.select('h2 a, article a[href]'):
        h=a.get_text(strip=True); href=a.get("href","")
        if not h or not href or len(h)<20: continue
        link = href if href.startswith("http") else "https://www.fgmr.com"+href
        items.append({"title":h,"link":link,"summary":""})
        if len(items)>=limit: break
    return items

def parse_kitco(html, base_url, limit=6):
    soup = BeautifulSoup(html, "lxml"); items=[]
    for a in soup.select('h3 a[href*="/news/"], h2 a[href*="/news/"], a[href*="/news/"]'):
        h=a.get_text(strip=True); href=a.get("href","")
        if not h or not href or len(h)<15: continue
        link = href if href.startswith("http") else "https://www.kitco.com"+href
        if "gold" not in h.lower() and "/gold-" not in link:
            continue
        items.append({"title":h,"link":link,"summary":""})
        if len(items)>=limit: break
    return items

HTML_PARSERS = {
    "Reuters": parse_reuters,
    "Investing": parse_investing,
    "World Gold Council": parse_goldorg,
    "BullionVault": parse_bullionvault,
    "Money Metals": parse_moneymetals,
    "FGMR": parse_fgmr,
    "Kitco": parse_kitco,
}

# --------------------------
# Dedup helpers & scoring
# --------------------------
RECENT_NEWS = {}          # key -> timestamp
RECENT_TTL  = 60 * 60     # 1 hour

def _canonical_url(u:str)->str:
    try:
        p = urlparse(u)
        qs = [(k,v) for k,v in parse_qsl(p.query, keep_blank_values=True)
              if k.lower() not in {"utm_source","utm_medium","utm_campaign","utm_term","utm_content","ref"}]
        p = p._replace(query="&".join(f"{k}={v}" for k,v in qs), fragment="")
        return urlunparse(p)
    except Exception:
        return u

def _norm_title(t:str)->str:
    t = re.sub(r"\s+", " ", t or "").strip().lower()
    t = re.sub(r"[^\w\s]", "", t)
    return t

def _make_key(title:str, link:str)->str:
    base = _norm_title(title) + "||" + _canonical_url(link)
    return hashlib.md5(base.encode("utf-8")).hexdigest()

def _recent_ok(key:str)->bool:
    now = time.time()
    for k,ts in list(RECENT_NEWS.items()):
        if now - ts > RECENT_TTL:
            RECENT_NEWS.pop(k, None)
    return key not in RECENT_NEWS

def _remember(key:str):
    RECENT_NEWS[key] = time.time()

def _mdv2_escape(text:str)->str:
    specials = r"\_*[]()~`>#+-=|{}.!"
    for ch in specials:
        text = text.replace(ch, "\\"+ch)
    return text

def _score(title, summary=""):
    t = f"{title} {summary}".lower()
    score = 0
    for _, data in NEWS_KEYWORDS.items():
        if any(w in t for w in data["words"]):
            score += data["weight"]
    return score

def _short(s:str, n:int=90)->str:
    s = s or ""
    return (s[:n] + "…") if len(s) > n else s

def _fetch_feed(source, url, limit=6):
    log.info("NEWS: fetch [%s] %s", source, url)
    try:
        r = _http_get(url, timeout=10)

        # تلاش با feedparser حتی اگر هدر RSS نبود
        fp = feedparser.parse(r.text)
        entries = getattr(fp, "entries", [])[:limit]
        if entries:
            log.info("NEWS: [%s] feedparser entries=%d", source, len(entries))
            return [{"title":getattr(e,"title",""), "link":getattr(e,"link",""), "summary":getattr(e,"summary","")} for e in entries]

        # HTML parser اختصاصی
        parser = HTML_PARSERS.get(source)
        if parser:
            items = parser(r.text, url, limit=limit)
            log.info("NEWS: [%s] HTML items=%d", source, len(items))
            return items

        log.info("NEWS: [%s] no usable content", source)
        return []
    except Exception as e:
        log.error("NEWS: feed failed for %s: %s", source, e)
        return []

def _collect_news_from_range(feeds_slice, want_max=4):
    picked = []
    seen_titles = set()
    seen_links  = set()
    per_source  = {}
    stats = {
        "total_entries": 0,
        "dup_title": 0,
        "dup_link": 0,
        "dup_recent": 0,
        "score_zero": 0,
        "per_source_cap": 0,
    }

    for source, url in feeds_slice:
        entries = _fetch_feed(source, url)
        stats["total_entries"] += len(entries)

        for e in entries:
            title = (e.get("title") or "").strip()
            link  = _canonical_url((e.get("link") or "").strip())
            if not title or not link:
                continue

            nt = _norm_title(title)
            if nt in seen_titles:
                stats["dup_title"] += 1
                log.warning("NEWS.DEDUP: duplicate TITLE skipped | src=%s | %s", source, _short(title))
                continue

            if link in seen_links:
                stats["dup_link"] += 1
                log.warning("NEWS.DEDUP: duplicate LINK skipped  | src=%s | %s", source, _short(link))
                continue

            sc = _score(title, e.get("summary",""))
            if sc < NEWS_MIN_SCORE:
                stats["score_zero"] += 1
                log.info("NEWS.SCORE: score<%d skipped | src=%s | %s", NEWS_MIN_SCORE, source, _short(title))
                continue

            per_source.setdefault(source, 0)
            if per_source[source] >= NEWS_PER_SOURCE_MAX:
                stats["per_source_cap"] += 1
                log.info("NEWS.CAP: per-source limit reached | src=%s | %s", source, _short(title))
                continue

            key = _make_key(title, link)
            if not _recent_ok(key):
                stats["dup_recent"] += 1
                log.warning("NEWS.RECENT: duplicate from previous run skipped | src=%s | %s", source, _short(title))
                continue

            picked.append({"source":source, "title":title, "link":link, "score":sc})
            per_source[source] += 1
            seen_titles.add(nt); seen_links.add(link)
            _remember(key)

            if len(picked) >= want_max:
                break

        log.info("NEWS.PROGRESS: src=%s | picked_now=%d | per_source=%d",
                 source, len([x for x in picked if x['source']==source]), per_source.get(source,0))

        if len(picked) >= want_max:
            break

    picked.sort(key=lambda x: x["score"], reverse=True)
    log.info(
        "NEWS.SUMMARY: extracted=%d | picked=%d | dup_title=%d | dup_link=%d | dup_recent=%d | cap=%d | score_lt_min=%d",
        stats["total_entries"], len(picked), stats["dup_title"], stats["dup_link"],
        stats["dup_recent"], stats["per_source_cap"], stats["score_zero"]
    )
    return picked[:want_max]

def build_news_msg(items):
    lines = ["**🧠 خبرهای اثرگذار بر بازار طلا**", ""]
    for it in items:
        t = _mdv2_escape(it["title"])
        s = _mdv2_escape(it["source"])
        url = it["link"]
        lines.append(f"✔️ {t} (منبع: [{s}]({url}))")
    return "\n".join(lines)

# =========================
# Telegram Handlers
# =========================
async def cmd_gold(update, context):
    user, chat = getattr(update.effective_user,'id',None), getattr(update.effective_chat,'id',None)
    log.info("/gold by user=%s chat=%s", user, chat)
    data = collect_gold()
    msg = build_gold_msg(data)
    await update.message.reply_text(msg, parse_mode="Markdown")
    log.info("GOLD: message sent")

async def cmd_news(update, context):
    user, chat = getattr(update.effective_user,'id',None), getattr(update.effective_chat,'id',None)
    log.info("/news by user=%s chat=%s", user, chat)

    primary = NEWS_FEEDS[:4]
    items = _collect_news_from_range(primary, want_max=4)
    log.info("NEWS: primary picked=%d", len(items))

    if not items:
        fallback = NEWS_FEEDS[4:]
        items = _collect_news_from_range(fallback, want_max=4)
        log.info("NEWS: fallback picked=%d", len(items))

    if not items:
        await update.message.reply_text("❌ خبری پیدا نشد.", disable_web_page_preview=True)
        return

    msg = build_news_msg(items)
    await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
    log.info("NEWS.SEND: sending %d items -> %s", len(items), ", ".join(f"{it['source']}" for it in items))

async def cmd_status(update, context):
    age, meta = _cache_meta()
    age_s = f"{age:.1f}s" if age is not None else "—"
    agg = ", ".join(meta.get("agg_keys", [])) or "—"
    fb  = ", ".join(meta.get("fallback_keys", [])) or "—"
    await update.message.reply_text(
        f"{APP_NAME} v{APP_VER}\nCache age: {age_s} / TTL={CACHE_TTL}s\nAggregate keys: {agg}\nFallback used: {fb}"
    )

async def cmd_help(update, context):
    await update.message.reply_text("دستورات:\n/gold — بازار طلا\n/news — خبرهای اثرگذار\n/status — وضعیت\n/help — راهنما")

# =========================
# Combined Handlers for external use
async def process_gold(store, chat_id, url, chat_lang):
    data = collect_gold()
    msg = build_gold_msg(data)
    return msg


async def process_news(store, chat_id, url, chat_lang):
    # try:
    #     items = _collect_news_from_range(NEWS_FEEDS, want_max=8)
    # except Exception:
    #     last_item = store.get_kv(chat_id, "last_news_item")
    #     if last_item:
    #         return (
    #             "🧠 خبرهای اثرگذار بر بازار طلا\n\n*"
    #             f"✔️ {last_item['title']} "
    #             f"(منبع: [{last_item['source']}]({last_item['link']}))\n\n"
    #             "خبر جدیدی منتشر نشده✖️"
    #         )
    #     return "❌ مشکلی در دریافت اخبار رخ داد."

    # if not items:
    #     last_item = store.get_kv(chat_id, "last_news_item")
    #     if last_item:
    #         return (
    #             "🧠 *خبرهای اثرگذار بر بازار طلا*\n\n"
    #             f"✔️ {last_item['title']} "
    #             f"(منبع: [{last_item['source']}]({last_item['link']}))\n\n"
    #             "خبر جدیدی منتشر نشده✖️"
    #         )
    #     return "❌ خبری پیدا نشد."
    
    items = _collect_news_from_range(NEWS_FEEDS, want_max=8)
    seen = set(store.get_seen(chat_id, url))
    new_items = []
    for it in items:
        key = _make_key(it.get("title", ""), it.get("link", ""))
        if key not in seen:
            new_items.append(it)
            seen.add(key)

    if new_items:
        msg = build_news_msg(new_items)
        store.set_seen(chat_id, url, seen)

        # ذخیره کل خبر اول با متد جدید set_kv
        store.set_kv(chat_id, "last_news_item", new_items[0])

        return msg

    # اگر خبر جدیدی نبود، آخرین خبر ذخیره شده رو نمایش بده
    last_item = store.get_kv(chat_id, "last_news_item")
    if last_item:
        return (
            "🧠 *خبرهای اثرگذار بر بازار طلا*\n\n"
            f"✔️ {last_item['title']} "
            f"(منبع: [{last_item['source']}]({last_item['link']}))\n\n"
            "خبر جدیدی منتشر نشده✖️"
        )

    return "🧠 *خبرهای اثرگذار بر بازار طلا*\n\n❌ خبری در دسترس نیست."

# ---------- Combined provider ----------
async def process_gold_and_news(store, cid_int: int, url: str, lang: str = "fa") -> Optional[str]:
    parts = []

    gold_msg = await process_gold(store, cid_int, url, lang)
    if gold_msg:
        parts.append(gold_msg)

    news_msg = await process_news(store, cid_int, url, lang)
    if news_msg:
        parts.append(news_msg)

    if not parts:
        return None

    return "\n\n".join(parts)

# =========================
# Bootstrap
# =========================
# def main():
#     load_dotenv()
#     token = os.getenv("TELEGRAM_BOT_TOKEN")
#     if not token:
#         log.critical("TELEGRAM_BOT_TOKEN not set"); raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
#     log.info("Starting polling... %s v%s", APP_NAME, APP_VER)
#     app = Application.builder().token(token).build()
#     app.add_handler(CommandHandler("gold",   cmd_gold))
#     app.add_handler(CommandHandler("news",   cmd_news))
#     app.add_handler(CommandHandler("status", cmd_status))
#     app.add_handler(CommandHandler("help",   cmd_help))
#     app.run_polling()

# if __name__ == "__main__":
#     main()
