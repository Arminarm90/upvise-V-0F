#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Project: Xminit Support Bot (Prompt-driven, source-silent)
Files:
  - bot.py
  - Prompt.md
  - FAQ.json
  - Requirements.txt
  - .env  (TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, ADMIN_CHAT_ID)

Key behaviors in this build:
- Never shows any source IDs or file names to the user.
- Never mentions "FAQ" or any knowledge file in user-facing text.
- Only ONE command is supported: /support
- /support sends English welcome by default + two inline buttons (🇮🇷 فارسی | 🇬🇧 English).
- Pressing a language button switches conversation language instantly; enforced in model prompt.
- Shows the typing indicator while preparing a reply (fix: offloads blocking calls to a thread).
"""

import os
import json
import logging
import datetime
from typing import List, Dict, Any, Tuple, Optional

import asyncio
import numpy as np
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from app.storage.state import SQLiteStateStore  # مسیر دقیق ماژول دیتابیس شما
store = SQLiteStateStore()

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("XminitSupportBot")

# ---------- Environment ----------
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("SUPPORT_GEMINI_API_KEY", "").strip()
ADMIN_CHAT_ID = os.getenv("SUPPORT_ADMIN_CHAT_ID", "").strip()

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in .env")
if not GEMINI_API_KEY:
    raise RuntimeError("Missing GEMINI_API_KEY in .env")
if not ADMIN_CHAT_ID or not ADMIN_CHAT_ID.isdigit():
    raise RuntimeError("ADMIN_CHAT_ID must be numeric")
ADMIN_CHAT_ID = int(ADMIN_CHAT_ID)

# ---------- Gemini Config ----------
import google.generativeai as genai
genai.configure(api_key=GEMINI_API_KEY)

MODEL_NAME = "gemini-2.5-flash"
EMBED_MODEL = "text-embedding-004"

GENERATION_CONFIG = {
    "temperature": 0.3,
    "top_p": 0.8,
    "top_k": 32,
    "max_output_tokens": 4096,
    "response_mime_type": "application/json",  # 👈 تغییر از application/json
}


# ---------- Paths ----------
PROMPT_PATH = "app/support/Prompt.md"
FAQ_PATH = "app/support/FAQ.json"

# ---------- FAQ Store (internal-only, never exposed to user) ----------
class FAQStore:
    def __init__(self, faq_path: str):
        self.path = faq_path
        self.items: List[Dict[str, Any]] = []
        self.chunks: List[Dict[str, Any]] = []
        self.embeddings: Optional[np.ndarray] = None  # (N, D)

    def load(self):
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Supports {"items":[...]} structure. If you switched to a raw list, wrap it as {"items": [...]}
        self.items = data.get("items", [])
        self.chunks = []
        for it in self.items:
            text = f"Q: {it.get('q','').strip()}\nA: {it.get('a','').strip()}"
            self.chunks.append({
                "id": it.get("id"),
                "text": text,
                "tags": it.get("tags", []),
                "source_confidence": float(it.get("source_confidence", 1.0)),
            })
        log.info(f"Loaded {len(self.chunks)} FAQ items.")

    # ---- Robust embedding extraction (recursive over varied SDK shapes) ----
    def _extract_values_recursive(self, obj: Any) -> Optional[List[float]]:
        if isinstance(obj, list):
            if obj and isinstance(obj[0], (int, float)):
                return [float(x) for x in obj]
            for x in obj:
                got = self._extract_values_recursive(x)
                if got is not None:
                    return got
            return None
        if isinstance(obj, dict):
            if "embedding" in obj and isinstance(obj["embedding"], dict):
                vals = obj["embedding"].get("values", [])
                if isinstance(vals, list) and vals and isinstance(vals[0], (int, float)):
                    return [float(x) for x in vals]
            if "embeddings" in obj and isinstance(obj["embeddings"], list) and obj["embeddings"]:
                first = obj["embeddings"][0]
                if isinstance(first, dict):
                    if isinstance(first.get("values"), list) and first["values"] and isinstance(first["values"][0], (int, float)):
                        return [float(x) for x in first["values"]]
                    if "embedding" in first and isinstance(first["embedding"], dict):
                        vals = first["embedding"].get("values", [])
                        if vals and isinstance(vals[0], (int, float)):
                            return [float(x) for x in vals]
            if "data" in obj and isinstance(obj["data"], list):
                for it in obj["data"]:
                    got = self._extract_values_recursive(it)
                    if got is not None:
                        return got
            if "values" in obj and isinstance(obj["values"], list) and obj["values"] and isinstance(obj["values"][0], (int, float)):
                return [float(x) for x in obj["values"]]
            for v in obj.values():
                got = self._extract_values_recursive(v)
                if got is not None:
                    return got
            return None
        emb = getattr(obj, "embedding", None)
        if emb is not None:
            vals = getattr(emb, "values", None)
            if isinstance(vals, list) and vals and isinstance(vals[0], (int, float)):
                return [float(x) for x in vals]
        return None

    def _embed_one(self, text: str) -> np.ndarray:
        resp = genai.embed_content(model=EMBED_MODEL, content=text)
        vals = self._extract_values_recursive(resp)
        if vals is None:
            raise ValueError("Could not parse embedding values from embed_content() response.")
        return np.array(vals, dtype=np.float32)

    def embed_all(self):
        if not self.chunks:
            self.embeddings = np.zeros((0, 768), dtype=np.float32)
            return
        vecs = [self._embed_one(c["text"]) for c in self.chunks]
        self.embeddings = np.vstack(vecs).astype(np.float32)
        log.info(f"Built embeddings: shape={self.embeddings.shape}")

    def search(self, query: str, top_k: int = 4) -> List[Dict[str, Any]]:
        if self.embeddings is None or len(self.chunks) == 0:
            return []
        q_vec = self._embed_one(query)
        q = q_vec / (np.linalg.norm(q_vec) + 1e-9)
        A_norm = self.embeddings / (np.linalg.norm(self.embeddings, axis=1, keepdims=True) + 1e-9)
        sims = A_norm @ q
        idx = np.argsort(-sims)[:max(1, top_k)]
        return [{"id": self.chunks[i]["id"], "text": self.chunks[i]["text"], "similarity": float(sims[i])} for i in idx]

faq_store = FAQStore(FAQ_PATH)

# ---------- Helpers ----------
def load_prompt() -> str:
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()

def utc_now() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

def user_identity(update: Update) -> Tuple[str, int]:
    u = update.effective_user
    return (u.username or "unknown", u.id)

def build_context(snips: List[Dict[str, Any]]) -> str:
    if not snips:
        return "[no relevant entries]"
    # Provide internal IDs for traceability inside the model, but we never show them to the user.
    return "\n\n".join([f"[source_id={s['id']}]\n{s['text']}" for s in snips])

def parse_json(text: str) -> Dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end+1])
        raise

async def send_admin_alert(ctx: ContextTypes.DEFAULT_TYPE, upd: Update, ans: str, reason: str, conf: float):
    user, uid = user_identity(upd)
    msg = upd.effective_message.text or ""
    alert = (
        f"🚨 Support Alert\n"
        f"User: @{user} (ID: {uid})\n"
        f"Reason: {reason} (confidence: {conf:.2f})\n"
        f"Time: {utc_now()}\n\n"
        f"Last message:\n{msg}\n\n"
        f"AI reply:\n{ans}"
    )
    try:
        await ctx.bot.send_message(chat_id=ADMIN_CHAT_ID, text=alert)
    except Exception as e:
        log.error(f"Failed to send alert: {e}")

# ---------- Typing Indicator ----------
async def _typing_indicator(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, stop_event: asyncio.Event):
    try:
        # Send once immediately so the user sees it right away
        await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        # Refresh every ~4s until stopped
        while not stop_event.is_set():
            await asyncio.sleep(4)
            await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception as e:
        log.debug(f"Typing indicator stopped: {e}")

# ---------- Gemini call (blocking SDK wrapped) ----------
def call_gemini(prompt: str, ctx_block: str, hist: List[Tuple[str, str]], user_text: str, lang: str) -> Dict[str, Any]:
    model = genai.GenerativeModel(model_name=MODEL_NAME, generation_config=GENERATION_CONFIG)
    hist_text = "\n".join([f"{r.upper()}: {m}" for r, m in hist[-4:]])
    language_override = (
        "\n\n# LANGUAGE OVERRIDE\n"
        f"User preferred language is '{lang}'. "
        "Always reply in this language for both `answer` and `follow_up_question`."
    )
    payload = [
        {"role": "user", "parts": prompt + language_override},
        {"role": "user", "parts": f"\n---\nCONTEXT:\n{ctx_block}\n---"},
        {"role": "user", "parts": f"CHAT HISTORY:\n{hist_text}"},
        {"role": "user", "parts": f"USER:\n{user_text}"},
    ]
    resp = model.generate_content(payload)  # blocking

    # 🚨 افزودن مدیریت خطا: بررسی بلاک شدن پاسخ
    if not resp.candidates:
        # هیچ کاندیدی وجود ندارد (معمولاً به معنای بلاک شدن کل پرامپت است)
        log.warning(f"Response blocked. Finish reason: {getattr(resp.prompt_feedback, 'block_reason', 'N/A')}")
        return {"alert_flag": True, "alert_reason": "Prompt blocked by safety filters.", "confidence": 0.0, "answer": "", "follow_up_question": ""}

    candidate = resp.candidates[0]
    if candidate.finish_reason.value != 1: # 1 is 'STOP' (success)
        # Finish reason != STOP (مثلاً 2: MAX_TOKENS یا 3: SAFETY)
        log.warning(f"Candidate response finished with reason: {candidate.finish_reason.name}")
        if candidate.finish_reason.value == 3: # 3 is 'SAFETY'
            return {"alert_flag": True, "alert_reason": "Candidate blocked by safety filters.", "confidence": 0.0, "answer": "", "follow_up_question": ""}
    # ... ادامه کد:    
    
    return parse_json(getattr(resp, "text", "") or "{}")

# ---------- Language & UI ----------
WELCOME_EN = "Hi! 👋 I'm the Xminit Support Assistant — how can I help you?"
WELCOME_FA = "سلام! 👋 من دستیار پشتیبانی Xminit هستم، چطور می‌تونم کمکتون کنم؟"

LANG_PREF: Dict[int, str] = {}  # chat_id -> 'en' or 'fa'

def language_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text="🇮🇷 فارسی", callback_data="lang_fa"),
        InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en"),
    ]
    return InlineKeyboardMarkup([buttons])

# ---------- Handlers ----------
async def support_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lang = store.get_lang(chat_id)
    LANG_PREF[chat_id] = lang

    if lang == "fa":
        await update.message.reply_text(WELCOME_FA)
    else:
        await update.message.reply_text(WELCOME_EN)


async def on_lang_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    if data == "lang_fa":
        LANG_PREF[chat_id] = "fa"
        store.set_chat(chat_id, {"lang": "fa"})
        await ctx.bot.send_message(chat_id, WELCOME_FA, reply_markup=language_keyboard())

    elif data == "lang_en":
        LANG_PREF[chat_id] = "en"
        store.set_chat(chat_id, {"lang": "en"})
        await ctx.bot.send_message(chat_id, WELCOME_EN, reply_markup=language_keyboard())


HISTORY: Dict[int, List[Tuple[str, str]]] = {}

async def on_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not update.effective_message.text:
        return
    chat_id = update.effective_chat.id
    text = update.effective_message.text.strip()
    lang = store.get_lang(chat_id)

    # Start typing indicator while we work
    stop_event = asyncio.Event()
    typing_task = asyncio.create_task(_typing_indicator(ctx, chat_id, stop_event))

    try:
        # Build retrieval context (offload blocking parts)
        hist = HISTORY.setdefault(chat_id, [])

        # faq_store.search embeds the query via a blocking API call — offload to a thread:
        snippets: List[Dict[str, Any]] = await asyncio.to_thread(faq_store.search, text)
        ctx_block = build_context(snippets)
        prompt = await asyncio.to_thread(load_prompt)

        # Gemini generate_content is blocking — offload to a thread:
        try:
            result: Dict[str, Any] = await asyncio.to_thread(
                call_gemini, prompt, ctx_block, hist, text, lang
            )
        except Exception as e:
            log.error(f"Gemini error: {e}")
            await update.message.reply_text(
                "Sorry—something went wrong. Please try again." if lang == "en" else "متأسفم—مشکلی پیش آمد. لطفاً دوباره تلاش کنید."
            )
            return

        ans = (result.get("answer") or "").strip()
        follow = (result.get("follow_up_question") or "").strip()
        try:
            conf = float(result.get("confidence", 0.0))
        except Exception:
            conf = 0.0
        alert = bool(result.get("alert_flag", False))
        reason = (result.get("alert_reason") or "").strip()
        # NOTE: We intentionally ignore/never show `sources` to user.

        # Compose user-facing reply (no sources, no file mentions)
        if ans:
            reply = ans
            reply = (
                reply.replace("*", "")  # remove markdown bold
                    .replace("**", "")
                    .replace("`", "")
                    .replace("_", "")
                    .replace("•", "▪️")  # unify bullets
                    .replace("*", "")
                    .replace(" - ", " • ")  # standard bullet
                    .replace("\n\n\n", "\n\n")  # collapse triple newlines
                    .strip()
            )

            # Ensure extra spacing between sections
            reply = reply.replace(":\n", ":\n\n")

            # If model uses markdown lists like "1.", fix them visually
            import re
            reply = re.sub(r"^\s*[\*\-\d]+\.\s*", "• ", reply, flags=re.M)
        else:
            reply = "I don’t have that information right now." if lang == "en" else "الان این اطلاعات رو ندارم."
        # if follow:
        #     reply += f"\n\n┄\n{follow}"

        await update.message.reply_text(reply)
        hist.append(("user", text))
        hist.append(("assistant", ans or reply))

        if alert:
            await send_admin_alert(ctx, update, ans or reply, reason or "Prompt-triggered", conf)
    finally:
        # Stop typing indicator
        stop_event.set()
        try:
            await typing_task
        except Exception:
            pass

# ---------- Main (sync) ----------
# def main():
#     log.info("Loading knowledge...")
#     faq_store.load()
#     log.info("Embedding knowledge...")
#     faq_store.embed_all()
#     log.info("Starting bot...")
#     app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
#     # ONLY ONE COMMAND: /support
#     app.add_handler(CommandHandler("support", support_cmd))
#     app.add_handler(CallbackQueryHandler(on_lang_button, pattern=r"^lang_(fa|en)$"))
#     app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_msg))
#     app.run_polling()

# if __name__ == "__main__":
#     main()
