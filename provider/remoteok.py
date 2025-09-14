import os, re, html, asyncio, logging, requests, jdatetime, time, random, json
from datetime import datetime
from dateutil import parser as dtparser
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ---------- logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("remoteok-bot")

REMOTEOK_API = "https://remoteok.com/api"

# ---------- URL parsing ----------
def parse_remoteok_url(u: str):
    """return ("results", None) or ("job", job_id) or (None, None)"""
    try:
        p = urlparse(u.strip())
        if p.scheme not in {"http", "https"}: return (None, None)
        if p.netloc not in {"remoteok.com", "www.remoteok.com"}: return (None, None)
        path = p.path.rstrip("/")
        if re.fullmatch(r"/remote-[a-z0-9\-]+-jobs", path, re.I):
            return ("results", None)
        m = re.fullmatch(r"/remote-jobs/([a-z0-9\-]+)-(\d+)", path, re.I)
        if m: return ("job", m.group(2))
        return (None, None)
    except:
        return (None, None)

# ---------- utils ----------
def _to_fa_digits(s):
    en, fa = "0123456789", "۰۱۲۳۴۵۶۷۸۹"
    s = str(s)
    for i, d in enumerate(en): s = s.replace(d, fa[i])
    return s

def _thousands(n: int) -> str:
    return f"{n:,}"

def _get_dt(job: dict) -> datetime:
    dt = None
    if job.get("date"):
        try: dt = dtparser.parse(job["date"])
        except: dt = None
    if not dt and job.get("epoch"):
        try: dt = datetime.utcfromtimestamp(int(job["epoch"]))
        except: dt = None
    if not dt: dt = datetime.utcnow()
    return dt.replace(tzinfo=None)

def to_jalali_str(dt_utc: datetime) -> str:
    j = jdatetime.datetime.fromgregorian(datetime=dt_utc)
    fa_months = ["فروردین","اردیبهشت","خرداد","تیر","مرداد","شهریور","مهر","آبان","آذر","دی","بهمن","اسفند"]
    return f"{_to_fa_digits(j.day)} {fa_months[j.month-1]} {_to_fa_digits(j.year)}"

def parse_filters_from_url(url: str):
    m = re.search(r"remote-([a-z0-9\-]+)-jobs", url, re.IGNORECASE)
    parts = [p for p in (m.group(1).split("-") if m else []) if p not in {"and","or","the","a","an"}]
    return {"tags": parts}

# ---------- HTTP helper with retries ----------
def http_get(url, max_tries=3, timeout=20):
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126 Safari/537.36 remoteok-telegram-bot/2.1"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://remoteok.com/",
        "Connection": "close",
    }
    last_exc = None
    for i in range(max_tries):
        try:
            r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            if r.status_code == 200 and r.text.strip():
                return r
            logger.warning("GET %s -> %s (try %d)", url, r.status_code, i+1)
        except Exception as e:
            last_exc = e
            logger.warning("GET %s failed (try %d): %s", url, i+1, e)
        time.sleep(0.6 + random.random()*0.7)
    if last_exc: raise last_exc
    raise RuntimeError(f"Failed to fetch {url}")

# ---------- API ----------
def fetch_remoteok():
    r = requests.get(REMOTEOK_API, headers={"User-Agent": "remoteok-telegram-bot/1.6"}, timeout=15)
    r.raise_for_status()
    data = r.json()
    jobs = [row for row in data if isinstance(row, dict) and row.get("id")]
    logger.info("Fetched %d jobs from API", len(jobs))
    return jobs

# ---------- matching/formatting ----------
def match_job(job: dict, wanted_tags: list[str]) -> bool:
    if not wanted_tags: return True
    hay = " ".join([
        str(job.get("position") or job.get("title") or ""),
        str(job.get("company") or ""),
        " ".join(job.get("tags") or []),
        str(job.get("description") or "")
    ]).lower()
    return all(tag.lower() in hay for tag in wanted_tags)

def format_location(job: dict) -> str:
    return job.get("location") or job.get("candidate_required_location") or "Remote"

def _currency_word(sym: str) -> str:
    return {"$":"دلار", "€":"یورو", "£":"پوند"}.get(sym, "دلار")

