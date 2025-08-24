from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from ..utils.text import ensure_scheme

def _rss(ctx):     return ctx.bot_data["rss"]
def _search(ctx):  return ctx.bot_data["search"]
def _store(ctx):   return ctx.bot_data["store"]

async def discover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("مثال: /discover مدیریت محصول یا /discover https://site.com")

    query = " ".join(context.args).strip()
    svc = _rss(context)

    if query.startswith(("http://", "https://")) or "." in query:
        site = ensure_scheme(query)
        found = await svc.discover_feeds(site)
    else:
        sites = await _search(context).sites_by_specialty(query)
        found = []
        for s in sites[:6]:
            found.extend(await svc.discover_feeds(s))

    if not found:
        return await update.message.reply_text("چیزی پیدا نشد.")

    # ساخت دکمه‌های افزودن
    cbmap_all = context.bot_data.setdefault("cbmap", {})
    cmap = cbmap_all.setdefault(update.effective_chat.id, {})
    rows, shown = [], set()
    for (url, title) in found[:12]:
        if url in shown:
            continue
        shown.add(url)
        key = str(len(cmap) + 1)
        cmap[key] = url
        btn = InlineKeyboardButton(f"➕ افزودن: {title or url}", callback_data=f"pick|{key}")
        rows.append([btn])

    await update.message.reply_text("فیدهای پیدا شده: 🔎", reply_markup=InlineKeyboardMarkup(rows))

async def pick_add_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not (q.data or "").startswith("pick|"):
        return
    key = q.data.split("|", 1)[1]

    url = context.bot_data.get("cbmap", {}).get(q.message.chat.id, {}).get(key)
    if not url:
        return await q.answer("منقضی شد، دوباره /discover بزن.", show_alert=False)

    svc = _rss(context)
    store = _store(context)

    if not await svc.is_valid_feed(url):
        return await q.answer("RSS معتبر نیست.", show_alert=False)

    if store.add_feed(q.message.chat.id, url):
        try:
            await q.edit_message_text(f"✅ اضافه شد:\n{url}")
        except Exception:
            await q.answer("✅ اضافه شد.", show_alert=False)
    else:
        await q.answer("ℹ️ قبلاً اضافه شده.", show_alert=False)
