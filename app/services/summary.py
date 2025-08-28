# app/services/summary.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import Tuple, List, Optional
import json, re

try:
    import google.generativeai as genai
except Exception:
    genai = None

# --- NEW: lightweight lang tools for Lite/AI output normalization ---
try:
    from langdetect import detect as _ld_detect
except Exception:
    _ld_detect = None

try:
    from deep_translator import GoogleTranslator as _GT
except Exception:
    _GT = None

from app.config import settings


def _system_prompt(lang: str) -> str:
    if (lang or "").lower().startswith("fa"):
        return (
            "خروجی فقط فارسی و روان باشد. لحن تحلیلی و کاربردی.\n"
            "دقیقاً و فقط یک JSON با کلیدهای زیر برگردان؛ هیچ متن اضافه ننویس. "
            "اگر هر بخشی دادهٔ کافی نداشت، مقدار همان بخش را خالی بگذار (آرایهٔ خالی [] یا رشتهٔ خالی \"\").\n"
            "{"
            "\"tldr\":\"۱–۳ جملهٔ جمع‌بندی تحلیلی؛ از تکرار عنوان خودداری کن\","
            "\"bullets\":[\"۳ تا ۶ نکتهٔ نتیجه‌محور؛ هر نکته با فعل شروع شود\"],"
            "\"opportunities\":[\"فرصت‌های کلیدی، مختصر و عملی\"],"
            "\"risks\":[\"ریسک‌ها/محدودیت‌ها، شفاف و واقع‌گرایانه\"],"
            "\"signal\":\"یک پیام/سیگنال کاربردی برای خواننده (در یک یا دو جمله)\""
            "}\n"
            "اگر داده ناکافی بود، محتاطانه خلاصه کن؛ اما باز هم فقط همین JSON را برگردان."
        )
    return (
        "Output must be in clear English with an analytical, practical tone.\n"
        "Return EXACTLY one JSON object with the keys below and nothing else. "
        "If any section lacks sufficient content, leave it empty (use [] for lists and \"\" for strings).\n"
        "{"
        "\"tldr\":\"1–3 analytical sentences; do not repeat the title\","
        "\"bullets\":[\"3–6 action-oriented key points; each starts with a verb\"],"
        "\"opportunities\":[\"Concise, actionable opportunities\"],"
        "\"risks\":[\"Clear, realistic risks/limitations\"],"
        "\"signal\":\"One concise, practical takeaway for the reader\""
        "}\n"
        "If content is limited, summarize cautiously; still return ONLY this JSON."
    )


_CLEAN_BULLET_PREFIX = re.compile(r"^[•\-–—\*\u2022\+\s]+")


def _dedupe_cap(bullets: List[str], cap: int = 6) -> List[str]:
    seen, out = set(), []
    for b in bullets or []:
        b = _CLEAN_BULLET_PREFIX.sub("", (b or "").strip())
        b = re.sub(r"\s+", " ", b)
        if len(b) < 8:
            continue
        key = b.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(b)
        if len(out) >= cap:
            break
    return out


def _strip_code_fences(s: str) -> str:
    return re.sub(r"^```(?:json)?|```$", "", (s or "").strip(), flags=re.I | re.M).strip()


# ---------- NEW: translate helpers ----------
def _detect_lang(s: str) -> str:
    """fa/en/.. or '' if unknown"""
    if not s or not _ld_detect:
        return ""
    try:
        return (_ld_detect(s) or "").split("-", 1)[0].lower()
    except Exception:
        return ""

def _translate(s: str, target: str) -> str:
    """Translate using deep-translator; if unavailable, return s."""
    if not s:
        return s
    t = (target or "").lower()
    if not t or not _GT:
        return s
    try:
        return _GT(source="auto", target=t).translate(s)
    except Exception:
        return s

def _force_lang(tldr: str, bullets: List[str], target_lang: str) -> Tuple[str, List[str]]:
    """
    If summary_strict is enabled or detected language != target, translate.
    Works for both AI and Lite outputs.
    """
    tgt = (target_lang or "").lower()
    if not tgt:
        return tldr, bullets

    strict = str(getattr(settings, "summary_strict", "false")).lower() == "true"

    # quick language guess from concatenated text
    sample = " ".join(([tldr] if tldr else []) + bullets)[:400]
    src = _detect_lang(sample)

    must_translate = strict or (src and src != tgt)
    if not must_translate:
        return tldr, bullets

    tldr_t = _translate(tldr, tgt) if tldr else ""
    bullets_t = [ _translate(b, tgt) for b in (bullets or []) ]
    return tldr_t or tldr, bullets_t or bullets