def format_salary_fa(job: dict) -> str:
    raw = str(job.get("salary") or job.get("compensation") or "").strip()
    if not raw:
        smin, smax, curr = job.get("salary_min"), job.get("salary_max"), (job.get("currency") or "$")
        if smin or smax:
            try:
                if smin and smax:
                    left  = _to_fa_digits(_thousands(int(float(smin))))
                    right = _to_fa_digits(_thousands(int(float(smax))))
                    return f"{left} {_currency_word(curr)} در سال تا {right} {_currency_word(curr)} در سال"
                n = _to_fa_digits(_thousands(int(float(smin or smax))))
                return f"{n} {_currency_word(curr)} در سال"
            except: pass
        return "نامشخص"
    curr_sym = "$" if "$" in raw else ("€" if "€" in raw else ("£" if "£" in raw else "$"))
    nums = re.findall(r"\d[\d,\.]*", raw)
    if not nums: return "نامشخص"
    try:
        parts = []
        for t in nums[:2]:
            val = int(float(t.replace(",", "")))
            parts.append(_to_fa_digits(_thousands(val)))
        return (f"{parts[0]} {_currency_word(curr_sym)} در سال تا {parts[1]} {_currency_word(curr_sym)} در سال"
                if len(parts) == 2 else f"{parts[0]} {_currency_word(curr_sym)} در سال")
    except:
        return "نامشخص"

# ---------- helpers ----------
_COUNTRY_SLUG_MAP = {
    "united-states": "United States", "usa": "United States",
    "canada": "Canada", "india": "India", "united-kingdom": "United Kingdom",
    "germany": "Germany", "spain": "Spain", "france": "France",
    "worldwide": "Worldwide", "europe": "Europe"
}

def titlecase_slug(s: str) -> str:
    return " ".join([w.capitalize() for w in s.split("-") if w])

# ---------- HTML fallback (DOM + JSON-LD + slug heuristics) ----------
def scrape_job_html(job_url: str) -> dict | None:
    try:
        r = http_get(job_url)
        soup = BeautifulSoup(r.text, "html.parser")

        # 1) JSON-LD (JobPosting)
        for s in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(s.string or s.get_text() or "{}")
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("@type") in ("JobPosting","Job"):
                            data = item; break
                if isinstance(data, dict) and data.get("@type") in ("JobPosting","Job"):
                    title   = (data.get("title") or "").strip() or None
                    company = ((data.get("hiringOrganization") or {}).get("name") or "").strip() or None
                    dateiso = (data.get("datePosted") or data.get("datePublished") or "").strip() or None
                    loc     = None
                    jl = data.get("jobLocation")
                    if isinstance(jl, dict):
                        loc = ((jl.get("address") or {}).get("addressCountry") or jl.get("addressLocality"))
                    salary = None
                    comp = data.get("baseSalary") or {}
                    if isinstance(comp, dict):
                        try:
                            val = comp.get("value") or {}
                            amt = val.get("value") or val.get("minValue")
                            curr = (comp.get("currency") or val.get("currency") or "$")
                            if amt: salary = f"{curr}{int(float(amt)):,}"
                        except: pass

                    dt = None
                    if dateiso:
                        try: dt = dtparser.parse(dateiso)
                        except: dt = None
                    if not dt: dt = datetime.utcnow()

                    return {
                        "id": None, "position": title or "Job Title",
                        "company": company or "Unknown",
                        "salary": salary, "location": loc or "Remote",
                        "tags": [], "date": dt.isoformat(), "url": job_url
                    }
            except Exception:
                continue

        # 2) DOM selectors
        def txt(n): return n.get_text(" ", strip=True) if n else ""

        title = None
        for sel in ["h1", "h1[itemprop=title]", ".job h1", ".content h1", "header h1"]:
            n = soup.select_one(sel)
            if n: title = txt(n); break
        if not title:
            og = soup.find("meta", property="og:title")
            if og and og.get("content"): title = og["content"].strip()
        if not title and soup.title: title = soup.title.get_text(" ", strip=True)

        company = None
        for sel in ["h2 a", "h3 a", ".companyLink", ".company a", ".company", ".job-company", "[itemprop=hiringOrganization]"]:
            n = soup.select_one(sel)
            if n: company = txt(n); break
        if (not company) and title and " at " in title:
            parts = title.split(" at ", 1); title, company = parts[0].strip(), parts[1].strip()
        if not company:
            ogs = soup.find("meta", property="og:site_name")
            company = (ogs.get("content").strip() if ogs and ogs.get("content") else "Unknown")

        salary = None
        for sel in [".salary", ".pay", ".compensation", ".bounty", "[class*=salary]"]:
            n = soup.select_one(sel)
            if n: salary = txt(n); break
        if not salary:
            blob = " ".join(txt(n) for n in soup.select(".tag, .tags, .badges, .benefits")[:10])
            m = re.search(r"[$€£]\s?\d[\d,\.]*", blob)
            if m: salary = m.group(0)

        location = None
        for sel in [".location", ".locations", "[class*=location]"]:
            n = soup.select_one(sel)
            if n: location = txt(n); break
        if not location: location = "Remote"

        tags = []
        for n in soup.select(".tags a, .skills .tag, .tag"):
            s = txt(n)
            if s and s.lower() not in {"remote","worldwide"} and len(tags) < 8:
                tags.append(s)

        dt = None
        mt = soup.find("meta", attrs={"property": "article:published_time"})
        if mt and mt.get("content"):
            try: dt = dtparser.parse(mt["content"])
            except: dt = None

        # 3) slug heuristics (اگر هنوز چیز زیادی نگرفتیم)
        if not title or company == "Unknown":
            p = urlparse(job_url).path.rstrip("/")
            m = re.fullmatch(r"/remote-jobs/([a-z0-9\-]+)-(\d+)", p, re.I)
            if m:
                slug = m.group(1)  # مثل: remote-rater-the-united-states-telus-digital
                parts = slug.split("-")
                # تلاش: شرکت = آخرین 1..3 بخش (رایج: telus-digital)
                for k in (3, 2, 1):
                    if len(parts) >= k:
                        comp_guess = "-".join(parts[-k:])
                        if any(x in comp_guess for x in ["inc","llc","labs","digital","group","company","corp","studio","soft","tech"]):
                            company = titlecase_slug(comp_guess); parts = parts[:-k]; break
                if company == "Unknown":  # حدسی
                    company = titlecase_slug("-".join(parts[-2:])) if len(parts) >= 2 else titlecase_slug(parts[-1])
                    parts = parts[:-2] if len(parts) >= 2 else parts[:-1]
                # عنوان = باقی ابتدای اسلاگ تا قبل از کشورها
                while parts and "-".join(parts[-2:]) in _COUNTRY_SLUG_MAP: parts = parts[:-2]
                if parts and parts[-1] in _COUNTRY_SLUG_MAP: parts = parts[:-1]
                if not title:
                    title = titlecase_slug("-".join(parts)) if parts else "Job Title"
                if location in (None, "Remote"):
                    for i in range(len(slug.split("-")), 0, -1):
                        key = "-".join(slug.split("-")[i-2:i]).lower()
                        if key in _COUNTRY_SLUG_MAP: location = _COUNTRY_SLUG_MAP[key]; break
                    if location in (None, "Remote"):
                        for token in slug.split("-"):
                            if token.lower() in _COUNTRY_SLUG_MAP:
                                location = _COUNTRY_SLUG_MAP[token.lower()]; break

        if not dt: dt = datetime.utcnow()

        return {
            "id": None,
            "position": title or "Job Title",
            "company": company or "Unknown",
            "salary": salary,
            "location": location or "Remote",
            "tags": tags,
            "date": dt.isoformat(),
            "url": job_url
        }

    except Exception:
        logger.exception("HTML scrape failed for %s", job_url)
        return None

# ---------- message builder ----------
def build_message(job: dict) -> str:
    title = job.get("position") or job.get("title") or "Job Title"
    company = job.get("company") or "Unknown"
    dt = _get_dt(job)
    date_hdr_gregorian = dt.strftime("%Y-%m-%d")
    date_body_jalali   = to_jalali_str(dt)
    location = format_location(job)
    tags_list = (job.get("tags") or [])[:5]
    tags = ", ".join([t.strip().title() for t in tags_list]) or "—"
    salary = format_salary_fa(job)
    url = job.get("url") or job.get("apply_url") or job.get("url_job") or ""

    title_h   = html.escape(title)
    company_h = html.escape(company)
    location_h= html.escape(location)
    tags_h    = html.escape(tags)
    url_h     = html.escape(url)

    header = f"<b>{title_h}</b>\n<i>{company_h} | {date_hdr_gregorian}</i>"
    body = (
        f"\n\n🔰 موقعیت شغلی: {title_h}"
        f"\n🏢 شرکت: {company_h}"
        f"\n🌍 موقعیت مکانی: {location_h}"
        f"\n🏷️ مهارت‌ها: {tags_h}"
        f"\n💰 حقوق: {salary or 'نامشخص'}"
        f"\n🗓 تاریخ انتشار: {date_body_jalali}\n\n"
        f"🔗 <a href=\"{url_h}\">لینک آگهی</a>"
    )
    return header + body