# ---------- NEW: language enforcement for premium fields ----------
def _force_lang_full(
    tldr: str,
    bullets: List[str],
    opportunities: List[str],
    risks: List[str],
    signal: str,
    target_lang: str,
) -> Tuple[str, List[str], List[str], List[str], str]:
    """
    همان منطق _force_lang اما روی همهٔ فیلدهای پرمیوم نیز اعمال می‌شود.
    """
    tldr2, bullets2 = _force_lang(tldr, bullets, target_lang)

    tgt = (target_lang or "").lower()
    if not tgt:
        return tldr2, bullets2, opportunities, risks, signal

    # تصمیم ترجمه بر اساس همان شرط سخت‌گیرانه
    strict = str(getattr(settings, "summary_strict", "false")).lower() == "true"
    sample = " ".join((opportunities or []) + (risks or []) + ([signal] if signal else []))[:400]
    src = _detect_lang(sample)
    must_translate = strict or (src and src != tgt)

    if not must_translate:
        return tldr2, bullets2, opportunities, risks, signal

    opp2 = [ _translate(x, tgt) for x in (opportunities or []) ]
    risk2 = [ _translate(x, tgt) for x in (risks or []) ]
    sig2  = _translate(signal, tgt) if signal else signal
    return tldr2, bullets2, opp2 or opportunities, risk2 or risks, sig2 or signal
# --------------------------------------------

def _extract_json(raw: str) -> str:
    """
    سعی می‌کنه از متن خام Gemini فقط JSON خالص رو بکشه بیرون.
    """
    if not raw:
        return "{}"
    # حذف بلاک‌های ```json ... ```
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.I | re.M).strip()
    # پیدا کردن اولین و آخرین { }
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        return cleaned[start:end+1]
    return cleaned