# ---------- bot ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Received /start from user_id=%s", update.effective_user.id)
    await update.message.reply_text(
        "سلام! لینک نتایج یا لینک یک آگهی RemoteOK را بفرست.\n"
        "مثال نتایج: https://remoteok.com/remote-marketing-jobs\n"
        "مثال آگهی:  https://remoteok.com/remote-jobs/<slug>-<id>\n"
        "من آگهی‌ها را یکی‌یکی می‌فرستم.",
    )

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    logger.info("Incoming message from user_id=%s: %s", update.effective_user.id, text)

    kind, job_id = parse_remoteok_url(text)
    if kind is None:
        await update.message.reply_text(
            "لطفاً لینک نتایج یا لینک یک آگهی RemoteOK بفرست.\n"
            "مثال نتایج: https://remoteok.com/remote-devops-jobs\n"
            "مثال آگهی:  https://remoteok.com/remote-jobs/<slug>-<id>"
        )
        return

    try:
        jobs = fetch_remoteok()
    except Exception as e:
        logger.exception("Fetch failed")
        await update.message.reply_text(f"خطا در دریافت از RemoteOK: {e}")
        return

    jobs.sort(key=lambda j: j.get("epoch", 0), reverse=True)

    if kind == "job":
        one = next((j for j in jobs if str(j.get("id")) == str(job_id)), None)
        if one:
            logger.info("Sending single job from API id=%s title=%s", one.get("id"), (one.get("position") or one.get("title")))
            await update.message.reply_text(build_message(one), parse_mode="HTML", disable_web_page_preview=True)
            return
        scraped = scrape_job_html(text)
        if scraped:
            logger.info("Sending single job scraped from HTML url=%s", text)
            await update.message.reply_text(build_message(scraped), parse_mode="HTML", disable_web_page_preview=True)
            return
        await update.message.reply_text("این آگهی در API نبود و از صفحه هم نتونستم بخونم.")
        return

    # kind == "results"
    wanted = parse_filters_from_url(text).get("tags", [])
    logger.info("Tags parsed: %s", wanted)
    matched = [j for j in jobs if match_job(j, wanted)]
    logger.info("Matched %d jobs after filtering", len(matched))

    if not matched:
        await update.message.reply_text("نتیجه‌ای مطابق فیلتر یافت نشد.")
        return

    for j in matched[:10]:
        logger.info("Sending job id=%s title=%s", j.get("id"), (j.get("position") or j.get("title")))
        await update.message.reply_text(build_message(j), parse_mode="HTML", disable_web_page_preview=True)
        await asyncio.sleep(0.7)

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled error", exc_info=context.error)

async def process_remoteok(feed, store, cid_int: int, url: str) -> str | None:
    try:
        jobs = fetch_remoteok()
        jobs.sort(key=lambda j: j.get("epoch", 0), reverse=True)

        seen = set(store.get_seen(cid_int, url))
        new_jobs = []

        for j in jobs:
            job_id = str(j.get("id"))
            if not job_id:
                continue
            eid = f"remoteok:{job_id}"
            if eid not in seen:
                new_jobs.append({"id": eid, "job": j})

        if not new_jobs:
            return None

        all_jobs = new_jobs + [
            {"id": f"remoteok:{j.get('id')}", "job": j}
            for j in jobs
            if f"remoteok:{j.get('id')}" in seen
        ]
        final_jobs = all_jobs[:10]

        for j in new_jobs:
            seen.add(j["id"])
        store.set_seen(cid_int, url, seen)

        out_msgs = []
        for item in final_jobs:
            out_msgs.append(build_message(item["job"]))

        return "\n\n".join(out_msgs)

    except Exception as ex:
        logging.exception("process_remoteok failed")
        return None



def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN را تنظیم کنید.")
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_url))
    app.add_error_handler(on_error)
    logger.info("Bot starting…")
    app.run_polling(drop_pending_updates=True)
    logger.info("Polling stopped")

if __name__ == "__main__":
    main()