class Summarizer:
    """
    Summary chain with Gemini primary and Lite fallback.
    Guarantees (tldr, bullets). Enforces user language if possible.
    """

    def __init__(self, api_key: Optional[str], prompt_lang: str = "fa"):
        self.api_key = api_key
        self.prompt_lang = (prompt_lang or "fa").lower()
        self._fail_count = 0
        self._cooldown_until: Optional[float] = None

    async def _call_ai(self, title: str, text: str) -> Tuple[str, List[str]]:
        if not (genai and self.api_key):
            return "", []
        if self._cooldown_until and time.time() < self._cooldown_until:
            return "", []

        try:
            genai.configure(api_key=self.api_key)
            model = genai.GenerativeModel(settings.summary_model_name)

            prompt = (
                _system_prompt(self.prompt_lang)
                + f"\nTitle: {title or '-'}\n"
                + f"Content:\n{(text or '')[:settings.summary_max_input_chars]}"
            )

            resp = await model.generate_content_async(prompt)
            raw = getattr(resp, "text", "") or ""

            # 🔎 لاگ برای دیباگ
            # print("=== RAW AI OUTPUT ===", raw[:500])

            json_str = _extract_json(raw)
            data = json.loads(json_str)

            tldr = (data.get("tldr") or "").strip()
            bullets = _dedupe_cap(data.get("bullets") or [], cap=getattr(settings, "summary_max_bullets", 4))

            tldr, bullets = _force_lang(tldr, bullets, self.prompt_lang)
            self._fail_count, self._cooldown_until = 0, None
            return tldr, bullets

        except Exception as e:
            import logging; logging.error(f"AI summary error: {e}", exc_info=True)
            self._fail_count += 1
            if self._fail_count >= getattr(settings, "summary_cb_errors", 3):
                import time
                self._cooldown_until = time.time() + getattr(settings, "summary_cb_cooldown_sec", 60)
            return "", []

    # ---------- NEW: full premium summary ----------
    async def summarize_full(
        self, title: str, text: str, author: Optional[str] = None
    ) -> Tuple[str, List[str], List[str], List[str], str]:
        """
        خروجی پرمیوم: (tldr, bullets, opportunities, risks, signal)
        - اگر مدل/کلید در دسترس نباشد یا خطا دهد → خروجی امن با فیلدهای خالی.
        - زبان خروجی روی همهٔ فیلدها enforce می‌شود.
        """
        title = (title or "").strip()
        text = (text or "").strip()
        base = (text if len(text) > getattr(settings, "summary_lite_min_len", 120) else f"{title}\n{text}").strip()
        if not base:
            return "", [], [], [], ""

        if not (genai and self.api_key):
            return "", [], [], [], ""

        import time
        if self._cooldown_until and time.time() < self._cooldown_until:
            return "", [], [], [], ""

        try:
            genai.configure(api_key=self.api_key)
            model = genai.GenerativeModel(settings.summary_model_name)

            prompt = (
                _system_prompt(self.prompt_lang)
                + f"\nTitle: {title or '-'}\n"
                + f"Content:\n{(base or '')[:getattr(settings, 'summary_max_input_chars', 6000)]}"
            )

            resp = await model.generate_content_async(prompt)
            raw = _strip_code_fences(getattr(resp, "text", "") or "")
            data = json.loads(raw)

            # base fields
            tldr = (data.get("tldr") or "").strip()
            bullets = _dedupe_cap(
                [x for x in (data.get("bullets") or []) if isinstance(x, str)],
                cap=getattr(settings, "summary_max_bullets", 4),
            )

            # premium fields
            opp_cap = int(getattr(settings, "summary_max_opportunities", getattr(settings, "summary_max_bullets", 4)))
            risk_cap = int(getattr(settings, "summary_max_risks", getattr(settings, "summary_max_bullets", 4)))

            opportunities = _dedupe_cap(
                [x for x in (data.get("opportunities") or []) if isinstance(x, str)],
                cap=opp_cap,
            )
            risks = _dedupe_cap(
                [x for x in (data.get("risks") or []) if isinstance(x, str)],
                cap=risk_cap,
            )
            signal = (data.get("signal") or "").strip()

            # enforce language on all
            tldr, bullets, opportunities, risks, signal = _force_lang_full(
                tldr, bullets, opportunities, risks, signal, self.prompt_lang
            )

            self._fail_count = 0
            self._cooldown_until = None
            return tldr, bullets, opportunities, risks, signal

        except Exception:
            self._fail_count += 1
            if self._fail_count >= getattr(settings, "summary_cb_errors", 3):
                import time
                self._cooldown_until = time.time() + getattr(settings, "summary_cb_cooldown_sec", 60)
            # بازگشت امن با فیلدهای خالی
            return "", [], [], [], ""

    # ---------- Lite summary (kept disabled) ----------
    # def _lite_summary(self, title: str, text: str) -> Tuple[str, List[str]]:
    #     """
    #     Heuristic TLDR + bullets from the raw text (no AI).
    #     Then enforces prompt_lang via translate helpers.
    #     """
    #     src = (text or "").strip()
    #     if not src:
    #         return "", []
    #     sentences = re.split(r"(?<=[.!؟\?])\s+", src)
    #     tldr = " ".join(sentences[:2]).strip()
    #     tldr = re.sub(r"\s+", " ", tldr)[:300]
    #     points: List[str] = []
    #     for line in re.split(r"[\n\r]+", src):
    #         line = line.strip()
    #         if not line:
    #             continue
    #         if len(line) < 40:
    #             continue
    #         if any(k in line.lower() for k in ("should", "will", "can", "lead", "include", "increase", "reduce", "cause", "help", "need", "است", "می‌شود", "می‌تواند", "خواهد")):
    #             points.append(line)
    #         if len(points) >= getattr(settings, "summary_max_bullets", 4):
    #             break
    #     if not points:
    #         long_sents = [s for s in sentences if len(s) > 50]
    #         points = long_sents[: getattr(settings, "summary_max_bullets", 4)]
    #     bullets = _dedupe_cap(points, cap=getattr(settings, "summary_max_bullets", 4))
    #     tldr, bullets = _force_lang(tldr, bullets, self.prompt_lang)
    #     return tldr, bullets

    async def summarize(
        self, title: str, text: str, author: Optional[str] = None
    ) -> Tuple[str, List[str]]:
        title = (title or "").strip()
        text = (text or "").strip()

        base = (text if len(text) > getattr(settings, "summary_lite_min_len", 120) else f"{title}\n{text}").strip()
        if not base:
            return "", []

        # Try AI and return the result directly
        tldr, bullets = await self._call_ai(title, base)

        # Optional second attempt
        if not (tldr or bullets):
            tldr, bullets = await self._call_ai(title, base + "\n(Please ensure at least 3 bullet points.)")

        # Return AI result or empty if both attempts fail
        return tldr, bullets

        # 3) Lite fallback (disabled)
        # return self._lite_summary(title, base)
